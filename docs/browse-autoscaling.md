# Browse pool policy and autoscaling

## Evidence and decision

The three-minute trial passed with **250 paced users**, **21,133 HTTP 200
responses**, zero errors, p95 **167.385 ms**, p99 **188.764 ms**, and maximum
267.868 ms. It ran for 180.132 seconds at 117.32 requests/second. Both pools
remained at eight connections, with no new connection attempts or pool errors.
Database minute samples reached CPU 8.64%, memory 21.36%, and 28 connections.
The wrapper restored four connections per pod and verified health afterward.
Run: `5707b72eb33d`. This is a bounded read experiment, not an SLO or a maximum
capacity claim.

The selected **autoscaling profile uses eight ready connections per Browse
process**, queue limit **16**, and **two to four Browse pods**. Each pod runs
one Uvicorn process. The previous fixed-two-pod manifest remains a historical
four-connection baseline; the autoscaling deploy command applies the new
profile to the existing immutable image. No image rebuild is needed.

## Connection budget

Use **128 total PostgreSQL connections as a planning budget** for this lab.
This is deliberately much smaller than the reported server limit of 859.
It is not a PostgreSQL-enforced quota or a claim that 128 busy queries are safe.

| Allocation | Connections |
| --- | ---: |
| Browse at four pods, eight each | 32 |
| Browse terminating-process overlap allowance | 32 |
| Booking, including rollout allowance | 16 |
| Publisher, consumer, expiry, Operations allowance | 16 |
| Diagnostics, renewal, migrations, administration | 16 |
| Unallocated reserve | 16 |
| Total planning budget | 128 |

The Browse rollout uses `maxSurge: 0`, `maxUnavailable: 1`. Terminating pods
can temporarily retain connections; the overlap allowance is a planning
assumption, not a hard Kubernetes bound. Run one rollout or diagnostic at a
time. Replica limits, one process per pod and pool maxima bound steady Browse
usage to 32. Before raising these limits, re-audit every workload and budget.
The drill stops on sampled database CPU >=70%, memory >=80%, or connection
count >=112, preserving reserve. Minute aggregates and publication delay can
miss short spikes, and connections include idle sessions.

## Scaling signals

The AKS managed KEDA addon creates a native HPA, `trafficxx-browse`.
It reads the existing private Prometheus service:

- **Pool pressure:** busy/preparing slots plus waiting borrowers; target five
  per pod. This reflects occupancy and queueing when CPU is low.
- **Borrow rate:** one-minute rate of database borrows; target 40/second per
  pod. This includes readiness reads and is a conservative proxy for read
  demand, rather than an exact user HTTP counter.

Both metrics use `AverageValue`: a total pressure of 12 requests three pods
at a target of five, subject to HPA tolerance and controller timing. The HPA
uses whichever signal asks for more replicas. It adds at most one pod per
minute, and waits five minutes before reducing pods, at most one per minute.
These are initial tuning choices to test, not measured production thresholds.
Missing, stale or unhealthy scrape data returns a scaler error, rather than
an explicit zero. `ignoreNullValues` is false; no scale-to-zero is configured.
Discovery can temporarily lag new ready pods; scaling is reactive, so bounded
queues and HTTP 503 protection still matter.

The addon can consume cluster resources and the existing node autoscaler may
add a VM if pods cannot be scheduled. Its configured node limits are unchanged.
No public monitoring endpoint, additional SQL grant, or database SKU change
is required.

## Deploy from Cloud Shell

```bash
cd ~/trafficxx
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py deploy
```

This checks account/context, health, active load Jobs and database pressure;
patches only pool policy and rollout strategy on the current image; enables
the AKS managed addon if absent; applies the generated ScaledObject; and
requires active HPA metrics. Allow several minutes for the managed addon.
The script can resume deployment, but refuses a foreign HPA. No load starts
in this step. After success, the normal policy remains eight connections.

Persist `enable_browse_keda = true` in your existing **private Terraform input
file** before future foundation plans, so Terraform retains the addon enabled
by the CLI. Review future plans normally; no full infrastructure apply is
needed for this command. The checked-in variable defaults false for opt-in.

Do not apply `browse-autoscaling.json` directly: it is the policy template;
the deploy script adds the reviewed pressure queries before applying it.
The old `deploy-browse.sh` now refuses an active Browse HPA so it cannot reset
replicas or revert the four-connection baseline. Earlier fixed-pod load and
pool checks are historical experiments; use this profile's checker while
autoscaling is enabled:

