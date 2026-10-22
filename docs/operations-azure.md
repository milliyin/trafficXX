# Operations and monitoring on Azure

The messaging experiments passed, including duplicate suppression, consumer
stop/start recovery and invalid-message isolation followed by valid processing.
The latest verified checkpoint has16outbox/inboxevents, eightlatestprojections,
a healthy consumer heartbeat and native queue active/dead_letters/total all zero.
The one observed_dead_letters SQL row is retained audit history from the invalid
message, not a currently queued dead letter.

We now prepare the operations workload, using its existing Azure identity and
Kubernetes service account. It is not deployed yet. No external monitoring scrape,
alert delivery or Azure telemetry collection is enabled by this identity setup.

## Register and verify the operations database identity

The managed identity trafficxx-lab-operations already exists, client ID
8e14cc93-46e3-466d-bec1-5dda5b3cc4ef, principal/object ID
8b0ecff7-b059-4af5-97b4-165598331eb9. Its service account is operations in namespace
trafficxx. These IDs are public configuration. Registration grants only CONNECT
on trafficxx and USAGE on public; no table privileges or broker rights.

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/run-postgres-admin-sql.sh database/azure/006-register-operations.sql
kubectl apply -f kubernetes/checks/postgres-operations-identity.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-operations-identity-check --timeout=300s
kubectl logs -n trafficxx job/postgres-operations-identity-check -c check
```

Expected OPERATIONS_ROLE_READY with CONNECT/USAGE true and CREATE/admin false,
then SQL_LOGIN_OK as trafficxx-lab-operations over certificate-verified TLS,
admin=false and the expected client ID. The user supplied SQL_LOGIN_OK as trafficxx-lab-operations, database trafficxx,
TLSv1.3, admin=false and expected client ID. A separate OPERATIONS_ROLE_READY
transcript was not supplied.

Registration runs as the human administrator through the existing temporary
client helper. It validates exact Entra object/tenant/service mapping and flags
before reusing a role, rejecting mismatches. Registration and database grants
are separate transactions; rerunning validates mapping and completes remaining
grants. The diagnostic uses the operations projected identity and explicit SDK
credential, with no fallback to human/node credentials and no token/raw-error
output. It installs pinned dependencies before the bounded, non-root login check.

Seven mocked scenarios passed: success, wrong client, wrong service account,
missing CA, admin, SQL failure and token failure. All256offline methods passed.
These local checks do not substitute for real Azure login evidence.

## Grant column-scoped monitoring reads

The migrator/table owner grants only SELECT on these columns:

| Table | Readable columns |
|---|---|
| outbox | created_at, published_at, message_id, publish_failures |
| consumer_inbox | message_id |
| worker_heartbeats | name, last_seen_at, last_error |
| worker_failures | id |
| observed_dead_letters | queue_name |

The single-column grants on the two count-only sources allow COUNT(*) while
excluding error/message details. These columns themselves remain readable; this
is column-level access, not an aggregate-only view or row-isolation boundary.
No payload/envelope, reservation, event, inventory, projection or ledger access
is granted. INSERT/UPDATE/DELETE/TRUNCATE/TRIGGER/REFERENCES and DDL remain denied.
See PostgreSQL's [SELECT privilege rules](https://www.postgresql.org/docs/17/sql-select.html).

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-operations-grant-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py=scripts/bootstrap-azure-schema.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/jobs/postgres-operations-grants.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-operations-grants --timeout=300s
kubectl logs -n trafficxx job/postgres-operations-grants -c grant
```

Expected OPERATIONS_GRANTS_OK with the exact read_columns policy and writes=false,
payload_access=false, reservation_access=false, inventory_access=false. Actual
Azure OPERATIONS_GRANTS_OK was supplied by the user, with the exact five-table
column policy above and all write/payload/reservation/inventory/destructive flags
false. The grant-only action requires migrator ownership,
checks effective role flags and all public table columns for extra/missing access,
including inherited privileges, and prints success only after commit. It skips
migration payload files and adds no broker role or runtime deployment. Previously
existing unexpected rights cause rollback rather than automatic revocation.

Six added offline methods cover exact SELECT-only grants, ownership, protected
reads, all writes/reference/destructive rights, every required read/column and
role/DDL flags. All256offline methods pass. Real operations-role COUNT(*), joined
backlog/age reads, heartbeat queries and negative SQL tests follow after this
Job succeeds. They are separate from catalog-based effective-grant checks.

## Verify real monitoring queries and protected operations

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-operations-permission-files -n trafficxx \
  --from-file=check-operations-permissions.py=scripts/check-operations-permissions.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/postgres-operations-permissions.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-operations-permissions-check --timeout=300s
