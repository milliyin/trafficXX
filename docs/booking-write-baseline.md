# Small Booking write baseline

Status: source, offline checks and the live bounded write baseline passed.
The first attempt failed before journal creation/writes; its failing command
remains unknown. The later read-only preflight and complete write run passed.
This follows the read-only baseline and the completed Booking target-warning
drill. It requires no new application image or infrastructure deployment.

## Verified live measurements

The user supplied BOOKING_WRITE_WORKLOAD_OK and BOOKING_WRITE_BASELINE_OK for
journal trafficxx-booking-write-cb92a4efef03.json. All 18 holds were created and
cancelled, with cancellation readbacks through both Booking replicas and cleanup
verified. Both demo events returned to 100 available tickets. Serving pods,
images and process restart counts remained unchanged across the experiment.

| Workers | Completed pairs | Phase seconds | Reserve p50 ms | Reserve p95/max ms | Cancel p50 ms | Cancel p95/max ms |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 6 | 15.097 | 562.123 | 615.504 | 538.130 | 595.680 |
| 4 | 12 | 8.466 | 674.846 | 1015.309 | 655.202 | 839.020 |

p99 also equalled the maximum in both small phases. Phase elapsed time includes
cleanup, both-replica readbacks and pacing; it is not the sum of timed requests
or a sustained arrival-rate measurement. Observed request times increased in
the four-worker phase. These samples do not isolate a database bottleneck or
establish a statistically significant difference. Cloud Shell/port-forward
overhead prevents direct comparison with the earlier in-cluster read baseline;
latency_slo_tested was false.

Booking pods were booking-67b7966d6c-l6xv7 and booking-67b7966d6c-npkcg, using
digest c94884448905a0e94f2e4b8502ed120767f8cf46a2aa2f8747862ad5f6a663f0.
Browse used digest f09d49ce8044461be8dbde85835888fec67a5ca5a2d848b20ed422da211c1e51.
The script deliberately performed inventory writes, followed by verified
cancellation and inventory restoration. Its HTTP result does not establish
that all newly emitted events have drained through publisher/consumer. The
subsequent user-supplied OPERATIONS_HTTP_OK reported processed_total 72, up
from the prior checkpoint of 36 and matching the expected 36 new logical events.
Outbox pending/age and processing outstanding/age were all zero; processing
failures stayed zero, publication failures stayed at the historical total of 4,
and the historical dead-letter audit count stayed 1. Consumer/publisher
heartbeats were fresh at 3.317029/0.094423 seconds with no errors or active alerts.
This is successful aggregate worker monitoring after the write run, not a
fresh row-by-row inbox/payload/projection audit or native Service Bus queue
count. native_queue_counts_available and notification delivery remain false.

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

To diagnose a preflight failure without starting writes:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/check-booking-write-baseline.py --preflight-only
```

This mode checks the subscription, context, both API pod snapshots and initial
demo inventory. It never loads the write client, creates a journal or starts
reservation flows. Errors now include the exact stage and command exit code,
without printing raw command output. Share that JSON before rerunning writes.
BOOKING_WRITE_PREFLIGHT_OK establishes read-only readiness only, not write-test
success. Possible stages distinguish account/context, individual API pod checks,
inventory before/after, workload and final pod stability checks.

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

All 434 offline methods passed. The original ten new methods cover 18 unique bounded
intents/90 normal HTTP calls, opposite-replica cancellation and both-replica
readbacks, persisted intent-before-POST, lost create/cancel responses with the
same key, failed cleanup retaining IDs/flags, stopping after partial failure,
journal overwrite refusal, subscription/inventory guards and post-workload
pod/inventory evidence. HTTP responses are simulated; no Azure write timings
or real database load were measured locally. Docker stayed stopped, no local
Azure access or credential read occurred, and hosted CI remains unchecked.
Two additional methods verify read-only mode cannot enter the write client or
journal creation, and command errors report stage/exit code without raw output.
