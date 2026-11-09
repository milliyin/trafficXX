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
observer command failure and repeat the same 500-user stage with the improved
safe diagnostic reporting described below before any higher user count.
Retain the existing Job and logs; do not delete it to reuse its name.

### Observer verification for the same 500-user profile

The old output cannot identify which command failed. Source inspection found
that the top-level handler omitted the operation and exit code, and a second
error during cleanup could replace the original exception. These reporting
defects are corrected; the cause of the historical external-command failure
remains unknown. No command failures are ignored or automatically retried.

Failures now report a fixed `command_operation` label, `command_exit_code`,
`command_timed_out`, and an `observer` record containing the stage, run ID,
Job, resource-gate state and accumulated sampled resources. Command arguments,
stdout, stderr and headers are omitted. Cleanup uses the owned Job UID,
reports its own failure separately, and preserves the primary exception.
Already-observed terminal Jobs need no extra cleanup query. If stopping fails,
the Job may continue until its existing deadline; inspect it before another run.

The following commands were used for the verification. Its fixed Job now
exists; retain and inspect it rather than replaying the creation command:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py drill --users 500 --verify-early
```

This creates only `browse-users-500-autoscaling-pressure3-v2`, preserving the
completed first Job and its workload success. It requires the deployed
pressure target 3, borrow-rate target 40, pool eight, queue 16, two-to-four pod
bounds and unchanged immutable application image. It repeats the same
120-second arrival ramp inside the scheduled 180-second window with two-second
think time and a maximum of 45,000 requests. Generator resources, freshness,
HTTP/pool/database stop conditions and bounded HPA metric-gap checks remain
unchanged. No image build, scaling-policy change or higher user count is needed.

Keep Cloud Shell open through natural recovery and retain the full output.
Only `BROWSE_AUTOSCALING_DRILL_OK` from this distinct run completes the
observer verification. If it fails, inspect the new stage/operation/cleanup
fields and the preserved Job. `diagnose-500-verify` captures this v2 workload's
historical pool counters, while `wait-idle` checks recovery after it is terminal.
The live repeat failed as recorded next; the full observer verification
remains incomplete.

### 500-user verification v2 stopped on a service rejection

Run `40064bcca30f`, Job `browse-users-500-autoscaling-pressure3-v2`, stopped
after 114.629 seconds, with 476 of 500 users participating. Of 12,987 requests,
12,986 returned HTTP 200 and one returned HTTP 503 with `DATABASE_BUSY` on
the seated route. That rejection occurred at 114.258 seconds and took
22.432 ms. Truncated-run latency was p50 130.644 ms, p95 197.139 ms,
p99 261.038 ms and maximum 370.026 ms. No inventory writes were generated.

The resource gate passed. Observations progressed from two ready pods to
three, then four. The parent reported `LOAD_JOB_FAILED`, stage
`read_failed_load_logs`, and cleanup `already_terminal`. This is a workload
failure, not evidence of another external-command failure; a null command
exit code and operation `unknown` are expected for this validation error.
The preceding 28,347-request success remains valid for that individual run,
but the repeat prevents treating it as established repeatable capacity.

The observer retained four resource summaries: the two original Browse pods,
the generator, and one added Browse pod. Sampled CPU maxima were approximately
0.044 and 0.038 cores on the original pods, 0.041 on the generator and 0.144
on the added pod; sampled memory maxima were 74.578, 72.789, 50.348 and
63.711 MiB respectively. The added pod had only one source sample and the
fourth Browse pod has no resource summary in this failed output. These are
partial sampled observations, not complete peak coverage or a passing audit.

`DATABASE_BUSY` identified a pool-level service rejection; the subsequent
counters confirmed queue-full rejection as recorded below. Four ready pods do not establish equal
traffic distribution, full pool readiness at the instant of rejection or
absence of brief per-pod queue pressure. The supplied database windows include
warmup and may lag the failure; they do not prove database saturation or its
absence at that instant. No further queue, pool or replica increase has been
selected. First collect:

```bash
python3 scripts/browse-autoscaling.py diagnose-500-verify
python3 scripts/browse-autoscaling.py wait-idle
```

These read-only checks subsequently completed. No 1,000-user stage or blind
rerun should follow this failure.

### V2 queue-full diagnosis and healthy recovery; per-instance capture next

Extended counters for run `40064bcca30f` reported queue-full and borrow-error
increases of 1.072727 each, consistent with approximately one rejection after
Prometheus extrapolation. They represent the same failed borrow, not two
separate failures. Acquisition timeouts, closed-pool rejections, connection
errors, lost connections and bad returns had zero increases. All three
instances visible at termination had later source samples; the post-terminal
window included a fourth instance. Sampled pre-terminal peaks were three
healthy scrape targets, waiting four, busy/opening 21 and borrow rate
131.536063/second. These sampled maxima need not coincide.

The Kubernetes observer had recorded four ready pods while this historical
scrape window recorded at most three targets. That is a visibility discrepancy,
not proof the fourth pod received no requests. Repository configuration uses
30-second DNS target refresh and 15-second scrapes. Startup already calls
`pool.wait(timeout=30)` for a configured minimum above one before the Browse
lifespan begins serving, so this evidence does not justify removing or adding
pool warmup. No readiness, discovery, queue or replica-policy change is made.

Recovery independently confirmed two ready pods, pool eight, pressure zero,
borrow rate 0.20/second, both demo events at 100 available tickets, Operations
72 processed events, no backlog or active alerts and healthy workers.
The five-minute recovery database sample maxima were CPU 6.632%, memory
21.006% and 41 connections, including idle sessions. Recovery passed; this
does not establish load-period peaks or repair the failed workload result.

To investigate which pod rejected and how borrow activity was distributed,
run only the following read-only capture in Cloud Shell:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py diagnose-500-distribution
```

It is pinned to the existing verification-v2 Job and checks its terminal state,
run ID and UID. Alongside the existing aggregate result, `per_instance` adds
eight counter increases (including borrows), sampled waiting/busy/borrow-rate
peaks, scrape counts through termination, and last source timestamps at the
termination and post-terminal cutoffs. Instances are scrape addresses, not
necessarily current pod names. Missing per-instance fields remain `null`;
zero and missing are distinct. Counter windows include warmup and the
60-second tail; borrow counts include readiness and other traffic and are
not exact workload request counts. Sampled peaks can miss short bursts.
If historical data has expired, do not interpret missing samples as zero.

This capture creates no Job, generates no HTTP workload, changes no policy
and opens no SQL connection. The original successful 500-user workload remains
a single passing attempt; v2 remains a failed repeat. The subsequent
per-instance result and selected timing comparison follow.

### Per-instance result: rejection on an established instance

The user-supplied distribution capture attributed the queue-full increase
1.072727 to `10.244.3.249:8000`. Its sampled waiting peak was zero, showing
why periodic samples cannot rule out a short full queue. It had 19 scrapes
before termination and approximately 4,995 borrows in the extended window.
The other established instance, `10.244.2.186:8000`, had 19 scrapes,
approximately 4,914 borrows, sampled waiting four and no error increase.

