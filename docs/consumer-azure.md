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

Expected CONSUMER_ROLE_READY: CONNECT/USAGE true, CREATE/admin false. A separate
registration transcript was not supplied; the subsequent real workload login
confirmed the registered consumer identity. The existing human administrator helper uses verified TLS and a
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

Verified SQL_LOGIN_OK: trafficxx-lab-consumer, database trafficxx, SSL true,
TLSv1.3, admin false and the expected consumer client ID. The read-only SQL diagnostic uses the consumer service account's
projected identity, explicit SDK credential and verify-full TLS. It never falls
back to human credentials or prints tokens/raw SDK errors. Dependencies are
installed in the diagnostic init container, requiring package-network access;
the short-lived Job is bounded by a deadline and completion TTL. The user supplied
the successful Azure login result on 2026-10-23. No queue receive, peek, settlement or SQL table write occurs
at this stage.

Seven mocked scenarios passed: success, wrong client, wrong service account,
missing CA, administrator role, SQL failure and token failure, with no fake
token output. Manifest command elements, embedded Python and identity binding
were validated. All 178 offline methods pass; real cloud identity and
permission evidence are separate from those local checks.

## Grant the consumer its current SQL contract

The migrator/table owner applies these column grants in one transaction:

| Table | Access |
|---|---|
| consumer_inbox | SELECT/INSERT message_id and envelope; no UPDATE |
| reservation_updates | SELECT/INSERT reservation_id, event_id, status, last_outbox_id; UPDATE event_id, status, last_outbox_id, updated_at |
| worker_heartbeats | SELECT/INSERT name and last_error; UPDATE last_seen_at and last_error |
| worker_failures | INSERT message_id and error_code only |
| observed_dead_letters | INSERT queue_name, sequence_number, message_id and reason only |

Conflict keys, EXCLUDED values and the projection ordering predicate need SELECT
permissions. Database defaults supply generated IDs and timestamps; this action
grants no sequence privileges. Actual inserts/upserts under the consumer identity
will be tested next. No access to reservations, events, ticket_units, outbox or
the migration ledger is granted, nor DELETE/TRUNCATE/TRIGGER, REFERENCES or DDL.

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-consumer-grant-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py=scripts/bootstrap-azure-schema.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/jobs/postgres-consumer-grants.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-consumer-grants --timeout=300s
kubectl logs -n trafficxx job/postgres-consumer-grants -c grant
```

Verified CONSUMER_GRANTS_OK from the user-run migrator Job: the read columns
and five insert tables match the policy above, with reservation_access,
inventory_access, outbox_access and destructive_table_access all false. This grant-only
action skips migration payload files. It verifies migrator ownership, non-admin
role flags and effective permissions on every public table column, rejecting
extra or missing access, including inherited permissions. Success prints after
commit. Unexpected access rolls back this transaction; previously existing
grants are not automatically revoked.

These are column boundaries, not row isolation: the consumer can update any
projection or heartbeat row through granted columns. Application code enforces
message validation, duplicate identity/payload checks and outbox ordering.
SQL grants alone do not enforce those protocols. The grant Job uses the migrator
service account and does not read, consume or send broker messages.

Eight added offline methods check ownership, protected reads/inserts/updates,
every required permission/column, role flags and destructive/reference privileges.

## Verify actual consumer SQL operations

The consumer-role diagnostic checks 14 allowed operations: inbox insert,
duplicate suppression and payload readback; projection lookup/upsert/newer update,
older-event rejection and readback; heartbeat insert/conflict update/readback;
failure insert; and dead-letter record insert/duplicate suppression. Thirty
protected operations must fail with SQLSTATE 42501, using savepoints so one denial
does not abort the remaining checks. Constraint/network errors do not count as
permission denials; unexpected permission success is a failure.

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-consumer-permission-files -n trafficxx \
  --from-file=check-consumer-permissions.py=scripts/check-consumer-permissions.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/postgres-consumer-permissions.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-consumer-permissions-check --timeout=300s
kubectl logs -n trafficxx job/postgres-consumer-permissions-check -c check
```

Expected CONSUMER_PERMISSIONS_OK with diagnostic_rows_committed=false,
broker_messages_sent=0 and broker_messages_received=0. Actual Azure results are
pending. Explicit WorkloadIdentityCredential and verify-full TLS use the consumer's
own service account. The bounded diagnostic Job installs its pinned dependencies
in an init container, as earlier permission checks did.

The projection foreign key uses the previously verified expired A2 reservation
2a6f5566-9870-45aa-9927-a85fd8e5f3a9, configured as PROBE_RESERVATION_ID in the
manifest. If it is missing, the check fails instead of creating a reservation
or granting reservation access. It does not read or change the reservation.
It stages a projection starting above any existing projection version. An outer
rollback restores diagnostic changes before success prints. Generated failure
identity sequence increments can survive rollback and leave harmless gaps.

This tests SQL permissions and upserts, not the live consumer loop. No broker
calls or message settlement occur. Keep the consumer undeployed for this staged
check. Eight new offline methods cover positive steps, unexpected permission
success, wrong SQLSTATE, missing foreign-key fixture, partial failure, identity,
payload/ordering mismatch and rollback before success. Local tests do not replace
real PostgreSQL evidence.

## Next stages

After CONSUMER_PERMISSIONS_OK, add queue-scoped Data Receiver authorization and
verify private broker access with send denied before deploying the consumer.

The real consumer parses the event, atomically records the deduplication inbox
and projection update, then acknowledges the broker after the SQL commit. A
duplicate can be delivered again after acknowledgement/crash boundaries; its
stable identity and payload must agree with the inbox record. Projection updates
compare outbox IDs to avoid regressing state when older events arrive later.
This records reservation state changes, not automatic booking confirmation.
Actual consumer processing, duplicate suppression, projection consistency and
queue settlement remain unverified in Azure.
