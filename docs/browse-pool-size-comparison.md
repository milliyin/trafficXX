# Temporary Browse pool size comparison

Status: live 250-user trial passed; four-connection baseline restored and health verified.

A separate three-minute stability mode has also passed. Its result, commands
and recovery journal are described at the end of this guide.

The user supplied BROWSE_POOL8_PREFLIGHT_OK with journal_created false and
pool_policy_changed false. Both serving pods confirmed minimum/maximum four
and queue limit 16. Demo availability remained 100 per event and Operations
reported processed 72, zero backlog or processing failures, no active alerts
and healthy worker heartbeats. The full trial source also passed all GitHub
CI jobs in [run 40115229776](https://github.com/milliyin/trafficXX/actions/runs/40115229776).

The latest [capacity observation](browse-user-load.md#live-read-only-capacity-observation-passed)
found low recent database resource use and cheap cached query execution, with
client database calls much longer than server execution. Both 250-user attempts
stopped early on queue-full rejections with four connections per pod. This
supports a controlled concurrency comparison; it does not prove eight
connections will fix the workload or establish database capacity.

The later result below confirms this one bounded trial passed with eight
connections. It does not establish sustained capacity or a permanent setting.

## Scope

The runner temporarily changes **only BROWSE_DB_POOL_MIN and BROWSE_DB_POOL_MAX
from four to eight**, using the existing immutable image. Both replicas keep
queue limit 16, the current single-query availability path, transaction handling,
checkout checks, identity, permissions and CPU/memory limits. Booking is unchanged.
The repository deployment manifest remains at four.

The diagnostic is the same 250-user, 60-second workload, with two-second
pauses and at most 7,500 GETs. It uses the distinct Job
`browse-users-250-pool8`; earlier Jobs are preserved. Generator limits remain
one CPU/256 MiB, and the generator binds to an existing ready apps node.
Fresh-resource, HTTP/pool error, two-second request, sampled ten-second waiting,
CPU/memory, pod/process stability and scoped-stop checks remain enabled.
Only this explicit mode accepts warm eight-connection Browse statistics;
ordinary probes and Booking retain their four-connection ceiling checks.

There are two steady-state Browse replicas. Normal rolling updates can
temporarily add one surge pod and additional SQL connections. Each policy
rollout must complete with two stable pods before the load or restoration
checks. The runner changes no replica or cluster autoscaler setting.

## Preflight: no policy change or load

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/run-browse-pool-comparison.py --preflight-only
```

This verifies the lab subscription/context, two stable Browse and Booking
replicas, immutable Browse image, baseline four/four/queue-16 policy, no Browse
HPA, no active read-load Job, no existing trial Job/journal, health and fully
available demo inventory, and fresh low-pressure Browse resource samples.
It prints BROWSE_POOL8_PREFLIGHT_OK, without creating a journal or changing
pool policy. HTTP health checks perform only reads.

## Reviewed trial

After reviewing preflight output, the full command is:

```bash
python3 scripts/run-browse-pool-comparison.py
```

No image build is needed. The command saves a private local recovery journal
at `~/trafficxx-browse-pool8-journal.json` before the first mutation. It contains
the deployment UID, image and template fingerprints, not credentials or the
environment contents. It refuses to overwrite an existing journal.

The JSON patch tests the deployment UID, resource version, original template
fingerprint and original pool values before changing the two pool settings.
The runner then waits for rollout, verifies actual warm eight-connection
pools in both processes, checks health/inventory, waits for new pod metrics,
and starts only the scoped pool-eight workload. No old failed Job is reset.

The workload must verify warm eight/eight pools, queue 16, unchanged errors,
single-query timers, legacy query counts, the HTTP client policy, numeric
resource evidence and serving-process/pod stability. Failure logs now include
same-process phase timing deltas where available. Those phase populations
include readiness and snapshot boundary effects; they are not all the same
set of completed requests.

The runner also requests one-minute Azure database CPU, memory and connection
metrics for the load-runner window, including generator warmup. Publication
lag or a brief failure can leave no samples. Missing metrics are reported as
unavailable, not zero, and do not become a database-headroom verdict or an
extra automatic database resource stop. Application/generator resource guards
remain the existing ones. Even an available minute series can miss short peaks.

## Restoration and interruption

On success, failure or handled Ctrl+C, the runner attempts to restore four
connections, waits for rollout, verifies both pools, and repeats demo/Operations
health checks. **It restores four even when the eight-connection test passes.**
The result distinguishes a passing workload from verified restoration.
BROWSE_POOL8_COMPARISON_OK requires both. There is no automatic 500-user stage
or permanent capacity change.

Keep Cloud Shell running for up to 15 minutes, allowing for two rollouts,
metric warmup, workload observation and recovery. A hard disconnect, process
kill or machine loss can prevent the Python finally block from running.
Keep the journal. After any diagnostic load is terminal, resume restoration:

```bash
python3 scripts/run-browse-pool-comparison.py --restore
```

Restoration refuses active diagnostic loads, replacement deployments, image
changes, unexpected policy, or other template edits. It tests the current
resource version and changes only the two pool values. Conflicts require
inspection; it never overwrites another edit or deletes a load Job. A restore
command reruns recovery checks without starting another workload. Journals
remain available after completion for review.

If output says BROWSE_POOL4_RESTORE_FAILED, do not start another test. Inspect
the stated check and current deployment before retrying restoration. Retain
the full output, including load result, database metrics and restoration result.
No inventory reset or SQL write cleanup is needed for this read-only workload.

## Live trial passed and baseline restored

Run `15c72440c1ef`, Job `browse-users-250-pool8`, used the existing caf image
`sha256:f3242207750d853f9980a1b18d72b618e8b40abea8aef5afd6e820d50be61cac`.
Baseline health and four-slot policy checks passed; the temporary rollout then
verified warm minimum/maximum eight with queue 16 in both new processes.

| Measurement | Result |
| --- | ---: |
| Participating paced users | 250 of 250 |
| Successful reads / attempts | 6,986 / 6,986 |
| HTTP / observer / pool errors | 0 |
| Measured elapsed | 60.144 seconds |
| Observed request rate | 116.154 requests/s |
| Latency p50 / p95 | 141.560 / 191.658 ms |
| Latency p99 / maximum | 227.932 / 383.112 ms |
| Observed peak HTTP requests in flight | 31 |
| Generator sampled peak process RSS | 42.695 MiB |
| Generator sampled peak threads | 252 |

All responses returned HTTP 200. Routes received 3,492 seated and 3,494 general
reads. Inventory stayed 100 per event, with no inventory writes. Both serving
pods/processes stayed unchanged during the load, made zero new connection
attempts and showed no increase in error/rejection counters. Sampled pool size
peaked at eight in each pod, with waiting peaks four and six. The workload
passed the existing resource, latency, waiting, query-policy and identity gates.

| Mean client-observed phase | 55pzs | w7vbf |
| --- | ---: | ---: |
| Acquisition | 35.473 ms (3,471 observations) | 38.812 ms (3,527) |
| Connection body | 54.804 ms (3,471) | 56.127 ms (3,528) |
| Return | 27.297 ms (3,471) | 28.036 ms (3,528) |
| Combined query | 54.771 ms (3,465) | 56.110 ms (3,521) |

Phase counts differ because readiness and in-flight work can cross snapshot
boundaries. Body includes query work, and acquisition includes checkout checks
and queue waiting. These means are not all additive or per-request percentiles.
The trial pod instance identifiers were 206ccb818be142feae17f07e8e98d890 and
2133bb6c31d84e3daae8f2f9cdd0d148.

The generator recorded 62 RSS samples, starting at 26.371 MiB and ending at
35.008 MiB. Kubernetes sampled generator maxima were 0.077509 CPU cores and
40.191 MiB across three distinct samples. App sampled CPU maxima were
0.091875/0.083609 cores and memory 64.879/65.086 MiB, across six/four samples.
CPU is windowed and memory/queue maxima are sampled, not precise peaks.

The database metric window was 2026-10-24 17:37:36-17:40:59 UTC, including
generator warmup. Each metric had three observations: CPU maximum 5.514583%,
memory maximum 21.195366% and connections maximum 25 (including idle).
All metrics had samples, but their cadence and publication lag still limit
interpretation of brief pressure.

Both BROWSE_USERS_WORKLOAD_OK and BROWSE_USERS_250_OK were followed by verified
restoration. New baseline pods `browse-6449545789-pw6wp` and
`browse-6449545789-z7r5q` confirmed minimum/maximum four and queue limit 16.
Post-restoration demo health was alive/ready with availability 100 per event.
Operations confirmed processed 72, no backlog or processing failures, no active
alerts and healthy heartbeats (consumer 1.283536s, publisher 1.197073s).
Historical publication failures four and dead-letter audit one were unchanged.
Native broker depth was not measured by these HTTP checks.

Final results were BROWSE_POOL4_RESTORED (health_verified true) and
BROWSE_POOL8_COMPARISON_OK (load_passed, baseline_pool_restored and
database_metric_samples_available all true). The journal remains at
`/home/jeremy/trafficxx-browse-pool8-journal.json`. No manual restore is required
for this completed run; retain the journal and logs for evidence.

**The current setting is four connections per pod.** The 250-user pass applies
to the temporary eight-connection configuration. This supports the bounded-pool
concurrency hypothesis, while changes in pod placement, generator node (000000
in this trial versus 000001 previously), timing and a single short observation
prevent isolating every causal factor. It is not 250 simultaneous requests,
a reservation-write test, a maximum-capacity measurement or a production SLO.
Longer/repeated trials and a decision on retaining eight should precede claims
about sustained operation or higher user counts. No 500-user stage ran.

## Prepared three-minute stability mode

This repeats 250 paced users with the same eight connections per pod for
180 seconds instead of 60. It keeps two-second pauses, queue limit 16, two
steady replicas, single-query SQL, transaction/checkout behavior, generator
one CPU/256 MiB and all error/latency/waiting/resource stop conditions. It is
not an autoscaling test and does not permanently retain eight connections.

Run only the preflight first:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/run-browse-pool-comparison.py --duration 180 --preflight-only
```

The expected BROWSE_POOL8_PREFLIGHT_OK includes duration_seconds 180 and Job
`browse-users-250-pool8-180s`, with journal_created and pool_policy_changed
both false. It still requires the currently restored four-connection baseline.
No image rebuild is needed.

After reviewing preflight, the full command is:

```bash
python3 scripts/run-browse-pool-comparison.py --duration 180
```

This uses a separate recovery journal:
`~/trafficxx-browse-pool8-180s-journal.json`. It preserves the original one-minute
Job and journal. Its workload is capped at **22,500 GETs** (90 per user), with
bounded in-memory receipt storage. Existing resource limits are not increased.
The diagnostic Job deadline becomes 540 seconds and the parent's observation
deadline 570 seconds, allowing for unchanged startup gates and the longer
workload. The resource-gate timeout remains 240 seconds. These are process
lifetime bounds, not relaxed per-request or resource stop thresholds.

Final validation requires the configured 180-second duration, measured elapsed
180-195 seconds, all 250 participants, matching successful responses and the
existing pool/query/resource/stability evidence. A one-minute result cannot
pass as a three-minute result. Database metrics still include generator warmup
and are subject to one-minute aggregation/publication delay.

The runner attempts restoration to four on completion or handled failure,
verifying both actual pool policies and health. Keep Cloud Shell running until
the final restoration result; allow roughly 15-20 minutes for rollouts,
sampling, workload and recovery. If interrupted hard, retain the three-minute
journal and, after diagnostic loads are terminal, use the matching command:

```bash
python3 scripts/run-browse-pool-comparison.py --duration 180 --restore
```

The original one-minute restore command continues to use its original journal.
Do not remove either journal or reset a Job to bypass an existing-run guard.
The longer mode accepts only the explicit 250-user, eight-connection,
single-query configuration; it is not a general arbitrary-duration load tool.
The live three-minute preflight passed: both Browse pods remained at minimum
and maximum four connections, queue limit 16, with healthy demo and Operations
checks. No journal was created or pool policy changed. Source CI also passed.
The subsequent live trial passed: run `5707b72eb33d` completed 21,133 successful
reads from all 250 users in 180.132 seconds, zero errors, p95 167.385 ms,
p99 188.764 ms, and maximum 267.868 ms. Both pools remained at eight without
new connection attempts or pool errors. Six minute-level database observations
reached CPU 8.64%, memory 21.36%, and 28 connections. The wrapper then restored
four connections per pod and verified health. See the [new autoscaling profile](browse-autoscaling.md)
for the selected eight-connection policy and bounded next experiment.
The original token-renewal Job remains a separate pending verification.