The added instance `10.244.3.209:8000` had two pre-terminal scrapes and
approximately 1,140 borrows. `10.244.2.84:8000` had no pre-terminal scrape
fields and approximately 1.81 borrows in the extended window. Borrow counts
include readiness/other traffic and extrapolation; they are not an exact
allocation of the 12,987 workload requests. Missing pre-terminal scrapes
do not prove no traffic reached the fourth instance. Longer-lived instances
also naturally have more time to accumulate borrows. This supports a
scale-up timing comparison, not a proven load-balancer defect or a conclusion
that four fully established pods cannot handle the steady workload.

### Comparison: one scale-up pod per 30 seconds

The next bounded comparison changes only the scale-up policy's
`periodSeconds` from 60 to 30. It permits one added pod per 30-second policy
period when the metric recommendation calls for it; it does not guarantee
pod readiness or a successful workload. Pressure target three, borrow target
40, two-to-four replica bounds, pool eight, queue 16, scale-down stabilization
300 seconds and one-pod-per-minute scale-down remain unchanged. The steady
Browse connection cap stays 32 and the total planning budget stays 128.
Extra pods may be used sooner, while DNS/scrape/control and startup delays
still exist. No discovery, routing, SQL or image change is included.

The following commands were used after healthy recovery. The fixed Job now
exists; retain it rather than replaying these creation commands:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py configure-fast-scale-up &&
python3 scripts/browse-autoscaling.py drill --users 500 --faster-scale-up
```

Configuration checks the reviewed pressure-three profile and ownership,
waits for a naturally idle two-pod baseline, and uses UID/resource-version/
whole-spec tests to replace only the scale-up period. It then waits for
the native HPA's targets and complete behavior to reconcile. If a wait fails
after patching, the policy may already be 30; rerun configuration to resume.
It changes no application template and starts no load itself.

The new fixed Job is `browse-users-500-autoscaling-step30`. It preserves
the prior failed verification and repeats its 500 users, 120-second arrival
ramp, scheduled 180-second window, two-second think time and 45,000-request
cap. All HTTP/resource/pool/database/metric-gap stops and natural recovery
requirements remain. The drill checks the native 30-second behavior before
traffic and throughout observation. Older modes require their original
60-second behavior, preventing an accidental comparison under a new policy.
Read-only `check`/`wait-idle` accept either complete reviewed profile.
Do not run the original `deploy` or `configure-early` to configure this trial.

Keep Cloud Shell running through recovery and paste the full output. A
failure is not permission for an overlapping load or higher user count;
`diagnose-500-step30` is the read-only historical capture for this specific
Job and includes per-instance data. The live trial was interrupted by a guard
as recorded next. A full `BROWSE_AUTOSCALING_DRILL_OK` remains required before
declaring the observer verification complete.

### Step30 trial stopped by queue-waiting guard; source timing investigation

The live configuration change was confirmed: pressure three, borrow target
40, one added pod per 30 seconds, two-to-four replicas, pool eight and queue
16. Run `2bfd8b5fcf69`, Job `browse-users-500-autoscaling-step30`, released
its resource gate and observed two, three and then four ready pods. The parent
stopped with `SUSTAINED_POOL_WAITING`, stage `observe_pool_pressure`, and
cleanup `stop_requested`. This is not a command error. The supplied output
contains no final workload summary; request counts, participation, latencies
and HTTP errors cannot be inferred from the parent stop reason.

Partial sampled resource maxima were: original pods CPU 0.085937 / 0.07945
cores and memory 74.398 / 73.270 MiB; added pods CPU 0.135291 / 0.136171 and
memory 64.707 / 64.039 MiB; generator CPU 0.117058 and memory 61.914 MiB.
There were respectively five, five, three, one and four distinct samples.
The output does not complete the workload/recovery audit or establish peaks.

Source inspection found the waiting guard starts a local wall-clock timer
whenever `max(pool_waiting)` is positive and stops after ten seconds of
positive reads. Prometheus scrapes every 15 seconds, so several observer
polls can reuse one source sample. The guard therefore does not establish
ten seconds of continuously queued requests. A real Prometheus regression
fixture demonstrates polls at times 166 and 176 both reading a positive
sample timestamped 165, followed by zero at 180. This proves the timing
flaw is possible, not that this particular stop was a false alarm.

First inspect the terminal Job/logs and verify natural recovery. Once no
load is active, pull the diagnostic update and capture the existing run:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py diagnose-500-step30 &&
python3 scripts/browse-autoscaling.py wait-idle
```

The read-only capture now supports this fixed Job if it is Failed with
`DeadlineExceeded`, the run-ID label matches and its sole `check` container
has exact arguments `[run_id, "500", "120"]`. If no final workload summary
exists, it reports `BROWSE_SCALING_WORKLOAD_INTERRUPTED` with unknown HTTP
counts/errors/participation, never a manufactured pass or zero errors. Jobs
still running, mismatched stages or completed Jobs without summaries are
refused. Existing valid workload summaries retain their strict run-ID checks.

`waiting_samples_before_terminal` contains actual source timestamps and gauge
values from the final 90 seconds before Job termination, without subquery
resampling. Missing history remains missing. Samples after the stop request
may reflect recovery; terminal time is not necessarily the exact stop time.
Use these samples together with per-instance counters to assess the guard
before a further comparison. No waiting threshold, safety stop, pool, queue
or maximum replica limit has been changed in response to this interruption.
The user subsequently confirmed that the Job was Failed and recovery passed:
four pods naturally returned to two, inventory remained 100 per demo event,
Operations had no backlog or active alerts, and workers were healthy. Recovery
database sample maxima were CPU 5.808%, memory 21.474% and 44 connections
including idle sessions. The historical source-sample capture subsequently
completed, as recorded below.

The first historical capture attempt failed at `read_load_logs` with exit
code one, matching the preceding failed `kubectl logs` attempt. The cause of
log unavailability is not established. The diagnostic now permits Prometheus
capture when log retrieval exits or times out **only** for the metadata-proven
deadline-stopped step30 Job described above. It reports
`workload_log_capture.available: false` and the safe operation/exit/timeout
fields. Workload status remains interrupted with unknown HTTP counts/errors;
unavailable logs never turn into a workload pass. Completed Jobs or other
diagnostic modes retain their strict log requirements. No load or guard change
was needed to retry this read-only capture after pulling the update.

### Step30 capture: no recorded pool errors; waiting measurement corrected

The user supplied the historical capture for `2bfd8b5fcf69`. All seven
pool-error counter increases were zero, with post-terminal source samples from
all four instances. Sampled aggregate peaks were four healthy targets,
waiting four, busy/opening 25 and borrow rate 153.044444/second. This does
not establish zero HTTP errors: the workload summary and logs remain
unavailable, and HTTP counts/participation/latencies remain unknown.

Original samples on `10.244.3.249:8000` were waiting two at 17:59:19.909 UTC,
then zero at 17:59:34.909 and 17:59:49.909. On `10.244.3.57:8000`, samples
were zero through 17:59:29.984, then waiting four at 17:59:44.984. The two
other instances had only zeros in their captured tails. The final positive
sample has no later pre-terminal sample; that is not proof of immediate
recovery. Neither instance has distinct positive samples establishing a
sustained per-instance wait in this window. The observation is consistent
with the guard's sampling flaw, not proof that the entire intended workload
would have passed or that no actual queue persisted between scrapes.

