# Temporary Browse pool size comparison

Status: prepared, with no live preflight or load result yet.

The latest [capacity observation](browse-user-load.md#live-read-only-capacity-observation-passed)
found low recent database resource use and cheap cached query execution, with
client database calls much longer than server execution. Both 250-user attempts
stopped early on queue-full rejections with four connections per pod. This
supports a controlled concurrency comparison; it does not prove eight
connections will fix the workload or establish database capacity.

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