```bash
python3 scripts/browse-autoscaling.py check
```

## Real scale-up and recovery drill

After deployment and its metric check succeed:

```bash
python3 scripts/browse-autoscaling.py drill
```

Keep Cloud Shell running through the final result; allow 15-20 minutes.
The new, preserved Job is `browse-users-250-autoscaling`. Its credential-free
generator runs 250 paced Browse users for 180 seconds, two-second pauses,
at most 22,500 GETs, one CPU and 256 MiB. It targets the internal Service so
new pods can participate. No booking writes, retries, manual replica changes,
or automatic higher-user stages occur.

The resource gate requires two distinct fresh source samples per initial pod
and generator. Pod metric age is limited to 90 seconds; new pods receive
bounded metrics warmup. The observer retains the previous sustained CPU80%
and memory85% stops, checks pool error-counter increases and sampled sustained
waiting, and samples database load every minute. HTTP errors or requests over
two seconds stop the generator. Observations remain sampled, not continuous.

A pass requires all 250 users, zero HTTP errors, workload duration 180-195
seconds, **at least three ready pods under the real HPA**, then **two ready
pods through natural downscaling**, fresh active metrics, post-run inventory
and Operations health, and database observations. Latency percentiles and
resource samples are printed. If no scale-up occurs, the drill does not pass;
do not manually scale to manufacture evidence. No live autoscaling deployment
or drill result has been supplied yet.

## Next bounded stage: 500 paced users

After the complete v3 pass, the user authorized a higher stage. The prepared
command is:

```bash
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py drill --users 500
```

This creates the separate, preserved Job `browse-users-500-autoscaling`.
It reuses the existing immutable image and mounts the reviewed updated probe;
no build, rollout, database upgrade or autoscaling-policy change is needed.
Only this selected stage runs; 1,000 users are unsupported.

| Parameter | 500-user stage |
| --- | --- |
| Users | 500, with every participant required for success |
| Arrival ramp | 60 seconds inside the test window |
| Total workload window | 180 seconds; users arriving near the end of the ramp have about 120 seconds remaining |
| Think time | Two seconds after each request |
| Workload cap | 45,000 GET attempts, at most 90 per user |
| Generator budget | One CPU, 256 MiB; bound to an existing apps node |
| App policy | Eight connections per pod, queue 16, HPA minimum two / maximum four |
| Connection budget | 32 steady Browse connections at four pods; total lab planning budget 128 |
| Mutations | No booking or inventory writes; no manual replica changes |

The ramp gives reactive scaling time to respond; it does not guarantee that
the current policy can absorb 500 users. This differs from the 250-user test's
two-second arrival spread, so latency differences are not a controlled
user-count-only comparison. Five hundred paced users are neither 500 RPS nor
500 simultaneous requests. The staged duration includes the ramp.

The same fresh-source resource gate, sampled CPU/memory stops, two-second
request stop, pool-error and sustained-waiting checks, database pressure
checks, 60-second cumulative HPA metric-gap bound, UID/template protection,
and natural downscale proof remain. The generator budget has not been raised:
the previous 250-user generator used about 41 MiB and 0.091 CPU in sampled
metrics, but that is not a guarantee of 500-user generator headroom.

Keep Cloud Shell open through the final result; allow 15-20 minutes. A pass
requires all 500 users, matching HTTP 200 counts, no request errors, verified
60-second arrival ramp and 45,000-attempt cap, at least three ready pods
observed under the HPA, then natural return to two ready pods and final health
and database observations. It reports `BROWSE_AUTOSCALING_DRILL_OK` with
`users: 500` and the distinct Job name. The first live attempt stopped early,
as recorded below. A later earlier-scaling workload passed, with a separate
recovery check; its continuous observer audit remains incomplete.

On failure, inspect this Job and logs without starting another load. The
observer attempts to stop only its own Job; the native 450-second deadline
also bounds a disconnected generator. Once the load is terminal, use
`python3 scripts/browse-autoscaling.py wait-idle` for read-only recovery.
Do not delete previous Jobs or increase limits simply to obtain a pass.

### First 500-user attempt stopped on HTTP 503

Run `27651353c0d9`, Job `browse-users-500-autoscaling`, stopped at 40.795
seconds. Of the planned 500 users, 338 had participated. There were 3,360
attempts: 3,359 HTTP 200 and one HTTP 503. Observed latency was p50 137.564 ms,
p95 240.005 ms, p99 276.534 ms and maximum 311.135 ms. These describe the
truncated failed run, not a completed 500-user result. No inventory writes
occurred. The observer still reported two desired and ready pods before stop.