The corrected `PoolWaitingWatch` reads actual 45-second range-vector source
samples rather than repeatedly timing an instant maximum using the observer's
wall clock. It tracks each instance separately, ignores already-processed
source timestamps, resets a hold on zero, and stops if distinct positive
samples on the same instance span at least ten seconds. With 15-second
scrapes, two successive positive samples normally suffice. This is evidence
of sampled persistence, not continuous observation between samples.

Missing/stale/invalid/changed samples fail explicitly; they are never replaced
with zeros. Loss of a previously observed instance fails, as does a source
gap over 30 seconds after a positive sample. Pre-workload-gate history cannot
start a new workload hold. Reports include `pool_waiting_observations` with
distinct sample counts, source timestamps, last values and positive-hold start
times. The ten-second threshold and every HTTP, pool-error, resource,
database, freshness and HPA metric-gap stop remain; the measurement changes.

### Source-sample guard verification

The verification changed no capacity or arrival setting. It used the
currently deployed pressure-three, borrow-40, one-pod-per-30-second scale-up
profile, pool eight, queue 16 and two-to-four replicas. The following commands
were used in Cloud Shell; the fixed Job now exists and should be retained:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py drill --users 500 --verify-waiting-samples
```

The fixed new Job is `browse-users-500-autoscaling-sourcewait`; the interrupted
step30 Job and all prior evidence remain. It repeats 500 paced users,
120-second arrivals inside the scheduled 180-second workload, two-second
think time, 45,000-request cap and existing generator limits. No image rebuild
or scaling reconfiguration is needed. Keep Cloud Shell open through natural
recovery and retain the full output. Only a complete
`BROWSE_AUTOSCALING_DRILL_OK` verifies this run; that result was supplied as
recorded below. Do not delete or replay this Job to create another experiment.
`diagnose-500-sourcewait` is its read-only historical capture, with the same
strict metadata guards for a deadline-interrupted Job lacking logs. No higher
user count follows automatically.

## 27 October: full 500-user observer verification passed

Run `b2b01a0f2145`, Job `browse-users-500-autoscaling-sourcewait`, emitted
both `BROWSE_SCALING_LOAD_OK` and `BROWSE_AUTOSCALING_DRILL_OK`. All 500
participants completed 28,418 HTTP 200 responses with zero errors and no
first failure. Elapsed workload time was 182.186 seconds. Latencies were:

| Measure | Milliseconds |
| --- | ---: |
| p50 | 127.132 |
| p95 | 171.568 |
| p99 | 203.318 |
| Maximum | 288.168 |

This was the existing in-cluster Browse Service read workload: a scheduled
180-second window, 120-second arrival ramp, two-second think time and a
45,000-request cap. All 500 paced users were therefore present for roughly
the final minute, not three minutes of 500 simultaneous HTTP requests. It
generated no reservation writes and did not test an external ingress path,
booking-write capacity or a production latency SLO.

One observer verified a two-pod baseline, real automatic scale-up through
three to four ready pods, and natural return to two ready pods. No manual
scaling was used. The profile stayed at pressure target three, borrow-rate
target 40, one added pod per 30-second policy period, pool eight, queue 16,
and two-to-four replicas. The steady Browse connection cap remained 32,
within the total planning budget of 128. HPA metric-gap count, total duration
and maximum duration were all zero.

The sampled resource summaries covered the generator and all four serving
pods observed during scale-up:

| Process / pod suffix | Distinct samples | Sampled max CPU cores | Sampled max memory MiB |
| --- | ---: | ---: | ---: |
| Original Browse pblw4 | 6 | 0.117695 | 74.816 |
| Original Browse z9wkl | 6 | 0.108626 | 73.848 |
| Added Browse 6vr8c | 3 | 0.123786 | 65.215 |
| Added Browse bf5gq | 2 | 0.151891 | 64.918 |
| Generator zpzrb | 5 | 0.193423 | 70.629 |

CPU readings are collector-window averages and maxima are sampled. This
bounded observer check does not establish continuous peaks. The source-time
waiting watch processed 12, 12, six and four distinct samples across the
four instances. Sampled waiting maxima were two, zero, one and one; every
last value was zero, with no remaining positive hold. Brief queueing was
observed and resolved without being mistaken for repeated fresh evidence.
The sustained-wait and pool-error guards did not trigger.

The final database observation window, 18:25:19 to 18:37:56 UTC, had 12
minute-level samples per metric. Maxima were CPU 12.468%, memory 21.362%
and 43 connections including idle sessions. It includes generator warmup,
load and recovery; aggregation and publication lag can miss brief pressure.
These observations do not establish database saturation or a hard capacity
limit. The final five-minute recovery window separately had CPU maximum
5.675%, memory 21.362% and 43 connections.

Final health checks passed: both demo events retained 100 available tickets;
Operations still reported 72 processed events, no pending/outstanding work,
no active alerts and healthy workers. Historical publication failures remained
four and the retained dead-letter audit count remained one. No external
notification delivery was configured or tested.

**This completes the bounded 500-user observer verification for the tested
profile.** Earlier queue-full failures, the original successful workload with
an interrupted observer, and the step30 guard interruption remain distinct
historical outcomes. This successful run does not retroactively complete their
audits, prove the exact cause of the original command failure, or isolate the
effect of each change. Longer runs and repeated trials remain future work
before a sustained-capacity or higher-user-count claim. No new load, larger
budget or 1,000-user stage was prepared as part of this verification.

## Next stage: 1,000 paced Browse users

The operator has authorized this next stage after the full 500-user verification.
It is prepared, not yet run. Run only from Azure Cloud Shell and keep the shell
open through resource observation and natural recovery:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/check-browse-http.sh --demo &&
bash scripts/check-operations-http.sh &&
python3 scripts/browse-autoscaling.py check &&
python3 scripts/browse-autoscaling.py drill --users 1000
```

The new preserved Job is `browse-users-1000-autoscaling`. Existing Jobs are
not deleted or reused; any active read load blocks the run. This is the same
180-second workload, 120-second arrival ramp and two-second pause after each
response as the verified 500-user profile, with at most 90,000 GETs. Every
participant must make a request; all 1,000 arrive only for roughly the last
minute. It is not 1,000 simultaneous requests or 1,000 requests per second.

The helper requires the existing pressure target three, borrow-rate target
40 and one-pod-per-30-second scale-up policy. It does not configure the policy
or increase capacity: two-to-four replicas, eight connections per pod, queue
16, steady Browse cap 32 and total planning budget 128 remain unchanged.
The generator retains one CPU and 256 MiB; its ability to generate this stage
is also under test. A generator/resource failure is not a server capacity verdict.

The existing fresh-source metrics gate, sustained resource/queue guards,
first HTTP error stop, pool-error checks, deployment identity checks and
owned-Job cleanup remain active. Success requires the completed workload,
observed automatic scale-up, natural return to two replicas, final health and
database evidence under the same observer. Budget for warmup and recovery as
well as load (approximately 15 minutes). Do not start overlapping loads.

If interrupted or failed, preserve the Job and paste the full runner output:

