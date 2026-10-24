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

## Failure or disconnect

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

### Live v2 workload completed; recovery check remains

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

See the official [AKS managed KEDA setup](https://learn.microsoft.com/en-us/azure/aks/keda-deploy-add-on-cli),
[KEDA Prometheus scaler](https://keda.sh/docs/2.18/scalers/prometheus/), and
[Kubernetes HPA behavior](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/).
