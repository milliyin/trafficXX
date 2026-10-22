# Expiry worker on Azure

The user reconnected on 2026-10-23 and verified all four AKS nodes Ready.
Browse and Booking each still had two Ready Running replicas and zero restarts.
The previous real five-minute expiry experiment passed using a booking
transaction. The expiry login, grants and actual permission probe subsequently
passed. The user subsequently supplied logs from deployment/expiry-worker showing
successful workload authentication and no visible sweep errors. The subsequent controlled idle-hold experiment verified Deployment readiness
and actual background expiry with read-only stored-state evidence.

## Register the worker's database identity

The existing service account is `expiry` in namespace `trafficxx`. Its Azure
identity is `trafficxx-lab-expiry`, with client ID
`87cf111e-64b3-4455-8704-7cde6aca1352` and principal/object ID
`61c68925-9feb-4d45-9c06-cc55548605f3`. These identifiers are public configuration,
not credentials. SQL registration uses the object ID; token exchange uses the
client ID.

Run in browser Cloud Shell Bash after reconnecting to the existing cluster:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/run-postgres-admin-sql.sh database/azure/003-register-expiry.sql
```

This uses your administrator Entra login via the existing temporary SQL client
helper, with certificate-verified TLS. It registers the role in `postgres`, then
grants database CONNECT and public-schema USAGE in `trafficxx`. Existing mappings
must match the expected object/tenant/type and non-admin flags; mismatches abort.
Schema/database CREATE and server administrator privileges are rejected. The
two transactions commit separately; if the second fails, registration can remain
and a rerun validates the mapping before retrying grants.

Expected result: `EXPIRY_ROLE_READY`, CONNECT/USAGE true, CREATE/admin false.
No application-table grants, migration replay, demo seed or worker deployment
are part of this stage. The subsequent successful expiry workload login confirms
the role is registered; a separate EXPIRY_ROLE_READY transcript was not supplied.

## Verify its own workload login

After registration succeeds:

```bash
kubectl apply -f kubernetes/checks/postgres-expiry-identity.yaml
kubectl wait --for=condition=complete job/postgres-expiry-identity-check \
  -n trafficxx --timeout=300s
kubectl logs -n trafficxx job/postgres-expiry-identity-check -c check
```

Expected: `SQL_LOGIN_OK`, identity `trafficxx-lab-expiry`, database `trafficxx`,
SSL true and admin false. The user supplied real SQL_LOGIN_OK on 2026-10-23:
trafficxx-lab-expiry, trafficxx, TLSv1.3, admin=false and the expected client ID.
This diagnostic uses the expiry service account's
projected identity, explicit SDK credential and verify-full TLS; no human-token
fallback is used. It runs read-only diagnostic SQL and prints no token or raw
SDK error. Table permissions and worker deployment remain separate stages.

If the Job fails, inspect the `check` and `install-dependencies` container logs.
The diagnostic installs pinned top-level SDK/driver versions from package
repositories; it requires outbound package access. Successful Jobs expire after
ten minutes. If an existing failed/completed Job must be rerun, delete only that
diagnostic Job before applying again. Do not recreate the database or identities.

## Next stages and validation

After login is verified, derive grants from `trafficxx.service.sweep` and
`expire_event`, test allowed operations and denied privileges, then deploy the
worker using workload identity. It needs to release expired assignments and
insert expiry outbox records; it must not acquire booking or publisher privileges
merely because they are convenient. A real idle-hold test must distinguish
worker cleanup from API-triggered expiry before background expiry is claimed.

The derived login probe passed seven mocked scenarios: success, wrong client,
wrong service account, missing CA, administrator role, SQL failure and token
failure. YAML command element types and embedded Python syntax passed, with no
fake token output. All 100 existing offline tests pass. These are local checks;
the real worker SQL login, grants and worker-role query checks subsequently
passed. Worker deployment is the current next step.

## Grant the current sweep SQL permissions

Run these commands as a new action from the synced repository:

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-expiry-grant-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py=scripts/bootstrap-azure-schema.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/jobs/postgres-expiry-grants.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-expiry-grants --timeout=300s
kubectl logs -n trafficxx job/postgres-expiry-grants -c grant
```