```bash
kubectl get job browse-users-1000-autoscaling -n trafficxx
kubectl get pods -n trafficxx -l job-name=browse-users-1000-autoscaling
kubectl describe hpa trafficxx-browse -n trafficxx
python3 scripts/browse-autoscaling.py wait-idle
```

No application image rebuild is needed: the helper mounts the reviewed probe
into a Job using the current immutable Browse image. This tests internal Browse
availability reads, not ClickClash clicks, booking writes, public ingress or a
production latency SLO. Since ClickClash now shares infrastructure, this stage
can contend with live users; its existing conservative stops remain in effect.
No higher stage follows automatically.

### 29 October: first 1,000-user attempt stopped on HTTP 503

Run `d22e0d48a388`, Job `browse-users-1000-autoscaling`, released its
resource gate and stopped after 35.1 seconds. Only 289 users participated:
2,478 requests returned HTTP 200 and one returned HTTP 503 `DATABASE_BUSY`.
The first rejection was a seated-availability read at 34.69 seconds, taking
18.118 ms. The controller had reached three ready pods. This is a failed
partial trial, not a 1,000-user pass or proof of a fixed user-count limit.

The last supplied pool samples showed zero waiting, but coarse samples can
miss a short burst. The response alone does not distinguish queue-full from
acquisition timeout. Minute-level database observations are also not proof
of the database state at that instant. Capture the preserved run and then
verify natural recovery, without starting another load:

```bash
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py diagnose-1000 &&
python3 scripts/browse-autoscaling.py wait-idle
```

The diagnostic pins the Job name, run label, 1,000-user arguments and
120-second ramp. It captures counter increases through 60 seconds after
termination (to include subsequent scrapes), peak samples ending at
termination, per-instance distribution and stored waiting source samples.
Missing values remain unknown. A diagnostic success means capture succeeded,
not that the load test passed. No configuration or connection-budget change
is proposed until this evidence and post-run health are reviewed.

The subsequent capture confirmed `queue_full_total` and `borrow_errors_total`
increased by approximately 1.043 on `10.244.3.131:8000`; acquire timeouts,
closed rejections, connection errors, lost connections and bad returns showed
zero increase. Prometheus extrapolates increases, so the fractional result is
consistent with the one observed HTTP 503, not a fractional request count.
Both original instances had post-terminal scrape coverage. Stored waiting
samples were all zero, demonstrating that short queue bursts can escape
15-second snapshots while cumulative rejection counters retain evidence.

The third address had no pre-terminal scrape evidence and only about five
estimated borrows in the extended counter window. Deployment readiness reached
three, but this does not prove when the added pod joined Service endpoints or
how requests were routed during the burst. Readiness, discovery and workload
distribution are different observations. The evidence establishes a local
queue-full rejection; it does not establish database saturation or the exact
reason for the burst. The 1,000-user arrival rate was twice the verified
500-user ramp's arrival rate because both used a 120-second ramp.

Natural recovery subsequently passed: two ready Browse replicas, pool eight,
zero current pressure, both demo availabilities 100, healthy Operations with
no backlog or active alerts. The last five-minute database recovery maxima
were CPU 6.388%, memory 21.217% and 37 active connections including idle
sessions. This is recovery evidence, not a replacement for the failed load
audit. Preserve this Job; do not increase the queue, database connections or
pod ceiling solely to hide the observed rejection. Review scale-up timing
and readiness/distribution evidence before choosing a distinct comparison.

### Authorized five-minute comparison

The operator requested a five-minute 1,000-user run. This explicitly changes
the scheduled workload window to 300 seconds, keeping the same 120-second
arrival ramp and two-second pauses. Thus all users participate for roughly
the final three minutes, not five minutes at full population. The new preserved
Job is `browse-users-1000-autoscaling-5m`; the failed first Job remains intact.
The maximum is 150,000 GETs, not a target request count. The generator deadline
is 570 seconds and parent load-observation deadline 600 seconds to include
resource warmup; natural recovery remains separately bounded at 600 seconds.

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/check-browse-http.sh --demo &&
bash scripts/check-operations-http.sh &&
python3 scripts/browse-autoscaling.py check &&
python3 scripts/browse-autoscaling.py drill --users 1000 --seconds 300
```

Keep Cloud Shell open through the final result, allowing about 20 minutes
including warmup and recovery. Only the explicit 1,000-user drill supports
300 seconds. No application rebuild or autoscaling configuration change is
needed. Pod, queue, database connection and generator resource limits remain
unchanged. The first-error and sustained-pressure stops remain in force:
requesting a longer run does not fix the preceding queue rejection or
guarantee that the workload reaches five minutes.

If the run fails, do not repeat it or delete its evidence. Capture and recover:

```bash
python3 scripts/browse-autoscaling.py diagnose-1000-5m &&
python3 scripts/browse-autoscaling.py wait-idle
```

The diagnostic pins the new Job and exact `[run_id, "1000", "120", "300"]`
arguments. A successful five-minute workload summary must prove all users,
the full duration, correct pacing/bounds and zero HTTP errors. The complete
drill additionally requires resource observation, real scale-up and natural
recovery. This comparison has been prepared but has not yet run.

The first live five-minute attempt, run `f116265fb4b7`, was subsequently
stopped by `SUSTAINED_POOL_WAITING`. At `10.244.3.206:8000` the observer
recorded positive waiting at 12:47:12.664 and 12:47:27.664 UTC, 15 seconds
apart; the last count was one and sampled maximum two. This meets the existing
source-sample guard, but does not prove continuous queue occupancy between
scrapes. Deployment observation reached three ready replicas. Cleanup
requested a stop of the owned Job. No final workload summary, HTTP counts,
participation count or post-run recovery result was supplied; the five-minute
workload is not a pass.

Use the read-only `diagnose-1000-5m` and `wait-idle` commands above after
pulling the update. The diagnostic now permits a missing final summary only
when exact Job name/run arguments and a Failed/DeadlineExceeded condition
independently establish the reviewed interrupted stage. HTTP counts, errors
and participation remain unknown in that case. The original three-minute
1,000-user Job does not qualify for this fallback. A still-active Job blocks
the diagnostic and recovery checks; do not start a second load.

After the shell reconnect, historical capture for this same run succeeded.
The preserved Job interval was 12:44:40 to 12:47:32 UTC, including warmup,
not five minutes of traffic. All three instances at termination had subsequent
scrape coverage. Counter increases showed queue-full and borrow-error activity
of approximately 1.165 on the newly observed `10.244.2.188:8000` instance;
acquisition timeouts, closed rejections, connection errors, lost connections
and bad returns showed zero increase. This is extrapolated counter evidence
of a queue-full rejection, not an exact HTTP response count. Positive waiting
source samples that stopped the observer were on a different instance,
`10.244.3.206:8000`; do not conflate those two observations or infer a routing
fault. The added instance had only one pre-terminal scrape, insufficient to
reconstruct its warmup or moment-by-moment routing.

Final workload logs were unavailable, and exact deadline-stop metadata was
used to classify the workload as interrupted. Request totals, HTTP error
counts, participant count and latency remain unknown. The disconnect did not
turn this into a completed five-minute workload. Natural recovery passed:
two ready Browse pods, zero current pressure, both event availabilities 100,
healthy workers and no backlog or active Operations alerts. Recovery-window
database maxima were CPU 5.950%, memory 21.217% and 38 connections including
idle sessions; they do not describe the exact rejection instant. Preserve
this failed run and its first three-minute predecessor for future comparisons.

### Next controlled comparison: four warm pods before traffic

The operator authorized the next attempt to complete the five-minute window.
`drill-prewarmed` separates running capacity from the two-to-four scale-up
transition. It keeps eight connections per pod, queue 16, maximum four pods,
the 32-connection steady Browse cap and total planning budget 128. It
temporarily changes only KEDA's minimum from two to four, with an ownership
annotation and compare-and-swap tests against the exact live object. The HPA
still owns replica changes; no direct Deployment scale command is used.

Before creating the load Job, the helper requires four ready pods, four
healthy Prometheus targets, the same four ready/nonterminating Service
EndpointSlice addresses, zero pool pressure, and eight available connections
per pod. This proves sampled readiness and endpoint eligibility, not balanced
per-request routing. It then runs the same 1,000 users, 300-second total
window, 120-second arrival ramp, two-second think time and 150,000-GET cap.
The new preserved Job is `browse-users-1000-prewarmed-5m`.

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py drill-prewarmed
```

