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
missing CA, admin, SQL failure and token failure. All243offline methods passed.
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
role/DDL flags. All243offline methods pass. Real operations-role COUNT(*), joined
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

Expected OPERATIONS_PERMISSIONS_OK with four allowed paths: readiness,
backlog join/ages, count-only sources and worker heartbeat reads; 30 protected
operations must fail with SQLSTATE42501. Actual Azure output is pending. The
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
All243offline methods pass. Real allowed/denied evidence remains separate.

## Next steps and current application limits

The prepared policy follows trafficxx.operations.snapshot:
outbox created_at/published_at/message_id/publish_failures; consumer inbox
message_id; heartbeat name/last_seen_at/last_error; count-only access to failure
and dead-letter audit tables through narrow readable columns. No reservation,
inventory, payload/envelope reads, database writes, schema creation or broker
send/receive privileges are needed for these SQL-derived signals. Effective
permission verification and real allowed/denied SQL probes precede deployment.

The current operations API was built for local development and needs deliberate
Azure adaptations before being presented as operational alerting:

- /backlog and /metrics expose database-derived values, not native queue depth.
- observed_dead_letters_total is historical audit history. Existing
  DEAD_LETTERS_OBSERVED remains set after the diagnostic was removed; it must
  not be described as a current dead-letter queue alarm.
- Stale-heartbeat checks only cover recorded workers. A publisher/consumer
  that never wrote a heartbeat is currently absent, not flagged missing.
- Publisher and consumer emit heartbeats; expiry currently does not. Expected
  heartbeat checks must reflect the actual runtime behavior.
- /metrics exports aggregate counts/ages but currently omits worker heartbeat
  samples and alert flags; these require implementation for a metrics scraper.
- API alert flags alone do not deliver notifications. A collector, alert rules,
  routing and failure tests remain future integration work. The existing
  Log Analytics workspace alone does not collect these application signals.

Keep the operations endpoint internal while developing this lab. Validate a
healthy baseline, then deliberately age a consumer backlog to prove the signals
appear and clear after recovery. Native Service Bus count monitoring must be
integrated separately without granting operations permission to consume messages.
