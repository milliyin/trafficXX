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
admin=false and the expected client ID. Actual Azure output is pending.

Registration runs as the human administrator through the existing temporary
client helper. It validates exact Entra object/tenant/service mapping and flags
before reusing a role, rejecting mismatches. Registration and database grants
are separate transactions; rerunning validates mapping and completes remaining
grants. The diagnostic uses the operations projected identity and explicit SDK
credential, with no fallback to human/node credentials and no token/raw-error
output. It installs pinned dependencies before the bounded, non-root login check.

Seven mocked scenarios passed: success, wrong client, wrong service account,
missing CA, admin, SQL failure and token failure. All229offline methods passed.
These local checks do not substitute for real Azure login evidence.

## Next steps and current application limits

After login, derive exact column reads from trafficxx.operations.snapshot:
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