The helper includes health, database pressure, active-load and policy checks.
Keep Cloud Shell open (allow about 25 minutes including warmup/recovery).
The same first-error, sustained queueing, resource and identity stops apply.
A longer duration or prewarming is not a guaranteed pass. No new application
build is needed. Shared public ClickClash may contend for this infrastructure.

`BROWSE_PREWARMED_CAPACITY_OK` means the workload and observer completed with
four held pods. It does not claim automatic scale-up. The wrapper restores
the minimum to two and waits for natural recovery; only then can it emit
`BROWSE_PREWARMED_DRILL_OK`. It attempts restoration on failure too, but
refuses to restore while a read load remains active or when policy/ownership
has changed. If restoration fails, the temporary four-pod minimum may persist.

After a disconnect, inspect the Job first and wait for it to become terminal:

```bash
kubectl get job browse-users-1000-prewarmed-5m -n trafficxx
```

Then capture the specific run and restore the marked minimum:

```bash
python3 scripts/browse-autoscaling.py diagnose-1000-prewarmed
python3 scripts/browse-autoscaling.py restore-prewarm
```

Run restoration even if diagnostic capture fails. The annotation survives
Cloud Shell loss, so restoration does not rely on a missing local journal.
The restore action validates the reviewed policy, marker and controller
ownership, changes the minimum only, and preserves Jobs, images and data.
If it refuses, share the error instead of manually deleting the annotation
or editing replica counts. Do not run normal `wait-idle` while the marked
minimum remains four: it expects the usual two-pod policy.

The first live prewarmed attempt was run `b5134614b92f` (temporary policy
marker `e52143bf0265`). The helper verified four ready Service endpoints and
eight idle connections per pod before traffic. All four remained ready during
the observed load. The observer stopped on `SUSTAINED_POOL_WAITING`: instance
`10.244.2.50:8000` had waiting count one at 13:07:26.110 and
13:07:41.110 UTC, 15 seconds apart. Another instance showed one waiting
request in its latest sample. No final workload summary was supplied, so
request totals, participation, HTTP errors and full latency are unknown.

This did not complete five minutes. Importantly, the queue guard triggers on
any positive waiting in successive samples; a count of one out of a configured
16-slot queue is not by itself proof of overload, continuous waiting or poor
HTTP latency. Queueing is expected when borrowing a finite connection pool.
Prewarming did not avoid this stop, so scale-up delay alone cannot explain
all outcomes. Capture `diagnose-1000-prewarmed` before deciding whether there
were actual rejected requests and whether a separately specified pressure
criterion is appropriate. Do not silently relabel this stopped trial a pass.

Restoration succeeded: the minimum returned to two, two Browse replicas were
ready, pool pressure was zero, both event availabilities were 100, and
Operations remained healthy with no backlog or active alerts. No additional
restore is needed for this reported run. The preserved Job remains available
for historical counter and source-sample capture; do not repeat its name.

The later diagnostic recovered the final workload summary for that same run:
598 users participated, with 10,328 HTTP 200 responses and one HTTP 503
`DATABASE_BUSY` among 10,329 attempts. The first rejection was at 71.785
seconds (11.822 ms); elapsed workload time was 72.171 seconds. Reported
latencies for this truncated mixed-result run were p50 139.508 ms,
p95 196.869 ms, p99 244.122 ms and maximum 416.540 ms. This supersedes the
earlier statement that those workload fields were unavailable.

Queue-full and borrow-error counters increased approximately 1.040 on
`10.244.3.206:8000`, with zero increase for timeouts, closed rejections,
connection errors, lost connections and bad returns. All four instances had
post-terminal coverage. Estimated borrow increases were approximately
2,771, 2,767, 2,814 and 2,722 (including background traffic and counter tail),
which do not suggest a large sustained distribution imbalance; they cannot
exclude brief uneven routing. Sampled busy/opening connections peaked at
29 of the 32-slot cap. Pool snapshots can miss the brief burst that fills a
16-slot local queue.

This confirms a real rejection in addition to the observer's conservative
waiting stop. Relaxing that waiting guard alone would not produce a zero-error
pass. Four ready, warm pods did not suffice for this tested arrival profile;
that does not establish a fixed 598-user capacity limit or database saturation.
Investigate connection holding time before another trial. Current Browse
code executes a single availability statement but uses the shared transactional
pool context and checkout liveness check. Earlier timings and the cross-region
deployment suggest network/transaction round trips are worth measuring; they
do not yet prove which optimization would complete 1,000 users. Any read-only
transaction optimization must preserve SQL permissions, error handling and
connection checks, and leave Booking write transactions unchanged.

### Browse single-statement read candidate

The operator authorized work on connection holding time after that diagnosis.
Browse now creates autocommit-capable pooled connections and uses a dedicated
`read_connection()` path for its fixed single-statement event list,
availability query and readiness SELECT. This avoids an explicit BEGIN/COMMIT
around each such read. PostgreSQL still executes each statement atomically;
the availability response continues to come from one SQL statement. This is
not a cache and does not change the availability predicate or SQL grants.

The existing `connection()` API still opens an explicit transaction for
multi-statement callers, preserving rollback behavior in pool diagnostics.
Booking continues to use its original transaction-based pool. Physical
connection checkout checks, token resolution on each new connection,
pool/queue sizes, timeouts, TLS settings and error responses remain enabled.
`single_statement_autocommit: 1` appears in Browse's allowlisted numeric
statistics. The timing checker reports `read_mode` and rejects mixed modes.

