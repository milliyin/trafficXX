# Consumer on Azure

The publisher's read-only database check reported 12 published outbox rows,
zero pending rows/failed attempts and a fresh error-free heartbeat. The user
then supplied Azure queue counts: active=12, dead_letters=0, total=12. These are
matching publisher/queue observations before consumer deployment, not proof of
consumer processing or exactly-once delivery.

## Register the consumer SQL identity

The existing managed identity is trafficxx-lab-consumer, with client ID
35057245-c3ec-4eba-b182-60549501df42 and object/principal ID
0cbc3689-5f58-4522-b1a5-4cb71eb79ef3. Its service account is consumer in namespace
trafficxx. These identifiers are public configuration, not credentials.

From browser Cloud Shell:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/run-postgres-admin-sql.sh database/azure/005-register-consumer.sql
```

Expected CONSUMER_ROLE_READY: CONNECT/USAGE true, CREATE/admin false. Real output
is pending. The existing human administrator helper uses verified TLS and a
temporary client. Registration verifies exact object/tenant/service mapping and
non-admin flags; an unexpected existing mapping is rejected. SQL registration
and database/schema grants are separate transactions, so a retry validates the
mapping and completes any remaining grants. It adds only CONNECT on trafficxx
and USAGE on public, no application-table grants or broker roles.

## Verify the consumer's own workload login

After registration succeeds:

```bash
kubectl apply -f kubernetes/checks/postgres-consumer-identity.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-consumer-identity-check --timeout=300s
kubectl logs -n trafficxx job/postgres-consumer-identity-check -c check
```

Expected SQL_LOGIN_OK, trafficxx-lab-consumer, database trafficxx, SSL true and
admin false. The read-only SQL diagnostic uses the consumer service account's
projected identity, explicit SDK credential and verify-full TLS. It never falls
back to human credentials or prints tokens/raw SDK errors. Dependencies are
installed in the diagnostic init container, requiring package-network access;
the short-lived Job is bounded by a deadline and completion TTL. Actual Azure
login is pending. No queue receive, peek, settlement or SQL table write occurs
at this stage.

Seven mocked scenarios passed: success, wrong client, wrong service account,
missing CA, administrator role, SQL failure and token failure, with no fake
token output. Manifest command elements, embedded Python and identity binding
were validated. All 162 existing offline methods pass; real cloud identity and
permission evidence are separate from those local checks.

## Next stages

Derive SQL grants from process_event/apply_projection, heartbeat, record_failure
and record_dead_letter; test actual allowed/denied operations. Then add only
queue-scoped Data Receiver authorization and verify private broker access with
send denied before deploying the consumer.

The real consumer parses the event, atomically records the deduplication inbox
and projection update, then acknowledges the broker after the SQL commit. A
duplicate can be delivered again after acknowledgement/crash boundaries; its
stable identity and payload must agree with the inbox record. Projection updates
compare outbox IDs to avoid regressing state when older events arrive later.
This records reservation state changes, not automatic booking confirmation.
Actual consumer processing, duplicate suppression, projection consistency and
queue settlement remain unverified in Azure.