Follow-up inspection confirmed the Job Failed 0/1, a healthy active HPA at
two current/desired replicas, demo availability 100 per event, and healthy
Operations with no backlog or active alerts. HPA resize events in that
inspection were from earlier activity, not this short attempt. No scale-up
was observed in the supplied 500-user record.

Traffic growing faster than reactive scale-up is a hypothesis to investigate,
not a confirmed root cause. The original probe reported the HTTP status but
did not retain its service error code. Future probes now record only an
allowlisted first-failure code (`DATABASE_BUSY` or `DATABASE_UNAVAILABLE`),
route and timing; no raw body or headers. Updating the source cannot recover
the missing code from this retained Job's older mounted probe.

Before changing thresholds, arrival pacing or pool capacity, collect this
read-only diagnostic from Cloud Shell:

```bash
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py diagnose-500
```

It requires the original Job to be terminal and matches its run ID to the
stored result. It queries historical queue-full, timeout, closed-pool, borrow
and connection-error counters, plus sampled healthy-target, occupancy, waiting
and borrow-rate peaks. Peak queries end at that Job's terminal timestamp;
counter queries extend 60 seconds past termination to include the next scrapes.
Both include warmup and 60 seconds before the Job's start. Prometheus `increase`
values are extrapolated estimates; missing samples remain null, not zero.
The command creates no Job, makes no configuration or SQL changes and does
not replay traffic. `BROWSE_500_DIAGNOSTICS_OK` means capture succeeded, not
that the load passed. Historical samples are retained for the collector's
configured retention period. Review this evidence before selecting a retry.

The first diagnostic for this failed run used the exact terminal cutoff,
14:50:30 UTC, and reported zero increases for every pool-error counter.
Its sampled peaks were two healthy targets, zero waiting, nine busy/opening
slots and a total one-minute borrow rate of 44.8/second. These measurements
do not establish a queue-full cause or a complete 500-user capacity result.

Code review identified a boundary issue: the 503 can happen after the last
scrape before Job termination. Its cumulative rejection count may first be
exported by the following scrape, outside the original diagnostic window.
The corrected counter query ends at 14:51:30 UTC and checks for post-terminal
samples from every instance visible at the cutoff. Peak queries retain their
original cutoff, so later idle activity is not mixed into workload peaks.
The original zero values therefore do not rule out a final unsampled-at-cutoff
rejection. This is an observation fix, not proof of the actual 503 cause.

Run the same read-only `diagnose-500` command after pulling the correction.
Inspect `post_job_sample_coverage` alongside the counter estimates; absent
instances or post-run samples leave evidence incomplete. A real Prometheus
regression test reproduces a zero at the old cutoff and a rejection increase
in the extended window. No load, application image or scaling policy change
is required to retrieve the additional history.

The extended live diagnostic subsequently reported queue-full and borrow-error
increases of **1.039573 each**, with zero timeout, closed-pool, connection-error,
lost-connection and bad-return increases. Both original instances had samples
after Job termination. The fractional figure is Prometheus extrapolation,
consistent with roughly one rejection; the two counters can describe the same
failed borrow and must not be added as two separate failures. This is evidence
of a queue-full rejection consistent with the lone HTTP 503. The instantaneous
queue spike was not visible in the 15-second sampled peak. It does not show
database server saturation or prove precisely why scaling had not yet reacted.

### Prepared comparison: slower arrival, unchanged capacity

The next bounded comparison changes only the arrival ramp, from 60 to **120
seconds**, while keeping the same 180-second workload window. All 500 users
must participate; the full population is therefore present for approximately
the final **60 seconds**, instead of about 120 seconds in the original plan.
This tests a slower traffic arrival pattern, not a fix for the original faster
ramp or a stronger sustained-load claim.

```bash
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py drill --users 500 --slower-ramp
```

The distinct Job is `browse-users-500-autoscaling-ramp120`. Both failed-run
evidence and the prior 250-user passes are preserved. The same eight
connections per pod, 16 waiting slots, two-four replica bounds, scaling
thresholds, one CPU / 256 MiB generator, 45,000-attempt ceiling and failure
guards remain. No application build or deployment is needed. The parent
requires the actual 120-second ramp in the result, zero request errors,
observed automatic scale-up and natural return to two pods with healthy
post-run checks. There is no live slower-ramp result yet and success is not
guaranteed. Keep Cloud Shell running for the final result.