The candidate passed local PostgreSQL reuse/error-recovery/rollback tests,
including Booking tests. These functional checks do not establish a latency
improvement or a 1,000-user pass. First deploy and measure from Cloud Shell:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-browse-image.sh &&
python3 scripts/browse-autoscaling.py deploy-image &&
bash scripts/check-browse-http.sh --demo &&
python3 scripts/check-browse-timing.py --pool-size 8
```

`deploy-image` requires the usual two-pod baseline and reviewed KEDA policy,
blocks active read loads, validates the digest, and compare-and-swaps only
the Deployment image. It preserves the running pool/replica policy instead
of applying the old fixed-two-pod manifest. Do not use `deploy-browse.sh`
while Browse has an HPA. No Booking or ClickClash image is updated.

Review both replicas' acquire/body/return deltas against earlier timings
before any distinct new load. Sequential port-forward timing includes client
and network overhead and is not a production SLO. The previous failed Jobs
remain intact; do not reuse their names. A seven-minute profile with a
two-minute ramp up, three-minute hold and two-minute ramp down remains a
separate future experiment, not part of this image/timing check.

See Psycopg's [transaction behavior](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
and [pool transaction contexts](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).

The operator deployed the candidate image
`sha256:9080ac3d8b0eb80680c2ad4f395679909b2509efd3c55614834acc28ff34db5c`.
Rollout, two-pod KEDA baseline and Browse/Operations health checks passed.
The initial timing invocation failed `Unexpected pool bounds`: the timing
script and its shared snapshot validator still expected the historical
four-connection pool. This was a checker incompatibility, not a measured
application failure or performance verdict. The checker now accepts explicit
`--pool-size 8` and requires that exact warm policy on both replicas before
and after reads; its default remains four for old comparisons. Mixed sizes,
drift, oversize pools and invalid counters still fail closed.

No second image build or deployment is needed for this checker-only correction:

```bash
git pull --ff-only origin main &&
python3 scripts/check-browse-timing.py --pool-size 8
```

The corrected live timing check passed on both pods using that image, pool
eight and `read_mode: single-statement-autocommit`. Reported acquire/body/return
means were 27.469/28.646/0.038 ms and 26.008/26.771/0.034 ms. The sums of
these phase means were approximately 56 and 53 ms, compared with approximately
118 and 103 ms in the preceding transactional single-query baseline. This is
encouraging sequential diagnostic evidence, not a controlled causal estimate,
exact connection holding time or a load-capacity result. Observation counts
can include readiness traffic. Port-forward HTTP means remained 416.880 and
381.210 ms, so these samples do not show a comparable end-to-end speedup.

### Explicit seven-minute user profile after the read optimization

The operator's requested profile is now available as a distinct comparison:

| Phase | Seconds from workload start | Paced users |
| --- | --- | --- |
| Ramp up | 0–120 | 0 to 1,000 |
| Hold | 120–300 | 1,000 |
| Ramp down | 300–420 | 1,000 to zero |

Each user's arrival and departure are scheduled relative to the same workload
clock. No new request starts after that user's departure; an already running
request may finish. Every user keeps the two-second think time after each
response. Active user count includes users thinking between requests; it is
not concurrent HTTP requests or requests per second. The 210,000-attempt cap
is an upper bound, not a throughput target. Progress is sampled around every
five seconds while requests complete, plus a final zero-active sample.
Final output includes per-phase request counts, status counts and latency.

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py drill-user-profile
```

The helper prewarms and holds four pods using the same marked minimum and
restoration safeguards as the previous capacity experiment. This isolates
the deployed read candidate with ready capacity; it does not test scale-up
from two. The new preserved Job is `browse-users-1000-autocommit-profile`.
All pool/queue/connection/generator budgets and first-error, request-latency,
source-waiting and resource stops remain unchanged. A profile pass requires
all participants, full 420-second duration, zero errors, a verified 1,000-user
hold and observed gradual withdrawal to zero. A stop remains a failed trial.

No image rebuild is required for the profile: the current application image
is used and the new probe is mounted as a ConfigMap. Keep Cloud Shell open
through marked-minimum restoration and recovery, allowing about 30 minutes
including warmup/load/recovery. The Job deadline is 690 seconds and parent
load observation deadline 720 seconds; recovery is separately bounded.

After a disconnect, inspect this exact Job and wait until terminal:

```bash
kubectl get job browse-users-1000-autocommit-profile -n trafficxx
```

Then capture and restore, running restoration even if capture fails:

```bash
python3 scripts/browse-autoscaling.py diagnose-user-profile
python3 scripts/browse-autoscaling.py restore-prewarm
```

This seven-minute workload has been prepared, not yet run on Azure. Historical
three- and five-minute failures remain unchanged. A successful new run would
prove only this bounded profile and would not retroactively complete them.

The first operator invocation of `drill-user-profile` failed during setup,
before the warm-pod and load-start markers. Restoration reported the normal
two-pod baseline and healthy Browse/Operations. The error was an exit-one
Kubernetes operation, without stderr captured by the original command wrapper.
This is not a failed 1,000-user workload: no workload result was produced.
The exact API rejection has not yet been established. Do not assume resource
exhaustion or weaken policy guards based on that generic exception.

Inspect the setup patch without persisting it or sending traffic:

```bash
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py preflight-prewarm
```

This validates the exact guarded minimum-four patch using server-side dry run,
checks that the live specification/marker remain unchanged, and emits either
`BROWSE_PREWARM_PREFLIGHT_OK` or a fixed error category. It does not start a
Job, change replicas or print raw API errors/object content. The diagnostic
distinguishes snapshot conflicts, authorization, admission, invalid patch and
API reachability problems where the server message permits classification.
Share its result before repeating the load command. No new image build is
needed for the diagnostic.

On 3 November the operator's server-side preflight passed with live minimum
two and projected minimum four, but a subsequent real invocation again failed
at `update_autoscaling_policy` before load started. Restoration passed with
two ready pods and healthy Browse/Operations. The live patch previously used
a generic wrapper that discarded stderr, unlike the dry run; therefore the
successful preflight did not identify the later real rejection's cause.

Both dry-run and live patches now use captured command results and fixed,
redacted error categories. A live snapshot-conflict rejection may be retried
at most three times after refetching the resource version. Every retry
requires the same resource UID, full policy specification and annotations as
the first attempt, as well as the controller/ownership guards. Operator edits,
foreign markers, authorization/admission failures, transport errors and
unclassified rejections are not silently retried or overridden. Returned
object identity, desired minimum and preserved policy are verified after
success. Setup failures now carry an explicit setup stage and load-start flag.

Since this reported failure occurred before Job creation and restoration
passed, the operator can pull this helper-only correction and invoke the same
distinct profile command again:

```bash
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py drill-user-profile
```

The existing-Job and active-load guards still refuse any overlapping or
previously created run. No rebuild is needed. A future error category is
evidence to investigate, not permission to remove compare-and-swap guards.
No successful seven-minute workload has been supplied yet.

The next supplied invocation did pass setup. Temporary policy marker
`abbaf5d5fd24` held four ready, warm pods and four ready endpoints. Workload
run `1a46e8803ef4` used the preserved Job
`browse-users-1000-autocommit-profile` and released its resource gate. All
four pods remained ready during the observed traffic, but the observer
stopped at `observe_pool_pressure` / `SUSTAINED_POOL_WAITING`.

