# Booking database pooling

Status: source/local validation, Azure image build ca9, Booking rollout,
read-only serving-pod reuse check and isolated SQL pool probe passed.
Post-pooling reservation flow, duplicate concurrency and seat contention passed.
Post-test demo inventory and Operations health checks also passed. These are
bounded functional checkpoints, not a sustained-load or token-renewal result.
Existing Browse monitoring is verified; its overnight token-renewal Job stays
on its original image/run and must not be restarted for this change.

## Verified image build

The user supplied successful ACR run ca9 after 43 seconds, tag
booking-20261023173158-17984, with immutable image:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:d00af2abd005f9c73157b8db532fbc45ea1bb51b68bdb2b8cee71fa00afad9e3
```

The reported Python 3.12-slim-trixie base digest matches the earlier Browse
build. The helper saved booking-image.txt in Cloud Shell, and the committed
Booking manifest now pins this reviewed image. Build success does not prove
rollout, live pooling, transaction correctness or a passing hosted CI run.

The first user-run deployment attempt returned "no objects passed to apply".
The helper used kubectl set image --local, which emits nothing when the image
already matches the file. Pinning ca9 introduced exactly that no-op. The
deployment apply received no objects; rollout and probes did not run.
Both Booking and Browse helpers now render with local JSON patch, test the
expected container name, and reject an empty result before apply. Unchanged
and changed digests both produce a full object. No rebuild is needed; pull the
fix and retry the deployment/check commands below. The user-run retry has now
passed rollout and both read-only checks, as recorded below.

## Verified live pool checkpoint

The fixed helper successfully rolled out Booking. New pods
booking-76c99c6fbb-dwtxs and booking-76c99c6fbb-tzq9r were Ready 1/1 with zero
restarts on the two different apps nodes. One old pod was still Terminating in
the displayed snapshot; this does not establish node-failure resilience.

BOOKING_HTTP_OK confirmed alive/ready, the expected API and customer-scoped
missing-reservation404. The same forwarded process reported four pool borrows,
zero new connection attempts, sampled size1 and maximum4. Reuse was supported,
and readiness passed after the lookup's rollback. No inventory writes occurred.
This is a bounded read-only observation of one serving pod, not both replicas
or a load/latency SLO.

The separate booking-pool-check Job completed with BOOKING_POOL_OK, identity
trafficxx-lab-booking and TLSv1.3. Physical connection reuse, rollback reuse,
capacity503 and closed-connection replacement all passed with its own one-slot
pool. The "discarding closed connection" warning belongs to the deliberate
diagnostic close. It made no inventory writes and does not prove Booking token
renewal or serving-pod write correctness by itself.

The subsequent BOOKING_FLOW_OK verified reservation
a09bb8b1-289b-4f64-958a-a0bb920e1d53: duplicate requests reused its hold,
changed-request key reuse was rejected, another customer's lookup was hidden,
cancellation reached CANCELLED, and replaying the cancelled key returned that
same outcome. This establishes the sequential pooled API flow for that run.
The test retains cancelled history/outbox rows; post-test inventory and event
processing were checked separately below.

BOOKING_CONCURRENCY_OK then verified reservation
44edc74e-7184-4715-b037-eef6e36234f4 through both new Booking pods
(booking-76c99c6fbb-dwtxs and booking-76c99c6fbb-tzq9r). Concurrent submissions
returned HTTP200/201 with the same reservation and expiry. Both-replica reads
passed and the final status was CANCELLED. This supports duplicate handling
across the pooled replicas for that pair; it is not a load benchmark or proof
of all possible races.

BOOKING_SEAT_CONTENTION_OK subsequently tested two different customers for
demo-seated/A1 through those same two pooled replicas. Responses were HTTP201
and409 SEAT_UNAVAILABLE, with winning pod index0 and losing index1. Both-replica
reads passed for reservation a727535e-28e1-46d1-91c8-0a15f9151689, and cleanup
reached CANCELLED. That pair had one winner; it does not establish all inventory
races or sustained-load behaviour.

After all three write experiments, BROWSE_DEMO_HTTP_OK verified two events with
100 available units each for demo-general and demo-seated, plus alive/ready.
OPERATIONS_HTTP_OK reported 36 processed events, zero outbox pending and
processing outstanding, zero processing-failure records, and no active alerts.
Consumer/publisher heartbeats were fresh (1.585381/0.217789 seconds) without
errors. Publication-failure history remained4 and dead-letter audit history1;
neither is a current backlog or native queue-depth count. This confirms the
bounded post-test inventory and aggregate processing-health checkpoint. It is
not a new full-row inbox/projection consistency audit or native queue check.

## What changes

Booking now owns one synchronous psycopg-pool pool per Uvicorn process. Its
FastAPI lifespan opens/closes the pool; importing the app starts no connections
or background threads. Readiness and all four reservation routes borrow from
that same pool. A private /internal/database-pool endpoint exposes allowlisted
numeric statistics and a random process-instance ID without borrowing SQL.
Keep /internal paths excluded from any future public Booking ingress.

The existing Browse implementation is extracted into database_pool.py, with
separate BrowseDatabase and BookingDatabase policies. Browse's import path,
configuration and behaviour remain compatible. Booking has its own pool name
and BOOKING_DB_POOL_MAX, default 4 and allowed 1..8. It does not change the Browse
limit or share its live connections. Workers, migrations and service callers
without an injected connection factory retain direct connections.

| Setting | Booking value |
| --- | --- |
| Minimum connections | 1 per process |
| Maximum connections | BOOKING_DB_POOL_MAX=4 in the lab Deployment |
| Waiting borrowers | At most 8 |
| Borrow timeout | 3 seconds, then 503 DATABASE_BUSY |
| Connection lifetime | 1800 seconds with library jitter |
| Extra idle connection age | 60 seconds |
| Checkout validation | ConnectionPool.check_connection |

Two Booking pods with one process each can hold up to eight SQL connections
in steady state, in addition to Browse's eight. Rolling/terminating replicas,
diagnostic Jobs, workers and administrator sessions add to this budget. This is
not a whole-database limit or a measured latency/throughput improvement.

## Transaction and identity boundaries

reserve and reservation accept a keyword-only connection_factory. The Booking
app supplies its pool; direct service callers preserve their previous default.
One borrow still contains idempotency/event locks, reservation/inventory changes
and outbox insertion. There is no per-statement borrowing, autocommit change,
automatic write replay or change to lock order, hold duration or idempotency.

A successful context commits and returns its connection. Exceptions roll back
before return; a broken connection is replaced. The existing expired-action
conflict is deliberately raised after the context exits so expiry and inventory
release commit before the 409 response. Arbitrary persistent SET commands and
session-level locks remain unsupported without a reset policy.

Every new physical connection resolves validated connection parameters through
the existing SDK WorkloadIdentityCredential cache. No startup access token is
frozen in pool configuration. The Booking service account, SQL role, CA-verified
TLS, statement/lock/connect timeouts and grants stay the same. No credentials
belong in image-reference files. See the [Psycopg pool API](https://www.psycopg.org/psycopg3/docs/api/pool.html)
for connection contexts and per-attempt callable parameters.

## User-run Cloud Shell release

First pull and build the new source. Building alone changes no running pods:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-booking-image.sh
```

