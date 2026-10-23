# Booking database pooling

Status: source/local validation and Azure image build ca9 passed. Booking
rollout, isolated SQL probe and serving-pod checks are pending.
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
image using kubectl's local manifest transformation. The committed manifest
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

Local validation passed 384 offline tests, including provider injection, rollback
versus commit-before-conflict, no write replay, independent pool limits and
serving-check rejection of process resets, missing reuse or new errors. A smoke
test using the pinned FastAPI and psycopg-pool packages confirmed app lifespan,
readiness and injection for all reservation routes with fake SQL. Bash syntax,
manifest structure and mocked image/build/deploy/probe guards passed.

Five real PostgreSQL regression tests are added to the dedicated CI database
and their collection passed locally (pytest 8.4.2; CI pins 9.1.1):
outbox-failure rollback of all writes plus advisory/event-lock release; expiry
cleanup committed before conflict; concurrent duplicate handling; same-seat
contention; and HTTP pool sharing/capacity/shutdown. Docker remains stopped, so
those database tests were not run locally. Hosted CI, Azure rollout and the new
SQL/HTTP checks remain unverified. No throughput gain, Booking token renewal, pool dashboard
or external notification delivery is claimed by this stage.