Instance `10.244.3.255:8000` had positive waiting samples from
16:49:22.779 to 16:49:37.779 UTC on 3 November, with last value three and
sampled maximum five. Another instance had 11 waiting requests in its latest
sample, against the configured per-pod limit of 16. The remaining instances'
latest counts were four and one. These source timestamps differ; do not sum
them into an instantaneous total, and do not infer continuous waiting between
scrapes. No final workload summary was included in the supplied output, so
HTTP counts, participation, latency, phase completion and error counts remain
unknown pending historical capture.

The wrapper requested the owned Job stop and successfully restored minimum
two and two ready pods. Browse retained availability 100 for both events;
Operations remained healthy with no backlog or active alerts. This confirms
recovery, not a completed user-profile pass. Sampled CPU/memory and the
database five-minute windows do not establish instantaneous saturation.

Capture the exact stopped run without starting more traffic:

```bash
python3 scripts/browse-autoscaling.py diagnose-user-profile
```

No additional restore is required for this supplied result. Preserve the Job
and review its HTTP/rejection counters before another configuration change or
trial. The earlier sequential timing improvement remains valid within its
measurement scope; it does not imply this traffic profile must pass.

Historical capture for run `1a46e8803ef4` subsequently confirmed a queue-full
and borrow-error counter increase of approximately 1.0134 on
`10.244.3.255:8000`. All four original instances had post-terminal sample
coverage. The fractional value is Prometheus extrapolation, not an exact
HTTP error count. Acquisition timeouts, closed rejections, connection errors,
lost connections and bad returns showed no increases in this window.

Estimated borrow increases were similar across the four instances
(approximately 7,762 to 7,831, including background traffic and the counter
tail). This does not establish sustained routing imbalance; brief bursts
remain possible. Sampled aggregate waiting peaked at 16 across pods, which
must not be confused with a single pod's configured 16-slot queue limit.

Final workload logs could not be captured. The diagnostic correctly reports
`BROWSE_SCALING_WORKLOAD_INTERRUPTED`: HTTP totals, error counts, participant
counts, latency and phase completion remain unknown. A successful diagnostic
capture is not a successful seven-minute workload. The rejection confirms
actual local pool pressure in addition to the observer waiting stop; it does
not establish database CPU saturation. Recovery was already verified above.
The last verified capacity pass remains the 500-user run.

Before another trial, measure acquisition and query/body timings under load
to distinguish time spent checking out a connection from time spent holding
it for the query. The sequential baseline still includes a checkout health
check on every borrow; its cost is a candidate to investigate, not a proven
cause or permission to remove connection validation. Preserve this Job and
use a distinct reviewed comparison rather than repeating its fixed name or
loosening the failure guards.

### Combined measurements in one command

#### One-second recent-check comparison

The recovered instrumentation run `c4c8cc28797e` reached 1,000 paced users,
then failed at 135.24 seconds with two `DATABASE_BUSY` responses (36,761
HTTP 200 out of 36,763 attempts). It did not complete the hold or withdrawal.
Per-instance health-check means were 26.12–28.24 ms versus query means of
26.29–28.44 ms. These are overlapping phase measurements over the historical
window, including background reads, not additive independent HTTP timings.
Database minute-level CPU maximum was 10.51%, with 44 connections including
idle sessions. Queue-full counter increases were concentrated on one pod;
the exact two HTTP errors come from the workload summary, not extrapolation.
Restoration to two healthy pods and zero Operations backlog was confirmed.

Prepare and run the next distinct comparison with:

```bash
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py recent-check-workflow
```

This reuses the build/image-only deployment/baseline verification/seven-minute
load/restoration/report workflow, with a new preserved Job
`browse-users-1000-recentcheck-profile`. Its image explicitly sets
`BROWSE_DB_CHECK_INTERVAL_SECONDS=1` via a bounded build option. Ordinary
Browse builds and the application default remain zero (check every borrow).
Booking ignores this Browse-only setting and keeps its original callback.
Both serving pods must report interval one before load starts; deployment
environment overrides or stale images fail that check.

A successful driver's check may be reused on the same open, unbroken
connection for less than one second. A new connection, expired validation,
closed/broken connection or clock anomaly forces the original check. Only
successful checks establish a timestamp. Application/SQL errors invalidate
cached validation. Weak per-connection records disappear when a connection
is collected. Physical connection creation still resolves fresh credentials
and retains TLS validation. No application statement or transaction is retried.

This trades per-borrow liveness checking for bounded recent validation. A
connection dropped remotely inside the window can fail its next read; pool
replacement remains enabled and verified by a dedicated PostgreSQL test.
It is not a guarantee of zero errors during database failures. Keep the same
error, queue, request-latency and resource stop conditions for the comparison.

The report includes performed check counts, skipped checks and observed check
interval bounds. A sequential sample may perform no new checks; its mean is
unknown (`null`), not zero milliseconds. Under-load evidence determines
whether this change improves capacity. Pods remain eight connections each,
four during the trial and two after restoration; the candidate image remains
deployed. To return to every-borrow checking after the Job is terminal, use
the ordinary build followed by the image-only deployment:

```bash
bash scripts/build-browse-image.sh &&
python3 scripts/browse-autoscaling.py deploy-image
```

If the shell disconnects, inspect the new Job until terminal, then run
`restore-prewarm` followed by `measure-recentcheck-profile`. Preserve the Job;
the workflow refuses to reuse it. No new 1,000-user pass has been established.

The first recent-check trial (`8b8a327ad5db`, 8 November UTC) was interrupted
while the observer was at `observe_database_metrics`. Its exact original
exception was masked by a later restoration refusal while the stopped Job
was still becoming terminal. Stored counters with post-terminal coverage for
all four instances showed no pool rejection, timeout, lost-connection or bad
return increases. Acquisition means were 2.92–3.55 ms, versus 28.70–33.08 ms
in the earlier every-borrow instrumentation run. Query means remained
24.88–28.68 ms. Roughly 89% of checks reused recent validation, based on
extrapolated counter increases. These are promising observations from different
windows, not proof of a completed profile or an isolated causal capacity gain.

Final workload logs were unavailable, so HTTP totals/errors, participant
counts, latency and phase completion remain unknown. Manual owned restoration
subsequently confirmed two healthy pods and no Operations backlog or active
alerts. The original observer audit recorded the stage but not its error;
that exact cause cannot be reconstructed from the supplied output.

The wrapper now preserves the original exception if restoration also fails,
and saves fixed, redacted primary-failure details in the observer audit. After
requesting a stop, it waits up to 60 seconds for that exact Job UID and run
label to become terminal before attempting restoration. It never waits for,
stops or adopts another Job. Failure/metric guards remain unchanged.

Repeat the same comparison against the already deployed interval-one image:

```bash
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py verify-recent-check-profile
```

This verifies both serving pods' current read/check policy, creates the distinct
`browse-users-1000-recentcheck-profile-v2`, runs the same bounded profile,
restores the temporary minimum and captures the report. It performs no build
or image rollout. The original trial stays preserved. After a disconnect,
inspect the v2 Job until terminal, then run `restore-prewarm` and
`measure-recentcheck-verify`. Any new monitoring failure should expose its
original safe error category rather than being replaced by a recovery error.