The existing migrator/table owner runs `--grant-expiry` in one transaction;
success is printed only after commit. The grant action does not read migration
payloads or rerun migrations/seeding. Expected result is EXPIRY_GRANTS_OK;
the user supplied real EXPIRY_GRANTS_OK: grantee trafficxx-lab-expiry,
read_tables=[events,reservations], outbox_read=false, worker_table_access=false
and destructive_table_access=false. This verifies the grant transaction and
effective privilege checks; actual worker-role queries are tested below.

The current worker uses SELECT * on events for row locking and UPDATE RETURNING *
on reservations, so it receives full SELECT on those two tables. This includes
reservation customer fields; it is not column-level customer-data isolation.
It gets SELECT(reservation_id) and UPDATE(reservation_id) on ticket_units,
UPDATE(status) on reservations and INSERT(reservation_id,event_type,payload)
on outbox. It gets UPDATE(name) on events to support SELECT FOR UPDATE; this also
allows SQL-level display-name edits. No reservation INSERT, ticket label reads,
outbox reads/publication updates, worker-table grants, DDL or destructive table
grants are added. Column-level grants do not enforce an EXPIRED-only transition
or expiry predicate: the worker code enforces that condition. A narrower SQL
interface would require a separate reviewed design change.

Effective privilege checks reject excessive inherited or direct access across
current public table columns, elevated flags and schema/database CREATE. Required
columns/grants and migrator ownership are verified. Seven added offline methods
cover the contract, incorrect ownership, protected reads/writes, elevated/DDL
access, missing grants/columns and destructive/reference privileges; all 107
offline methods pass. Database-engine positive/negative expiry-role tests are
the next stage before deployment; this grant Job is not those tests.

## Test actual worker-role SQL permissions

From the synced Cloud Shell clone:

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-expiry-permission-files -n trafficxx \
  --from-file=check-expiry-permissions.py=scripts/check-expiry-permissions.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/postgres-expiry-permissions.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-expiry-permissions-check --timeout=300s
kubectl logs -n trafficxx job/postgres-expiry-permissions-check -c check
```

Expected: EXPIRY_PERMISSIONS_OK with nine allowed operations, fourteen denials
with SQLSTATE 42501, diagnostic_rows_committed=false, SSL true and admin false.
The user supplied real EXPIRY_PERMISSIONS_OK: nine allowed operations, fourteen
42501 denials, diagnostic_rows_committed=false, trafficxx, TLSv1.3 and admin=false.
The probe authenticates as expiry, never migrator.

The probe locks demo-seated and an existing expired demo reservation. It refuses
to proceed if no expired history is available or that row still owns inventory.
It temporarily marks that expired row HELD inside an uncommitted transaction,
without changing its past expiry timestamp, finds it with the sweep's due query,
and conditionally marks it EXPIRED again. It tests assignment lookup/release SQL
permissions and outbox INSERT/default identity generation without SELECT/RETURNING.
A unique diagnostic outbox event type avoids colliding with existing real EXPIRED
history. No diagnostic row or state transition is committed.

The release query matches zero rows because this fixture has no assigned ticket;
this is permission evidence, not actual worker-driven inventory release. The
outbox event is a diagnostic type, not proof of a committed EXPIRED message.
Background expiry and its actual inventory/message effects need a later real
worker experiment. Identity sequence counters can advance on rollback normally;
no sequence grant or reset is added.

Protected operations include reservation INSERT, ticket label reads/updates,
changing customer/hold duration/event inventory, adding inventory, deleting
reservations, reading/publishing outbox, worker/migration table reads and DDL.
Each denied operation has a savepoint and must yield 42501. Unexpected successes
are rolled back and fail the probe. The outer transaction is rolled back before
success output, or on failure. Eight additional offline methods cover required
queries/denials, unexpected success, wrong SQLSTATE, missing/bound history,
failed expiry update, final rollback, failed outbox and wrong identity. All 115
offline methods pass; the real Azure permission-query result subsequently passed.

## Deploy one background expiry worker

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/deploy-expiry.sh
kubectl logs -n trafficxx deployment/expiry-worker --tail=50
```

Expected helper output is EXPIRY_DEPLOYED after rollout. A separate helper
transcript was not supplied; the subsequent experiment verified the existing
worker Deployment Ready and stable before and after a successful expiry. This reuses the digest-pinned image already running both APIs, which
contains trafficxx.expiry and workload-identity database connections. No image
build, Service Bus setup or HTTP Service is needed. The helper checks the
subscription/context before applying only the expiry Deployment.