If this comparison fails, keep its Job and use the read-only
`python3 scripts/browse-autoscaling.py diagnose-500-slow` command for that
specific run; `diagnose-500` continues to inspect the original 60-second ramp.

## Failure or disconnect

### 27 October: corrected-observer verification passed

Fresh user-run checks passed: two Browse replicas, eight connections per pod,
zero pool pressure, demo availability 100 per event, and healthy Operations
with no backlog or active alerts. To verify the corrected observer through
one complete run while preserving both previous Jobs:

```bash
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py drill --verification-run
```

This selects the distinct Job `browse-users-250-autoscaling-v3`. It uses the
same 250 paced users, 180-second workload, two-second pauses, 22,500 GET cap,
resource budgets, metric guards, scaling bounds and natural downscale checks.
It does not rebuild or redeploy the app, change pool policy or increase load.
Keep Cloud Shell running through `BROWSE_AUTOSCALING_DRILL_OK`; allow 15-20
minutes for resource sampling, workload and downscale stabilization.

The user supplied the full live pass, run `f807afdc2940`, Job
`browse-users-250-autoscaling-v3`:

| Measurement | Result |
| --- | --- |
| Users participating | 250 of 250 |
| Responses | 21,129 HTTP 200, zero request errors |
| Workload elapsed | 182.14 seconds |
| Latency p50 / p95 / p99 / maximum | 128.172 / 159.051 / 176.862 / 387.427 ms |
| Ready Browse replicas | Two before, three under load, two after natural downscale |
| HPA metric gaps | One, recovered in 16.78 seconds |
| Final inventory | 100 available tickets per demo event, no writes |
| Final Operations | Healthy workers, zero backlog and active alerts; 72 processed events |
| Database observations | 12 per metric over 14:23:10-14:35:10 UTC, including warmup/recovery |
| Database window maxima | CPU 8.71%, memory 21.04%, connections 35, including idle sessions |
| Final result | `BROWSE_AUTOSCALING_DRILL_OK` |

Sampled resource maxima were:

| Process | Distinct source samples | CPU cores | Memory MiB |
| --- | ---: | ---: | ---: |
| Initial Browse pod pblw4 | 7 | 0.074280 | 72.473 |
| Initial Browse pod z9wkl | 7 | 0.077136 | 72.449 |
| Added Browse pod jn4zr | 3 | 0.155997 | 64.770 |
| Generator lfsm4 | 5 | 0.091138 | 41.043 |

The corrected observer completed its cycle without the earlier false failure
at generator completion. The transient HPA gap was explicitly recorded and
recovered inside its limit; it was not replaced with a zero pressure reading.
The current policy remains eight connections per pod, queue limit 16, and
two-to-four Browse pods, with two ready at the final check. Sampling still
cannot establish continuous resource peaks or a production latency SLO. Heavy
traffic ended before downscale, so HTTP continuity during downscale is not
proved by this run. No 500- or 1,000-user experiment ran.

The first live drill, run `1ec4a7395615`, installed the managed addon and
observed two, three, then four ready Browse pods. HPA events independently
confirmed both resizes. The observer stopped on `HPA_METRIC_NOT_ACTIVE`;
the failed Job is retained. Subsequent inspection showed an error fetching
the borrow-rate metric, followed by recovery to `ScalingActive=True`.
Browse availability remained 100 per event and Operations was healthy.
At that inspection, four pods were still held by downscale stabilization;
natural recovery to two and the full workload were not yet verified.

The exact cause and duration of the metric gap are not established by the
event summary. New-target rate warmup is one possible contributor: the
reviewed rate query requires enough samples from every discovered target.
No query, freshness guard, resource limit or scaling bound is changed to
hide missing measurements.

The observer now allows metric inactivity for less than 60 seconds, both
per episode and cumulatively during the load. It still verifies HPA ownership,
bounds and policy immediately, and continues resource, HTTP, pool-error and
database checks. Recovery is required, and final checks remain strict. Gap
counts and durations are included in any passing result. A persistent gap
still stops the Job.

After the original Job is terminal and health is checked, use the distinct
retry Job, `browse-users-250-autoscaling-v2`:

```bash
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py drill --retry-metric-gap
```

This preserves the original failed Job, waits up to ten minutes for natural
recovery to two ready pods before starting load, and runs the same 250-user
workload and bounds. No addon reinstall, image build or manual scaling is
needed. Its live result is recorded below.

