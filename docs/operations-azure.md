# Operations and monitoring on Azure

The messaging experiments passed, including duplicate suppression, consumer
stop/start recovery and invalid-message isolation followed by valid processing.
The latest verified checkpoint has16outbox/inboxevents, eightlatestprojections,
a healthy consumer heartbeat and native queue active/dead_letters/total all zero.
The one observed_dead_letters SQL row is retained audit history from the invalid
message, not a currently queued dead letter.

We now prepare the operations workload, using its existing Azure identity and
Kubernetes service account. The internal operations Deployment and healthy HTTP
checks have now passed in Azure. No external monitoring scrape,
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
missing CA, admin, SQL failure and token failure. All272offline methods passed.
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
role/DDL flags. All272offline methods pass. Real operations-role COUNT(*), joined
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
All272offline methods pass. The real Azure allowed/denied result above is separate from these local tests.

## Updated monitoring logic and new image build

The source now separates active alert conditions from historical dead-letter
audit observations. These changes passed local checks and the subsequent Azure operations rollout
and healthy HTTP/metric checks. The ACR build below succeeded. SQL permissions and query expressions stay the same.

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
missing/invalid digest. It performs no deployment. The user supplied successful ACR run ca2 (41seconds), tag
operations-20261022222812-28732 and digest
sha256:dd4e53ba28a0cd1d052700b56f82bd023e09ec7731324b221bc793fc9d5b7dc9.
The operations Deployment is pinned to this reviewed digest.
The reference file is excluded from Git and the Docker build context.

Ten new monitoring-rule methods plus three readiness methods passed (256 total),
including absent/stale/error workers, recovery, historical counters, threshold
validation, metric labels and no raw errors. Real FastAPI HTTP smoke checks for
health/readiness/backlog/metrics passed with mocked SQL; exported text was parsed
by prometheus_client. Four mocked build-helper scenarios passed (success, wrong
subscription, build failure, bad digest), plus Bash syntax/LF. Local Docker was
not restarted; PostgreSQL/AMQP integration suites were not rerun locally. Hosted
CI status was not checked. Real Azure healthy HTTP and the subsequent active alert appearance/recovery
drill both passed, as recorded below.

## Deploy internal operations and check healthy HTTP/metrics

The prepared Deployment runs one operations replica on an apps node, with the
operations service account/PG role and expected heartbeat workers publisher and
consumer, threshold30seconds. It uses reviewed ACR digest
dd4e53ba28a0cd1d052700b56f82bd023e09ec7731324b221bc793fc9d5b7dc9. Startup/liveness
use /healthz; readiness uses /readyz and SELECT1. The container is non-root with
read-only root filesystem, dropped capabilities, bounded CPU/memory and /tmp.
The Service is ClusterIP on port80 to container8000, with no public ingress.
The helper applies this pinned manifest directly; it does not rebuild or select
a newer tag automatically, and operations-image.txt is not required to deploy.

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/deploy-operations.sh
bash scripts/check-operations-http.sh
```

Verified OPERATIONS_DEPLOYED and OPERATIONS_HTTP_OK from the user. One pod
operations-548fd6c9bf-9rd5c was Running/Ready with zero restarts on an apps node.
Health/readiness passed, outbox/processing pending and ages were zero, processed
count16, publish_failures_total1, processing_failures_total0, observed_dead_letters_total1.
Consumer/publisher ages were4.2994/0.793295seconds, present and error-free. Active
alerts were empty; historical_observations contained DEAD_LETTERS_PREVIOUSLY_OBSERVED.
Worker/alert metric samples passed and native_queue_counts_available remainedfalse. The checker uses a temporary localhost-only port forward,
then removes it. It requires health/readiness, zero pending/outstanding work,
no active alerts, fresh error-free publisher/consumer heartbeat records, complete
zero-state alert rules and matching exported presence/age/error/alert samples.
It accepts retained historical publish failures/dead-letter observations and
reports them separately. It also requires native_queue_counts_available=false;
these SQL-derived gauges must not be called native queue counts.

Eight new offline checker methods cover healthy history, wrong health/readiness,
backlog/active alerts, stale/missing/error workers, missing alert rules, dropped
history, inconsistent exported gauges and duplicate metric samples (264 total).
Four mocked deployment helper scenarios passed: success, wrong subscription,
wrong context, apply failure. Bash syntax/LF plus manifest digest/identity/module,
worker configuration, internal Service routing and security/probes were checked.
No local Azure account access, rollout, Docker restart or broker calls occurred.

## Verify alert appearance and recovery in the running lab

Run with other booking experiments idle and leave Cloud Shell open until it
finishes. The existing publisher, expiry and operations workloads stay running.

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/check-operations-alerts.sh
```

Expected OPERATIONS_ALERTS_OBSERVED: two processing-outstanding events aged at
least30seconds, a stale consumer heartbeat, fresh publisher heartbeat and both
PROCESSING_BACKLOG_AGING/CONSUMER_HEARTBEAT_STALE active in JSON and metrics.
The runner requires a healthy HTTP/metrics baseline before pausing. It reuses
the verified backlog drill's subscription/context/readiness/native-empty checks,
then pauses the consumer, creates and cancels one demo hold and observes two
queued events with zero consumer pods. A callback makes up to18observations at five-second intervals for the
aged/stale signals while the consumer remains paused. It makes only read-only
HTTP calls with a15second timeout per request during that wait and does not alter heartbeat timestamps or SQL rows.

