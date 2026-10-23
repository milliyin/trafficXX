# Booking identity and database permissions

Current next step: [Booking connection pooling](booking-database-pooling.md).
The sections below record earlier verified identity/API stages. The current
deploy-booking.sh requires a fresh build-booking-image.sh result in ignored
booking-image.txt; the original shared-image release did not have this requirement.

Browse is deployed and its seeded availability and single-pod recovery checks
have passed. Booking's managed identity, federated credential and Kubernetes
service account already exist. The user then verified BOOKING_ROLE_READY with
CONNECT/USAGE true, public CREATE/admin false, followed by real booking pod
SQL_LOGIN_OK: trafficxx, trafficxx-lab-booking, TLSv1.3, admin=false and the
expected client ID. The user then supplied BOOKING_GRANTS_OK from migrator:
three read tables, no outbox read, worker-table access or destructive table
access. Actual booking-role tests then passed: eleven permitted operations,
twelve 42501 denials, no diagnostic rows committed, TLSv1.3 and admin=false.
Booking rollout, read-only HTTP smoke checks and the sequential real API
hold/retry/conflict/cancellation experiment subsequently passed. Concurrent
duplicate requests across two explicit replicas subsequently passed. A Browse demo HTTP check
reported both demo-general and demo-seated at 100 available after cancellation.

## Register booking and verify its own SQL login

Upload the generated trafficxx-booking-setup.zip, containing the reviewed SQL,
existing administrator helper and booking-specific read-only login Job:

```bash
unzip -o ~/trafficxx-booking-setup.zip -d ~/trafficxx-booking-setup
cd ~/trafficxx-booking-setup
bash run-postgres-admin-sql.sh 002-register-booking.sql &&
kubectl apply -f postgres-booking-identity.yaml &&
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-booking-identity-check --timeout=300s &&
kubectl logs -n trafficxx job/postgres-booking-identity-check -c check
```

The human administrator creates or verifies trafficxx-lab-booking against
service-principal object ID 3dac90be-1d3c-4a7f-b444-309ea3f308f1 in the expected
tenant. A differently mapped or privileged existing role is rejected rather
than adopted. It grants CONNECT on trafficxx and USAGE on public, with an
effective-privilege guard rejecting CREATE on the schema/database. No table,
sequence or administrator grants are made. Registration and grants are separate
transactions in postgres and trafficxx, as with the migrator setup; rerunning
checks the existing mapping and retries grants after a partial failure.

Expected SQL summary: BOOKING_ROLE_READY, can_connect=t, can_use_schema=t,
can_create_objects=f, admin=f. The Job then uses serviceAccountName=booking and
its own Azure SDK credential, expecting client ID
b7cf76ca-6692-432f-8e0e-088b42d9bff0, and connects as trafficxx-lab-booking over
verify-full TLS. Expected output: SQL_LOGIN_OK, identity trafficxx-lab-booking,
database trafficxx, ssl=true and admin=false. It only reads session metadata;
passing this check does not prove reservations can be written.

The helper retains its subscription/context guards, copied public CA, preflight
before token acquisition, stdin-only human token transport and cleanup. The Job
retains the known diagnostic init dependency installation, zero retries, bounded
deadline and ten-minute TTL. Delete only its named diagnostic Job before reruns
while it still exists. No API image build or rollout occurs in this stage.

## Table grants applied; runtime operations still need verification

The current trafficxx/service.py queries require:

| Object | Required access | Purpose |
|---|---|---|
| events | SELECT and a limited UPDATE column privilege | Read event type and lock its row before changing inventory |
| reservations | SELECT, INSERT on request fields, UPDATE(status) | Idempotency lookup, holds, confirmation, cancellation and expiry |
| ticket_units | SELECT and UPDATE(reservation_id) | Claim/release existing inventory units |
| outbox | INSERT on reservation_id, event_type, payload | Atomically record reservation state changes |

No general table-delete, DDL, publication-metadata update, consumer-inbox or
worker-control access is needed. Table grants must be applied by the migrator
owner, and the actual insert/default/identity behavior must be tested as booking.
No broad sequence privileges are assumed before that test.