The first retry invocation stopped at `DISCOVERY_NOT_CONVERGED` before a new
Job was created. Fresh demo and Operations checks passed; the database's
five-minute observations reached CPU 8.38%, memory 22.00% and 43 connections.
The checker now waits up to 180 seconds for readiness, target discovery and
active metrics to agree before the unchanged strict validation succeeds.
Policy, ownership and replica-budget errors still fail immediately. This
bounded read-only wait does not substitute missing targets or zero metrics.
The same retry command can be used because that invocation created no Job.

### Live v2 workload and separate recovery verified

The subsequent v2 run `a7565c1fef4e` completed 1/1. All 250 users produced
**21,086 HTTP 200 responses**, zero errors, in **182.162 seconds**. Latency
was p50 134.081 ms, p95 167.385 ms, p99 187.999 ms and maximum 371.069 ms.
The observer recorded two to three ready pods. Subsequent inspection showed
three Running, ready pods with zero restarts, availability 100 per event,
and healthy Operations with no backlog or active alerts.

The observer reported `RESOURCE_METRICS_MISSING` near completion. Code review
found a race: it checked live container resource metrics before handling the
Job's Complete condition. A completed generator can disappear from the metrics
API, and Pod Succeeded can also precede the Job condition. Both terminal cases
are now handled before expecting live generator usage. Live-pod missing or
stale samples still fail, and final workload and pool-error validation remain
required. This fixes an observer race; the original error did not identify
which pod's sample was absent, so that attribution cannot be proven from it.

The workload pass and observed scale-up are valid evidence. The original
continuous resource audit did not finish, and natural scale-down had not yet
been supplied. Do not replay the load or delete either Job. Finish with this
read-only recovery check:

```bash
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py wait-idle
```

It refuses active load Jobs, waits up to ten minutes for the HPA to return to
two ready pods, requires matching fresh targets and active metrics, then
checks application health and sampled database pressure. It creates no Job,
changes no replicas and emits `BROWSE_AUTOSCALING_RECOVERY_OK` only on success.
That result verifies post-run recovery; it does not retroactively complete
the original continuous resource audit or claim a latency SLO.

The user subsequently supplied `BROWSE_AUTOSCALING_RECOVERY_OK`. The read-only
checker observed three ready pods, then two ready pods through the existing
HPA, with no manual scaling or new load. Final pressure was zero; borrow rate
was approximately 0.20/second. Both demo events retained 100 available tickets,
Operations had no backlog or active alerts, and both workers were healthy.
The five-minute database window had five observations per metric: CPU maximum
7.73%, memory 21.56%, and 35 connections, including idle sessions and earlier
overlap. The final normal configuration is two ready Browse pods, eight
connections each, queue limit 16, and autoscaling bounds two to four pods.
The original workload's continuous resource audit remains incomplete.

Keep the Job and logs. The Job has a 450-second deadline, including resource
warmup. On a handled failure the observer attempts to stop only its own Job
using its UID; it does not disable the HPA or force a successful downscale.
On Cloud Shell disconnect, inspect the Job before any new experiment:

```bash
kubectl get job browse-users-250-autoscaling -n trafficxx
kubectl logs job/browse-users-250-autoscaling -n trafficxx -c check
kubectl get hpa trafficxx-browse -n trafficxx
python3 scripts/browse-autoscaling.py check
```

If recovery requires pausing the controller at two replicas, after the load
is terminal use:

```bash
kubectl annotate scaledobject browse -n trafficxx \
  autoscaling.keda.sh/paused-replicas="2" --overwrite
```

This is manual recovery, not a passing natural downscale. To resume later,
remove that annotation and run the checker. Keep both earlier comparison
journals; their four-connection rollback commands belong to those completed
trials and must not be used to undo this new autoscaling policy.

## 500-user slower ramp: stopped; earlier scaling comparison prepared

The 120-second arrival trial (`60787ecc1b0b`, Job
`browse-users-500-autoscaling-ramp120`) stopped after 75.85 seconds, with
315 participants and 5,715 requests: 5,714 HTTP 200 responses and one HTTP 503.
The first failure reported `DATABASE_BUSY` in 29.502 ms. At the latest
observation the controller wanted three pods, but only two were ready.
This is a failed partial run, not a 500-user pass. The fast rejection and
pending third pod support investigating earlier scaling; they do not prove
database saturation or establish the exact rejection category without counters.

Capture this run and confirm recovery first:

```bash
python3 scripts/browse-autoscaling.py diagnose-500-slow
python3 scripts/browse-autoscaling.py wait-idle
```