The existing finally block restores one consumer replica on success, timeout,
HTTP failure or KeyboardInterrupt. Native queue drain and a fresh read-only SQL
inbox/projection observer must pass. The runner then makes up to12checks at five-second intervals for the
healthy HTTP snapshot and all worker/alert metrics to return to zero-alert state.
Final expected output OPERATIONS_ALERT_RECOVERY_OK includes before/during/after
monitoring and CONSUMER_BACKLOG_RECOVERY_OK evidence. From the current16event
checkpoint, expect18inbox/outbox rows and nine latest projections afterward.
The user supplied OPERATIONS_ALERTS_OBSERVED and OPERATIONS_ALERT_RECOVERY_OK
for cancelled reservation72d72373-d3ab-446d-9142-c9cf99969329:

| Stage | Verified evidence |
|---|---|
| Before | No backlog/alerts,16processed events, healthy HTTP/worker metrics |
| Paused | Zero consumer replicas/pods; native active2/DLQ0/total2 |
| Active signals | Processing outstanding2, oldest30.706597seconds; consumer heartbeat40.683745seconds old; publisher heartbeat1.018812seconds old |
| Alert export | PROCESSING_BACKLOG_AGING and CONSUMER_HEARTBEAT_STALE both verified in JSON and metric gauges |
| After | Consumer restored to one replica; native active0/DLQ0/total0; no active alerts and zero backlog |
| Stored state |18outbox/inbox rows, nine latest projections, zero missing/payload/projection/orphan mismatches and zero processing-failure records |
| Fresh workers | Consumer1.116569seconds, publisher0.444427seconds, both error-free |
| SQL observer | Read-only migrator, TLSv1.3; heartbeat1.201273seconds old |

The retained dead-letter audit count remained1 throughout and did not create an
active warning. publish_failures_total increased1to2 during the drill; all events
were eventually published and processed, but the cause of the extra send failure
has not been diagnosed. This is successful recovery evidence, not a claim of
error-free publication or an availability/latency SLO.

This drill changes no images or database grants. It retains the cancelled
reservation and its event history. As with the backlog test, hard Cloud Shell
termination can prevent finally cleanup; inspect/restore if the session is lost:

```bash
kubectl get deployment/consumer -n trafficxx
kubectl scale deployment/consumer -n trafficxx --replicas=1
kubectl rollout status deployment/consumer -n trafficxx --timeout=180s
```

Eight new offline methods cover joint JSON/metric evidence, historical audit not
counting as active alert evidence, bounded waits, full fake-cloud recovery,
monitoring timeout restoration, failed clearing and bad baseline before mutation.
All272offline methods pass, plus Bash syntax/LF checks. The successful user-run live evidence above is separate from those local tests.

## Remaining monitoring integration

### Watch the same drill in Grafana

The user has now verified the HTTPS dashboard and supplied screenshots of all
twelve panels. To observe the next controlled drill, set Grafana to Last15minutes
and refresh5seconds, then run in Cloud Shell:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/check-operations-alerts.sh --observe-seconds 45
```

The default observation window remains0 for existing callers. The optional
window accepts integer0-120seconds, validated before any pause. Once both API
warning flags/gauges appear, the runner prints GRAFANA_OBSERVATION_WINDOW and
keeps checking those signals for another45seconds. This gives the15second
Prometheus scraper time to collect multiple samples. It does not prove stored
scrapes itself; actual Grafana screenshots/history are separate evidence.

Expect processing outstanding2, active warning signals2, rising consumer
heartbeat age and processing event age. Publisher remains fresh. The existing
finally block restores the consumer if observation fails or is interrupted;
hard shell termination can still prevent cleanup. Keep the command running.
After restoration, require OPERATIONS_ALERT_RECOVERY_OK with native empty queue,
matching SQL state and clear current warnings. Historic chart spikes remain
visible after current flags clear. Starting from the latest18event snapshot,
expect20processed events afterward if no unrelated work occurred. Historical
dead-letter audits stay1; publication-failure history is cumulative and may
change if a retry happens. No password, image or SQL grant changes are made.

Three added offline methods cover the45second pause window, restoration after
observation failure, and rejection of invalid windows before mutation. All323
offline methods and Bash syntax checks passed. The new dashboard-observed drill
has not yet been run in Azure.

The [private Prometheus collector](prometheus-azure.md) passed stored scrape
verification; the [Grafana HTTPS dashboard](grafana-https-azure.md) passed API,
cookie session and user browser-rendering checks. Connect alert rules and notification routing
separately: API flags and exported metrics alone do not notify anyone. The
existing Log Analytics workspace alone does not collect application signals.
Native Service Bus count monitoring needs a separate read/management path;
operations has no broker send/receive role. No external notification delivery
is tested or enabled by this drill. Prometheus collection is separate and now verified.