The bootstrap helper's mutually exclusive --grant-booking action and
kubernetes/jobs/postgres-booking-grants.yaml implement this contract. The Job
runs as the migrator/table owner, not booking or the human administrator. It
grants SELECT on events/reservations/ticket_units, UPDATE(name) on events,
INSERT(id,event_id,customer_id,idempotency_key,request,quantity,status,expires_at)
and UPDATE(status) on reservations, UPDATE(reservation_id) on ticket_units,
and INSERT(reservation_id,event_type,payload) on outbox. It does not grant any
sequence permissions or table creation/deletion capabilities.

It checks effective privileges for all current public table columns. Unexpected
reads outside the three source tables, writes beyond the explicit column lists,
REFERENCES, DELETE/TRUNCATE/TRIGGER, schema/database CREATE or elevated role flags
abort the transaction. This rejects inherited excessive access too; it does not
silently revoke unrelated grants. Success is printed only after commit.

Upload trafficxx-booking-grants.zip and run:

```bash
unzip -o ~/trafficxx-booking-grants.zip -d ~/trafficxx-booking-grants
cd ~/trafficxx-booking-grants
kubectl create configmap postgres-booking-grant-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f postgres-booking-grants.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-booking-grants --timeout=300s
kubectl logs -n trafficxx job/postgres-booking-grants -c grant
```

Expected output is BOOKING_GRANTS_OK with grantee trafficxx-lab-booking,
outbox_read=false, worker_table_access=false and destructive_table_access=false.
The user supplied this successful result from Azure. This stage granted
privileges only; inventory and reservations were not modified. Run the real
booking-role test below before Booking API deployment.

## Verify real booking-role operations

Upload trafficxx-booking-permissions.zip, then run:

```bash
unzip -o ~/trafficxx-booking-permissions.zip -d ~/trafficxx-booking-permissions
cd ~/trafficxx-booking-permissions
kubectl create configmap postgres-booking-permission-files -n trafficxx \
  --from-file=check-booking-permissions.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f postgres-booking-permissions.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-booking-permissions-check --timeout=300s
kubectl logs -n trafficxx job/postgres-booking-permissions-check -c check
```

The probe uses the booking service account, its own SDK token and verify-full
TLS. It checks identity/non-admin flags in an explicit read-write transaction.
Eleven allowed checks exercise the idempotency advisory lock, event FOR UPDATE,
permitted no-op name update, free-unit lookup, reservation INSERT RETURNING,
customer/key lookup, ticket claim, HELD outbox insert, cancellation status update,
ticket release and CANCELLED outbox insert. The existing demo-general event and
at least one unassigned unit are required. No missing inventory is reset.

It expects twelve 42501 denials: event-inventory-type edit, customer edit, hold
extension, seat-label edit, adding inventory, reservation DELETE, outbox read,
publishing metadata update, consumer-inbox/migration-ledger reads and public
table/schema creation. Each forbidden attempt has a savepoint and is rolled
back even if unexpectedly permitted; unexpected success fails the Job. Missing
objects, syntax or read-only errors do not count as permission evidence.