The subsequent comparison changed **only the pool-pressure target from
5 to 3 per pod**. The borrow-rate target stays 40. Pools remain eight
connections per pod, waiting queues remain 16, and replicas remain bounded
at two to four: a steady Browse connection cap of 32 within the total
planning budget of 128. Earlier scaling can consume extra pods sooner at
moderate traffic. Its workload and separate recovery passed as recorded below;
the complete observer audit remains unverified.

The following commands were used after diagnostics and healthy recovery.
The fixed Job now exists; inspect its logs rather than replaying these commands:

```bash
git pull --ff-only origin main
python3 scripts/browse-autoscaling.py configure-early &&
python3 scripts/browse-autoscaling.py drill --users 500 --earlier-scaling
```

Configuration refuses active load, foreign ownership, paused autoscaling or
unreviewed policy. It waits for two naturally ready pods and healthy metrics,
then uses UID/resource-version/spec tests to patch only the pressure threshold.
It waits for the native HPA targets to become 3 and 40. If reconciliation times
out, the threshold may already be 3; rerun `configure-early` to resume safely.
It creates no load, modifies no application template and requires no image build.
Read-only `check` and `wait-idle` accept either complete reviewed profile.
The new drill requires target 3; older drill modes require target 5, preventing
an accidental comparison with a different policy. The original `deploy` action
still installs target 5 and should not be used for this comparison.

The new fixed Job is `browse-users-500-autoscaling-pressure3`, preserving both
previous failed Jobs. Its 120-second arrival ramp, 180-second total workload,
two-second think time, request cap and resource/latency/error/database guards
are unchanged from the preceding trial. All 500 participants, zero request
errors, actual ready-pod scale-up and natural return to two ready pods remain
required. No automatic higher stage or capacity increase is performed.

If the comparison fails, retain its Job and capture it with
`python3 scripts/browse-autoscaling.py diagnose-500-early`; use `wait-idle`
after the load stops. A workload pass is not a complete observer audit or latency SLO.

## 500-user earlier scaling: workload and recovery passed; observer audit incomplete

Run `4940bd9ef3b8`, Job `browse-users-500-autoscaling-pressure3`, completed
with all 500 participants, 28,347 HTTP 200 responses and zero errors.
The workload lasted 182.187 seconds, with arrivals spread over 120 seconds
inside the scheduled 180-second window and a two-second think time. Latencies
were p50 131.767 ms, p95 185.227 ms, p99 226.088 ms and maximum 372.583 ms.
These are paced readers, not 500 simultaneous requests or 500 requests/second.
There were no inventory writes.

The pressure-target change from 5 to 3 was confirmed live. Other reviewed
limits stayed at pool eight, queue 16 and two-to-four pods. The observer
recorded two ready pods, a third desired pod, then three ready pods. It then
exited with `CalledProcessError`, without identifying the failing command in
the supplied output. The exact cause is not established. The load Job
continued, reached Complete 1/1 and emitted `BROWSE_SCALING_LOAD_OK`.

A separate `wait-idle` invocation first refused while that Job was active.
After completion, recovery observed four ready pods and then a natural return
to two, emitting `BROWSE_AUTOSCALING_RECOVERY_OK`. Both demo events retained
100 available tickets. Operations reported 72 processed events, zero pending
or outstanding work, no active alerts, healthy workers, and unchanged
historical publication-failure count four / dead-letter audit count one.

The recovery five-minute database window had five samples per metric:
maximum CPU 5.904%, memory 21.246% and 43 connections including idle sessions.
This recovery window does not establish peak load-period database utilization.
Earlier scaling was sufficient for this workload attempt; one passing trial
does not guarantee every arrival pattern or establish isolated causality.

**Verified:** workload success, observed scale-up, separate natural downscale
and final application health. **Incomplete:** the original continuous
resource/database/controller audit; there is no `BROWSE_AUTOSCALING_DRILL_OK`
for this run. Final recovery cannot replace that audit. Next, identify the
observer command failure, improve its safe diagnostic reporting, and prepare
a distinct repeat of the same 500-user stage before any higher user count.
Retain the existing Job and logs; do not delete it to reuse its name.

See the official [AKS managed KEDA setup](https://learn.microsoft.com/en-us/azure/aks/keda-deploy-add-on-cli),
[KEDA Prometheus scaler](https://keda.sh/docs/2.18/scalers/prometheus/), and
[Kubernetes HPA behavior](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/).
