# Small Booking write baseline

Status: source and offline checks passed; live measurements remain pending.
This follows the read-only baseline and the completed Booking target-warning
drill. It requires no new application image or infrastructure deployment.

## Workload and limits

The Cloud Shell script runs 18 distinct one-ticket holds for demo-general and
cancels every one. Each intent uses its own simulated customer and idempotency
key. The first phase has one worker and six pairs; the second has four workers
and twelve pairs. Each worker starts at most one flow per second, with no
catch-up burst. There are at most four flows in flight and four tickets held
by this experiment concurrently. This is a short, count-bounded experiment,
not a sustained offered-load test.

Initial creates alternate across the two Booking pod forwards. Timed cancellation
goes through the other replica. Cleanup repeats cancellation idempotently, then
reads CANCELLED through both replicas. A normal run makes 90 workload/recovery
HTTP calls: 18 creates, 18 timed cancellations, 18 repeated cancellations and
36 readbacks. Additional readiness and Browse inventory checks are not timing
samples. There is no confirmation, seat-label change or inventory reset.

Each phase reports nearest-rank p50/p95/p99/max for the initial create and timed
cancel requests. These timings include Cloud Shell network/port-forward/client
overhead and JSON parsing; they are not server handler times or comparable to
the earlier direct-pod in-cluster read timings. The reserve timer excludes the
local recovery-journal write. Six and twelve samples are small: p95 and p99
both equal the maximum at these sample counts. Do not claim a production
latency SLO, throughput/capacity limit, statistically significant change or
database-lock bottleneck from this run.

The existing API processes and pool sizes are not changed. This experiment does
not independently prove pool reuse or token renewal; those have separate checks.
Reservation/cancellation history and outbox/inbox/projection records are retained.
The normal logical event expectation is 36 new events (HELD and CANCELLED for
each reservation), but the write helper does not audit stored worker state or
native queue counts. Check worker processing separately after the HTTP result.

## Run in browser Cloud Shell

Do not run another booking/load/scaling experiment at the same time. Both demo
events must start fully available and both APIs must have two stable ready pods.

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/check-booking-write-baseline.py
```

Keep the command running, normally about one to two minutes. It guards the
subscription/context, verifies readiness/Deployment-ReplicaSet-pod ownership,
and checks both demo events have 100 available tickets before starting.
BOOKING_WRITE_WORKLOAD_OK contains phase timings and per-intent cancellation
readback evidence. The final BOOKING_WRITE_BASELINE_OK additionally requires
100 available tickets for each demo event and unchanged serving pod
UIDs/images/restart counts/Deployment generations. Workload success alone is
not the final inventory/stability verdict. No confirmation message is sent
outside the existing lab event-processing pipeline.

## Cleanup and uncertain outcomes

Before any writes, a new owner-private JSON journal is created in the Cloud
Shell home directory: trafficxx-booking-write-<run>.json. It contains only
simulated customer IDs, request keys, reservation IDs, attempted/cleanup flags
and the fixed request body, not Azure credentials. Each attempted flag is
persisted atomically before sending a POST. The helper refuses to overwrite
an existing journal. Preserve it if the test fails; do not upload credential
files or reset inventory to clean up.

Every started flow enters cleanup, including when the create/cancel response is
lost. An unknown create outcome gets one replay using the exact original
customer/key/body, which may create one hold if the original never committed.
Cancellation has at most one cleanup retry. A failed workload remains failed
even if cleanup succeeds. New intents stop after a failure; already-started
flows finish their cleanup. A hard-killed Cloud Shell process cannot guarantee
cleanup; Ctrl+C attempts to finish in-flight work before forwarding processes
close. Uncancelled holds expire after five minutes, with release handled by
the existing expiry mechanism.

On failure, keep the named journal and inspect only attempted entries whose
cleanup_verified is false. Their original customer/key/body identify ambiguous
creates; a new key would represent a new reservation. Report the safe failure
JSON and whether unresolved entries remain so recovery can target those exact
intents. Do not repeat the whole write experiment to recover missing output.

## Local validation

All 432 offline methods passed. Ten new methods cover 18 unique bounded
intents/90 normal HTTP calls, opposite-replica cancellation and both-replica
readbacks, persisted intent-before-POST, lost create/cancel responses with the
same key, failed cleanup retaining IDs/flags, stopping after partial failure,
journal overwrite refusal, subscription/inventory guards and post-workload
pod/inventory evidence. HTTP responses are simulated; no Azure write timings
or real database load were measured locally. Docker stayed stopped, no local
Azure access or credential read occurred, and hosted CI remains unchecked.