The whole transaction is rolled back before success is printed, including both
temporary outbox rows, the temporary reservation and ticket/name changes. The
outbox inserts deliberately use actual defaults/implicit identity generation
without SELECT/RETURNING or additional sequence grants. PostgreSQL sequences
can advance even when rows are rolled back, so gaps in outbox IDs after this
probe are normal; sequence numbers are not ticket counts. See
[PostgreSQL sequence behavior](https://www.postgresql.org/docs/17/functions-sequence.html).

Expected result: BOOKING_PERMISSIONS_OK, eleven allowed names, twelve denied
names, denial_sqlstate=42501, diagnostic_rows_committed=false and safe TLS/user
metadata. The user supplied this exact success result from Azure, including both
outbox default/identity inserts without adding sequence grants. Failure logs
include a fixed check name, error class and SQLSTATE, never exception text/tokens/
customer data. The usual dependency-install and Job cleanup limitations apply.
This is SQL capability validation, not API duplicate handling or oversell/load
testing; those follow after Booking API deployment.

## Original Booking API deployment using the existing runtime image

The image already deployed for browse contains trafficxx.booking and the generic
workload-identity database connection implementation. No new application build
is needed for this stage. The booking Deployment pins that same known image:
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:22a28c8c6cc45354a327b8b7d5423cb4b169daa49a3efe320c392173dc923dbc.
It starts uvicorn trafficxx.booking:app, selects the booking service account and
PGUSER=trafficxx-lab-booking, and requests two non-root, read-only-root replicas
on apps nodes. Resource/probe/spreading settings follow the verified Browse
Deployment. Its separate booking service is ClusterIP, not public ingress.

Upload trafficxx-booking-release.zip, which preserves the two manifest paths and
two helper-script paths, then run:

```bash
unzip -o ~/trafficxx-booking-release.zip -d ~/trafficxx-booking-release
cd ~/trafficxx-booking-release
bash scripts/deploy-booking.sh && bash scripts/check-booking-http.sh
```

The deployment helper validates selected subscription/context, applies only the
booking Deployment/service and waits for both replicas' rollout. The HTTP helper
uses its own localhost port forward on 18002, checks health/readiness and the
Booking OpenAPI title/POST route, then performs a GET for a random nonexistent
reservation with the simulated lab customer header. It requires the application
404 code RESERVATION_NOT_FOUND, not a generic router 404. This confirms the real
booking module and a reservation-read path; it does not POST a hold or commit
any reservation/outbox rows. Its port-forward process/log are cleaned up.

The user verified BOOKING_DEPLOYED with two Ready replicas on separate apps
nodes and zero restarts at rollout. The HTTP helper then failed at `set -euo
pipefail` because the release ZIP contained Windows CRLF line endings. The local
helper and ZIP have been corrected; a byte-level regression check now guards
all shipped shell helpers. In the existing Cloud Shell release folder, repair
the uploaded helper and rerun only the HTTP check:

```bash
cd ~/trafficxx-booking-release
sed -i 's/\r$//' scripts/check-booking-http.sh
bash scripts/check-booking-http.sh
```

The user subsequently supplied BOOKING_HTTP_OK: alive, ready, trafficXX Booking
API and application-specific missing-reservation status 404. The helper probes one service-selected pod;
rollout readiness separately covers both replicas. Booking remains private and
the customer header remains simulated. Real hold/retry/conflict/cancellation
tests are the next step, followed by worker roles and messaging.

PostgreSQL SELECT FOR UPDATE requires UPDATE on at least one selected-table
column as well as SELECT. The planned minimal grant is UPDATE(name) on events,
which also permits changing event display names at SQL level; it is not a
lock-only permission. Event ID/inventory type updates will remain excluded.
This tradeoff preserves the current tested row-lock protocol rather than
silently changing its consistency design. See the
[PostgreSQL privileges documentation](https://www.postgresql.org/docs/17/ddl-priv.html).

Booking's SQL role needs full reservation reads for the existing service queries.
Customer scoping is enforced by the API, not PostgreSQL row-level security, and
X-Customer-Id is still a forgeable lab identity header. Do not treat this role
mapping as verified production customer isolation or expose booking publicly.
Real allowed/denied operations and API correctness tests are subsequent stages.

## Local validation

The derived Job passed seven mocked scenarios: success, wrong client, wrong
subject, missing CA, unexpected administrator membership, SQL failure and token
failure, without fake token disclosure. YAML command-element types, embedded
Python syntax and ZIP contents are checked. The administrator SQL follows the
already verified migrator setup pattern and subsequently passed for booking in
Azure, including its own pod SQL login. Eight additional offline grant-policy
methods now pass (61 total suite methods), covering the required contract,
ownership, unexpected worker/outbox reads, protected-field updates, elevated
roles, destructive/reference access and missing columns/grants. The table-grant
Job and effective-privilege query subsequently passed in Azure. Nine new offline
permission-probe methods now pass (70 total suite methods), covering required
SQL sequencing, all unexpected-success rollbacks, exact denial-code handling,
no free inventory, failed claims/outbox writes, safe check labels, final rollback
before success, rollback on partial failure and identity mismatch. The driver/
SDK are stubbed; the corresponding real booking-role query probe subsequently
passed in Azure. Six additional HTTP validation methods pass (76 total suite
methods): expected API/read-only lookup, wrong module, missing route, unexpected
reservation, generic 404 rejection and readiness failure. Four stubbed deployment
scenarios also pass (success, wrong subscription/context and failed apply), plus
Bash syntax/manifest/bundle checks. The subsequent real rollout passed; HTTP
verification subsequently passed after the CRLF repair above. The offline suite now has 77 methods,
including the shell line-ending regression check. No new hosted CI, local
database-engine run or local Azure access is claimed yet.

## Test a real hold, retry and cancellation

Upload trafficxx-booking-flow.zip to Cloud Shell, then run:

```bash
unzip -o ~/trafficxx-booking-flow.zip -d ~/trafficxx-booking-flow
cd ~/trafficxx-booking-flow
bash scripts/check-booking-flow.sh
```

This uses a temporary localhost-only port forward to the internal Booking
service and a unique simulated customer/key. It creates a real two-ticket hold
on demo-general (201), repeats the identical request (200 with exactly the same
hold and expiry), rejects a changed quantity using the same key (409), checks a
different customer cannot retrieve it (404), cancels it twice and verifies the
original key returns the cancelled result instead of creating a new hold.
The user supplied BOOKING_FLOW_OK for reservation
`b8aca9c7-35fb-4eb6-9db8-fadb1094ec49`: duplicate_reused_hold,
changed_request_rejected, other_customer_hidden and cancelled_key_reused_result
were all true, with final_status=CANCELLED. This verifies the sequential real
HTTP flow through one service-selected Booking pod.

The experiment retains a cancelled reservation and HELD/CANCELLED outbox history.
It attempts cancellation on assertion failures after a reservation ID is known.
An ambiguous network failure can leave a hold; failure output includes the random
lab customer/key for inspection with the same key, and the five-minute hold
expires logically. Do not retry an unknown outcome with a new key. No worker is
running to drain these outbox messages yet. The check does not confirm payment,
measure availability restoration, prove concurrent/cross-replica correctness or
establish production authentication. Five offline failure-path methods pass;
all 82 offline methods pass, with Bash syntax and LF bundle validation.

## Concurrent duplicate requests across two replicas

Upload trafficxx-booking-concurrency.zip to Cloud Shell and run:

```bash
unzip -o ~/trafficxx-booking-concurrency.zip -d ~/trafficxx-booking-concurrency
cd ~/trafficxx-booking-concurrency
python3 scripts/check-booking-concurrency.py
```

The helper guards the Kubernetes context, checks Deployment/ReplicaSet/pod
ownership, requires two Ready Booking pods on separate nodes, and opens two
localhost-only forwards directly to their pod names. Each forward must confirm
its own port binding before any requests run. A two-thread barrier releases one
POST per pod with an identical random lab customer, key and two-ticket
GENERAL request. Expected status codes are one 201 and one 200 with an identical
reservation and expiry; both pods must read that same hold.

Before reporting success, the helper cancels the hold and verifies CANCELLED
through both pods. Unexpected results also attempt cancellation for every
returned reservation ID. If both create responses are lost, cleanup attempts to
resolve the outcome by replaying the original key; this can create a diagnostic
hold if no original create committed, which is immediately cancelled. If cleanup
cannot be verified, the test fails and prints random lab customer/key identifiers
for inspection. Do not reset inventory or retry an unknown outcome with a new
key. Logical five-minute expiry still applies to an uncancelled hold.

Expected output is BOOKING_CONCURRENCY_OK, http_statuses=[200,201], both pod
names, same_reservation_and_expiry=true, both_replica_reads=true and
final_status=CANCELLED. The user subsequently supplied BOOKING_CONCURRENCY_OK
for reservation `7944c2ac-77bb-4216-a477-630f83bd61e3`, statuses [200,201],
same_reservation_and_expiry=true, both_replica_reads=true and final_status=CANCELLED,
targeting booking-84b6d5dbfb-9zvvn and booking-84b6d5dbfb-c8t6l.
This creates retained
cancelled reservation/outbox history. It tests concurrent client submissions to
two explicit replicas; server scheduling/lock overlap is not instrumented, and
one pair does not establish performance, broad oversell safety or assigned-seat
contention. The seven added offline methods use fakes for endpoint responses,
cleanup and ownership validation; all 89 offline methods pass. No local Azure
account access or Docker restart was used.

## Different customers competing for one assigned seat

Upload trafficxx-booking-seat-contention.zip, then run in Cloud Shell:

```bash
unzip -o ~/trafficxx-booking-seat-contention.zip -d ~/trafficxx-booking-seat-contention
cd ~/trafficxx-booking-seat-contention
python3 scripts/check-booking-seat-contention.py
```

The bundle includes the shared pod-ownership/port-forward helper and the seat
experiment. It sends demo-seated/A1 requests through separate Booking pods,
using distinct random lab customers and idempotency keys. The client threads
start together. Expected results are one 201 HELD and one 409 SEAT_UNAVAILABLE.
Both replicas must read the winner's exact reservation. The helper then cancels
the winner and checks CANCELLED through both replicas before printing
BOOKING_SEAT_CONTENTION_OK. The user subsequently supplied that result for
reservation `c354ee8a-849c-4f6e-b598-4c4c815e85fb`: seat A1, statuses [201,409],
SEAT_UNAVAILABLE conflict, winning pod index 0, losing index 1,
both_replica_reads=true and final_status=CANCELLED. Targets were
booking-84b6d5dbfb-9zvvn and booking-84b6d5dbfb-c8t6l. Post-experiment Browse
availability remains pending; this result verifies cancellation readback.

This retains cancelled reservation/outbox history. A preoccupied seat causes
failure without cancelling another customer's reservation. Error cleanup resolves
unknown requests with their original customer/key before cancelling any known
hold, so a losing cleanup replay does not acquire an already released seat.
If a bug yields two winning IDs, both are cancelled. Unknown outcomes that cannot
be resolved or cancellation failures fail the test; use the printed base customer
and key with -seat-0 and -seat-1 suffixes for inspection. No inventory reset,
confirmation or payment is performed. Logical five-minute expiry applies to any
uncancelled hold. Post-test availability must be checked separately.

Six new offline methods cover distinct identities, success, two winners,
preoccupied seat, lost winner response, wrong conflict and failed cleanup;
all 95 offline methods pass. This is one pair of competing client submissions,
not a load benchmark or proof covering all inventory races. Server overlap is
not instrumented. No local Azure commands or Docker restart were used.

## Real five-minute expiry and seat reuse

After the seat-contention experiment, the user supplied BROWSE_DEMO_HTTP_OK
with demo-general=100 and demo-seated=100, health alive and readiness ready.
To run the next experiment, upload trafficxx-booking-expiry.zip:

```bash
unzip -o ~/trafficxx-booking-expiry.zip -d ~/trafficxx-booking-expiry
cd ~/trafficxx-booking-expiry
python3 scripts/check-booking-expiry.py
```

Keep this command running for a little over five minutes. It holds demo-seated/A1
through pod 0 and verifies a distinct customer through pod 1 gets SEAT_UNAVAILABLE.
It checks the configured hold duration is about five minutes, prints
WAITING_FOR_HOLD_EXPIRY, then waits 305 monotonic seconds after the first response.
No database timestamp or configured timeout is modified.

The second customer then retries the same previously rejected key and should
create a new hold on A1. That booking transaction lazily expires the old hold.
Both replicas must see the old reservation EXPIRED, and replaying its original
key must return that same expired reservation. Cleanup cancels the replacement
and checks final states through both replicas before BOOKING_EXPIRY_OK.
The user supplied WAITING_FOR_HOLD_EXPIRY (305 seconds), then BOOKING_EXPIRY_OK:
old reservation `a4c2c95c-2b45-447a-b80b-a3770bfed2f9` expired and replacement
`125a8dc9-8cc7-41cb-85ac-62efea4c16d4` acquired A1. blocked_before_expiry,
seat_rebooked_after_expiry, expired_key_reused_result and both_replica_reads
were true; replacement_final_status was CANCELLED and expiry_mechanism was
booking_transaction. Both named Booking replicas participated. This does not
verify a background expiry worker or Browse availability during the hold; no
expiry worker is deployed. The subsequent BROWSE_DEMO_HTTP_OK confirmed health
alive, readiness ready, two events and 100 available for both demo-general and
demo-seated after this experiment.

Cleanup resolves uncertain creates with the original identities/keys, cancels
known active holds, preserves expired history, and fails if cleanup is unverified.
A cleanup replay can create a diagnostic hold if the original create never
committed; that hold is then cancelled. Failure output contains base lab identifiers:
append -expiry-0 or -expiry-1 to both customer and key for inspection. Do not
reset inventory or use a new key to resolve an unknown outcome. Keep Cloud Shell
open; interruption may leave a hold until logical expiry. The experiment leaves
expired/cancelled reservations and outbox history. Five new offline methods use a
simulated clock to cover success, unchanged five-minute configuration, failed seat
reuse, wrong old status and cleanup failure; all 100 offline methods pass.