kubectl logs -n trafficxx job/postgres-operations-permissions-check -c check
```

Verified OPERATIONS_PERMISSIONS_OK from the user with four allowed paths: readiness,
backlog join/ages, count-only sources and worker heartbeat reads; 30 protected
operations failed with SQLSTATE42501. No diagnostic rows were committed or
broker messages sent/received; operations identity used TLSv1.3, admin=false.
Its snapshot had zero unpublished/processing backlog and age, processed_total=16,
processing_failures_total=0, publish_failures_total=1 and observed_dead_letters_total=1.
Publisher/consumer ages were0.654274/0.347912seconds with no current errors.
The two nonzero counters are historical observations, not current failure flags. The
three monitoring SQL expressions match the existing operations.snapshot queries,
including COUNT(*) under column-only grants. The diagnostic does not invoke the
HTTP app or claim its alert logic is fixed. It does not require a zero backlog:
its purpose is query permission verification, not a healthy-state assertion.

The result includes numeric aggregate metrics and worker names/ages/error-presence
booleans. Worker raw errors, message bodies and tokens are not printed. Historical
observed_dead_letters_total should be1 after the completed invalid-message test;
that is not a native DLQ count. native_queue_counts_available remainsfalse.

Negative probes cover protected booking/inventory/payload/envelope/projection/
ledger reads, inserts/updates/deletes across monitored tables and DDL. Each uses
a savepoint and must return42501; unrelated SQL errors and unexpectedly allowed
operations fail the diagnostic. The transaction is READ WRITE solely so read-only
transaction errors cannot masquerade as privilege denials; all positive queries
are SELECT, and the whole transaction is rolled back before success. No diagnostic
rows are committed, no broker is contacted and no additional grants are applied.

Eight new offline methods cover allowed queries, all unexpected permission
successes, wrong SQLSTATE, invalid metric/heartbeat values, raw-error redaction,
outer rollback/verified TLS, positive-query failure and wrong injected identity.
All256offline methods pass. The real Azure allowed/denied result above is separate from these local tests.

## Updated monitoring logic and new image build

The source now separates active alert conditions from historical dead-letter
audit observations. These changes have passed local checks but are not yet
built/deployed in Azure. SQL permissions and query expressions stay the same.

- Configure EXPECTED_HEARTBEAT_WORKERS=publisher,consumer on the Azure operations
  Deployment. Missing expected workers produce NAME_HEARTBEAT_MISSING even if
  no heartbeat row has ever existed. Only actual heartbeat emitters are expected;
  expiry does not currently write one. Empty/default configuration retains local
  mode without requiring worker processes.
- Recorded workers reaching BACKLOG_ALERT_SECONDS (default30) produce
  NAME_HEARTBEAT_STALE; a recorded error produces NAME_ERROR. Fresh/error-free
  heartbeats clear those signals. Raw error values are replaced by has_error.
- Outbox/processing backlog alerts require nonzero work and age at least the
  configured positive finite threshold. No backlog clears these alerts.
- observed_dead_letters_total remains a historical counter, with
  DEAD_LETTERS_PREVIOUSLY_OBSERVED under historical_observations. It no longer
  generates an always-on live DEAD_LETTERS_OBSERVED alert after cleanup.
- /metrics now exports worker_heartbeat_present, worker_heartbeat_age_seconds,
  worker_has_error and alert_active gauges with worker/alert labels, in addition
  to existing aggregate metrics. Missing workers export presence0 and no fake
  age value. Every rule exports a zero/one sample so recovery is visible.
- /backlog and /metrics still derive from PostgreSQL, not native Service Bus
  queue depth. native_queue_counts_available=false remains explicit.

Operations readiness now uses SELECT1. The prior shared helper queried events,
which the operations role correctly cannot read. The helper accepts a query
option; Browse/Booking retain their existing default. Snapshot SQL runs in a
REPEATABLE READ, READ ONLY transaction, with no new grants required.

Build the updated runtime image using ACR from Cloud Shell:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/build-operations-image.sh
```

Expected IMAGE_BUILD_OK and a digest-pinned reference saved to ignored
operations-image.txt. The helper checks subscription/registry and rejects a
missing/invalid digest. It performs no deployment. Actual Azure build/digest
is pending; share the reference before configuring the operations rollout.
The reference file is excluded from Git and the Docker build context.

Ten new monitoring-rule methods plus three readiness methods passed (256 total),
including absent/stale/error workers, recovery, historical counters, threshold
validation, metric labels and no raw errors. Real FastAPI HTTP smoke checks for
health/readiness/backlog/metrics passed with mocked SQL; exported text was parsed
by prometheus_client. Four mocked build-helper scenarios passed (success, wrong
subscription, build failure, bad digest), plus Bash syntax/LF. Local Docker was
not restarted; PostgreSQL/AMQP integration suites were not rerun locally. Hosted
CI status and real Azure HTTP/alert behavior are not yet verified for this change.

## Work remaining after the image build

Deploy the operations endpoint internally with its own read-only identity and
explicit expected-worker configuration. Check healthy HTTP/metrics, then age a
controlled consumer backlog and prove active alert gauges appear and clear after
recovery. Connect a metrics scraper/collector, alert rules and notification
routing separately: API flags and exported metrics alone do not notify anyone.
The existing Log Analytics workspace alone does not collect application signals.
Native Service Bus count monitoring also needs a separate read/management path;
operations has no broker send/receive role. No external alert service or cloud
telemetry collection has been enabled by the source changes above.