Review IMAGE_BUILD_OK and its immutable digest. The helper checks the lab
subscription/registry and saves the reference to ignored booking-image.txt.
After a successful build:

```bash
bash scripts/deploy-booking.sh &&
bash scripts/check-booking-http.sh --pool &&
bash scripts/check-booking-pool.sh
```

Deployment validates account/context/digest and substitutes only Booking's
image using kubectl's local JSON patch transformation. The committed manifest
also pins reviewed ca9; the helper still requires the validated build reference.
The two Booking replicas roll out with BOOKING_DB_POOL_MAX=4. No Browse/Grafana/
Prometheus/worker or renewal-Job rollout occurs.

The read-only HTTP check retains health/readiness, API route and customer-scoped
missing-reservation 404 checks. With --pool it also compares statistics for one
port-forwarded serving process, requires more borrows than new connection
attempts, unchanged error counters and a ready response after the 404 rollback.
It reports reuse_supported, not the exact physical connection for every request,
both replicas, sustained load, or a latency SLO. A process change fails the check.

The temporary booking-pool-check Job uses Booking's currently deployed immutable
image and service account. Only its own process uses maximum 1. It verifies SQL
identity/TLS, physical PID reuse, rollback after SELECT 1/0, bounded capacity 503
and replacement after closing its own connection. It makes no inventory writes
and never terminates another session. Expect BOOKING_POOL_OK. A discarded-closed-
connection warning is expected for the deliberate diagnostic close. The Job has
no retries, a 180-second deadline and ten-minute completion TTL; wait for cleanup
before rerunning the same name, or inspect the completed Job first.

After those checks, repeat the existing behavioural tests in separate steps:

```bash
bash scripts/check-booking-flow.sh
python3 scripts/check-booking-concurrency.py
python3 scripts/check-booking-seat-contention.py
bash scripts/check-browse-http.sh --demo
bash scripts/check-operations-http.sh
```

These create/cancel diagnostic reservations and preserve history/outbox events.
Use their original keys to resolve ambiguous outcomes; a pool does not make a
failed network response proof that a write was never committed. Flow/concurrency/
seat-contention success must be supplied before claiming pooled Azure writes
remain correct. Publisher/consumer may need time to drain the extra events.

## Validation and remaining evidence

Local validation passed 387 offline tests, including provider injection, rollback
versus commit-before-conflict, no write replay, independent pool limits and
serving-check rejection of process resets, missing reuse or new errors. A smoke
test using the pinned FastAPI and psycopg-pool packages confirmed app lifespan,
readiness and injection for all reservation routes with fake SQL. Bash syntax,
manifest structure and mocked image/build/deploy/probe guards passed.

Three release-helper regression methods cover same/changed digest rendering,
empty/failed render rejection before apply, and invalid image rejection for
both APIs. The actual local kubectl v1.32.2 binary reproduced the old no-op and
validated complete JSON patch output for both cases, using an empty isolated
kubeconfig and local-only commands. No cluster/account connection was used.

Five real PostgreSQL regression tests are added to the dedicated CI database
and their collection passed locally (pytest 8.4.2; CI pins 9.1.1):
outbox-failure rollback of all writes plus advisory/event-lock release; expiry
cleanup committed before conflict; concurrent duplicate handling; same-seat
contention; and HTTP pool sharing/capacity/shutdown. Docker remains stopped, so
those database tests were not run locally. Hosted CI and a new full-row consumer
consistency/native queue audit remain unverified. No throughput gain, Booking token renewal, pool dashboard
or external notification delivery is claimed by this stage.