The v2 repeat (`68f1360bbe72`, 8 November UTC) produced a complete failure
summary: all 1,000 users participated, with 59,560 HTTP 200 responses and one
`DATABASE_BUSY` / HTTP 503 out of 59,561 attempts. The first error occurred at
181.308 seconds; total elapsed time was 181.491 seconds. Thus it completed
the two-minute arrival ramp and about 61 seconds of the planned 180-second
hold, but none of the scheduled withdrawal. The final zero-active-users
sample reflects stopping after the error, not a completed ramp-down.

Workload p50/p95/p99 were 48.989/92.885/115.909 ms, maximum 186.651 ms.
All four targets had post-terminal coverage. Queue-full and borrow-error
increases of approximately 1.027123 were confined to `10.244.3.161:8000`;
the other error categories did not increase. The fractional increase is a
Prometheus estimate; the exact single HTTP error is from the workload log.
Estimated per-instance borrow totals were broadly similar (15,172–15,657,
including background traffic), which does not establish sustained imbalance.
Source scrapes can miss the brief queue-full burst.

Observed acquisition means remained 3.71–4.46 ms and query means
25.96–28.32 ms. The one-second policy was present throughout each instance's
stored window. Database minute-level maximum CPU was 10.02%, memory 20.24%,
and active connections 44 including idle sessions. These averages/sampled
peaks do not explain the precise timing of the burst or establish database
CPU saturation. The observer correctly preserved `LOAD_JOB_FAILED`, with no
HPA metric gaps, and restoration verified two healthy pods and healthy
Browse/Operations with no backlog. This is still not a seven-minute pass.
Any next queue/admission comparison must be explicit and retain latency/error
acceptance criteria; do not silently rerun the preserved Job or ignore its
single failed request.

#### Temporary queue-32 comparison

The next comparison gives short bursts 32 waiting slots per pod while keeping
eight warm connections, the one-second check policy, four prewarmed pods and
all existing failure/latency/resource criteria. More waiting room can increase
latency; it does not establish more database capacity or guarantee a pass.

```bash
git pull --ff-only origin main &&
python3 scripts/browse-queue-comparison.py run
```

Allow about 30 minutes for the build, rollouts, seven-minute profile and
restoration. The command builds the recent-check image with Browse-only
support for queue 32, deploys it at queue 16 first, then makes an owned,
temporary Deployment queue change. UID/resourceVersion/template/annotation
compare-and-swap guards protect that change. Its durable marker records the
run, Deployment UID and exact temporary template fingerprint; restoration
refuses foreign runs, recreated Deployments or operator template changes.
HPA ownership, eight-connection policy and rollout connection budgets remain
enforced. Booking cannot use the new 32-slot option.

Both serving pods must report queue 32 and check interval one before load;
all four warm targets must then expose fresh queue-32 metrics. Queue limits
remain checked during observation. Default helper expectations stay at 16;
only this explicit process-local comparison context admits 32. The Job is
`browse-users-1000-queue32-profile`, with the same 120-second arrival ramp,
180-second hold and 120-second withdrawal. The report includes observed
queue-limit bounds and the expected policy in the observer audit.

After the workload, the workflow restores the owned temporary minimum to two,
captures the historical report, then restores queue 16 with a rollout and
health checks. It preserves the new image, existing failed Jobs, credentials
and inventory. A primary failure survives a report or restore failure. A
successful workload alone is insufficient for `BROWSE_QUEUE32_COMPARISON_OK`;
report collection and baseline restoration must also pass.

If Cloud Shell disconnects, wait until the new Job is terminal, then use:

```bash
python3 scripts/browse-queue-comparison.py diagnose
python3 scripts/browse-queue-comparison.py restore
```

Attempt restoration even if report capture fails. These actions refuse active
load and never reset inventory or manually scale the Deployment. Use this
queue-specific restore command: it recovers both the marked prewarm minimum
and the marked queue policy. Do not restart the comparison or delete its Job.
The queue-32 outcome remains pending until actual Cloud Shell evidence arrives.

For a new instrumented measurement trial, the entire release/test/report
sequence is available as one Cloud Shell command:

```bash
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py measurement-workflow
```

Keep the session open for the build, rollout, warmup, seven-minute profile
and restoration (allow about 30 minutes). This builds a new Browse image,
deploys only that image, verifies sequential health-check timing on both
pods, and runs a distinct `browse-users-1000-healthcheck-profile` Job. It
holds four warm pods with eight connections each for the same 120-second
ramp, 180-second hold and 120-second withdrawal. Queue, resource, error and
latency guards remain unchanged. Existing Jobs are preserved; invoking the
workflow again with its Job present fails before building or deploying.

The added `health_check` timer wraps the driver's existing check on every
Browse borrow, including failed attempts. Checks are neither skipped nor
made less frequent. Booking retains its original callback. This is an
instrumentation trial, not an automatic optimization or a two-to-four pod
autoscaling test. Health-check time overlaps acquisition and includes a
different population when checks retry; do not subtract their means and
label the difference exact queue time.

After a completed or handled stopped load, the existing ownership-checked
restoration returns the temporary minimum to two. The workflow attempts the
combined report afterward, preserving the original failure if capture also
fails. A report cannot turn a failed workload into a success. Setup failures
without a Job emit an explicit unavailable report. If Cloud Shell disconnects,
inspect the Job until terminal, then run:

```bash
python3 scripts/browse-autoscaling.py restore-prewarm
python3 scripts/browse-autoscaling.py measure-healthcheck-profile
```

Restoration refuses active load or foreign policy ownership. Do not start
another load or delete the preserved Job. The second command captures this
new trial only; the older profile's report command below remains separate.

After the profile Job is terminal, collect the available evidence together:

```bash
git pull --ff-only origin main &&
python3 scripts/browse-autoscaling.py measure-user-profile
```

This is a read-only cloud operation: it does not create load, change policy or
write inventory. It pins the existing profile Job identity and combines its
workload summary (or explicit interrupted status), per-instance queue/error
counters, source waiting samples, coverage and pool phase timings. Azure
database CPU, memory and connection aggregates use the Job start through the
post-terminal scrape tail, rather than a current five-minute window.

Timing means are ratios of increases in total microseconds and observation
counts, per instance. They include warmup, readiness and other traffic in the
same window. Acquisition combines waiting and checkout checks; these metrics
cannot isolate those two costs. Query time includes network and result
fetching; it is not server execution time. Body includes the query, so do not
add those phases together. No latency percentile can be reconstructed from
these counters. Missing series or means without observations stay unknown.

The command saves its JSON report at
`test-results/browse-measurements/<run-id>-report.json` and prints that path.
Future load invocations also save their resource, waiting and HPA metric
observer summaries on normal completion or handled failure, keyed by run and
Job UID. The combined command includes that audit only when identities match.
Old runs have no saved audit, and Cloud Shell resets may remove these local
files. Current pod usage is never presented as historical CPU/memory evidence.
Download the report before leaving the session if it needs to be retained.
An unavailable Azure metric section does not suppress the other measurements.
No new image build is required. This command does not rerun the preserved Job
or turn an interrupted workload into a pass.

See the official [AKS managed KEDA setup](https://learn.microsoft.com/en-us/azure/aks/keda-deploy-add-on-cli),
[KEDA Prometheus scaler](https://keda.sh/docs/2.18/scalers/prometheus/), and
[Kubernetes HPA behavior](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/).