One apps-node replica uses service account expiry and PGUSER trafficxx-lab-expiry.
It runs python -u -m trafficxx.expiry, sweeping every five seconds, with a non-root
user, read-only root, dropped capabilities, bounded resources and temporary
writable storage. Recreate updates prevent overlap of old/new worker replicas;
this is a single-replica lab worker, not worker HA.

Readiness executes a sanitized database connection/SELECT 1 check every thirty
seconds. It verifies authentication/connectivity, not loop progress or sweep
permissions. There is no sweep heartbeat or application liveness probe; a stuck
loop is not automatically detected. The existing loop retries psycopg errors
after five seconds and logs errors; it logs a count when it expires reservations.
Empty logs with no due holds can be normal. Rollout and quiet logs do not prove
successful background expiry.

Four mocked deployment cases passed: success, wrong subscription/context and
apply failure. Bash syntax/LF, manifest identity/image/module/security/probe
structure and readiness success/failure paths passed. All 115 offline methods
pass. No new image scan or local database-engine run was performed. After rollout,
create an idle hold and inspect its raw stored state without an API request that
could trigger lazy expiry; verify worker-driven EXPIRED, assignment release and
an expiry outbox row before claiming successful background cleanup.

## Verify idle expiry using raw stored state

The supplied worker logs contain SDK INFO output, including successful token
requests. They do not show access-token values. Repeated get_token successes can
use the SDK cache; the first HTTP exchange and subsequent success messages do
not prove a network exchange on every sweep. They also do not prove that any
reservation was expired. Container log timestamps are recorded as supplied;
client-facing checkpoint date is 2026-10-23.

Run from Cloud Shell after syncing the repository:

```bash
cd ~/trafficxx
git pull --ff-only origin main
python3 scripts/check-expiry-worker.py
```

Keep this command running for a little over five minutes, and leave other
Booking experiments/API requests idle during the test. The script uses the
existing two-pod connection helper, verifies the expiry Deployment is Ready
with the reviewed image/command/service account, then creates an A2 hold.
It creates a short-lived observer Job from the same image using the migrator
service account. That observer's first SQL statement is SET TRANSACTION READ ONLY;
it queries stored reservation, assignment and outbox state without importing or
calling reservation/expiry service functions.

Baseline must show HELD, one matching A2 assignment, one HELD outbox row and no
EXPIRED outbox row. The helper prints WAITING_FOR_WORKER_EXPIRY and waits 310
monotonic seconds after that baseline. It makes no Booking/Browse calls in this
interval or during the final observation. A second read-only Job must find
EXPIRED, zero assignments and exactly one EXPIRED outbox row. The Deployment
UID/generation must be unchanged and still Ready. Only then EXPIRY_WORKER_OK is
printed. The user supplied the baseline HELD observation and then real
EXPIRY_WORKER_OK for reservation 2a6f5566-9870-45aa-9927-a85fd8e5f3a9: final
status EXPIRED, remaining_assignments=0, held_outbox_rows=1,
expired_outbox_rows=1, observation=read_only_sql and
expiry_triggering_api_calls_after_create=0. Baseline had one assignment, one
HELD message and zero EXPIRED messages. The worker Deployment was Ready and
unchanged through the check. This controlled lab experiment assumes
no other actor triggers expiry for this event; it does not instrument the
database writer identity or establish a background-expiry latency SLO.

Observer Jobs are named uniquely, removed afterward and bounded by deadlines/TTL.
They use the existing non-admin table-owning migrator identity only for reads;
the worker receives no extra permissions. The expiry helper and observer files
must both be present in scripts/. The worker's existing immutable image contains
the database connection code; the observer code is passed as the Job command,
so no image rebuild or package installation is needed.

On experiment failure only, cleanup may replay the original customer/key and
cancel/expire the diagnostic hold through Booking. Such cleanup never counts as
worker evidence. If cleanup cannot be verified, inspect the reported original
key; do not reset inventory or submit a new key. Successful experiments retain
expired reservation and HELD/EXPIRED outbox history, not delivery to Service Bus.
Post-test Browse availability is a separate check. Seven added offline methods
cover read-only SQL, wrong metadata/state/counts, observer Job isolation,
no API calls after create, failed-observer cleanup and Deployment changes;
all 122 offline methods pass with a simulated wait. The corresponding real
background expiry experiment subsequently passed. Continuous availability,
precise expiry latency, sustained token renewal and outbox delivery are still
unverified. Check final Browse availability separately.
