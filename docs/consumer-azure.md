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
were validated. All 194 offline methods pass; real cloud identity and
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

Verified CONSUMER_PERMISSIONS_OK from the user: all 14 allowed operations and
all 30 denials passed with SQLSTATE42501, diagnostic_rows_committed=false,
broker_messages_sent=0 and broker_messages_received=0. Identity was
trafficxx-lab-consumer, database trafficxx, TLSv1.3, admin=false. Explicit WorkloadIdentityCredential and verify-full TLS use the consumer's
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

## Plan the consumer queue role

The foundation now prepares one additional resource:
azurerm_role_assignment.consumer_servicebus_receiver. It grants Azure Service
Bus Data Receiver to the existing consumer managed identity, principal ID
0cbc3689-5f58-4522-b1a5-4cb71eb79ef3, scoped to the reservation-events queue ID.
No Data Sender or Data Owner role is added. Existing publisher authorization
remains queue-scoped Data Sender. See Microsoft's
[Service Bus role and scope documentation](https://learn.microsoft.com/en-us/azure/service-bus-messaging/authenticate-application).

From the existing initialized Cloud Shell checkout:

```bash
cd ~/trafficxx
git pull --ff-only origin main
export ARM_SUBSCRIPTION_ID=35aaf788-f4cf-40c3-9ea5-bc8fe8708a85
export TF_VAR_subscription_id="$ARM_SUBSCRIPTION_ID"
export TF_VAR_location=eastus
export TF_VAR_registry_name=trafficxxacre8708a85
export TF_VAR_aks_admin_object_id="$(az ad signed-in-user show --query id -o tsv)"
export TF_VAR_postgres_admin_principal_name="$(az ad signed-in-user show --query userPrincipalName -o tsv)"
cd ~/trafficxx/infrastructure/foundation
terraform validate
terraform plan -out=consumer-receiver.tfplan
```

If this shell has lost its Terraform initialization, follow the existing
[publisher guide's backend recovery instructions](publisher-azure.md#prepare-private-azure-service-bus-infrastructure)
using the same lab/foundation.tfstate key; never initialize an empty replacement
state. Expected plan: one addition, zero changes and zero destroys, only the
consumer queue receiver assignment. Review the full plan before applying; any
other mutation needs investigation. The user supplied the planned receiver role:
expected consumer principal, ServicePrincipal type, exact Data Receiver role and
exact reservation-events queue scope. They then supplied foundation Outputs after
the apply instruction, reconfirming namespace, queue, endpoint and identity IDs;
a separate Apply complete summary was not included. Actual broker authorization
is tested next. Do not reuse an old saved plan after state changes or target away
unrelated mutations.

Offline Terraform 1.16.6/AzureRM 5.10.0 formatting and validation passed in an
isolated copy without the backend; no local Azure login, state or plan access.
This assignment enables authorization only; it does not start a consumer or
settle queue messages. After the reviewed plan is applied, verify private broker
connectivity and receiving/peek authorization with send denied before deployment.
RBAC propagation may take up to five minutes according to the documentation.


## Check consumer private broker access

This diagnostic runs under the consumer service account with explicit
WorkloadIdentityCredential and the existing application image. It requires DNS
and a CA/SNI-verified TLS peer in 10.40.17.0/24, then opens the main queue receiver
and peeks at most one message. It repeats peek on the dead-letter subqueue,
which the runtime consumer also observes. Both use prefetch_count=0, without
receive_messages, iteration, locking, settlement or SQL writes. Message contents
and credentials are not printed. Empty queues are valid for this access test.

It then tries opening the sender link and creating an unsent batch. Only a
ServiceBusAuthorizationError counts as the expected denial. An unexpectedly
successful sender link, network/timeout error or failed receive permission fails
the diagnostic. No send_messages call exists, even if sender access unexpectedly
succeeds. This is link/peek authorization evidence, not consumer processing proof.
See the SDK [peek documentation](https://learn.microsoft.com/en-us/python/api/azure-servicebus/azure.servicebus.servicebusreceiver?view=azure-python)
and [dead-letter receiver option](https://learn.microsoft.com/en-us/python/api/azure-servicebus/azure.servicebus.servicebusclient?view=azure-python).

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap servicebus-consumer-check-files -n trafficxx \
  --from-file=check-servicebus-consumer.py=scripts/check-servicebus-consumer.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/servicebus-consumer-access.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/servicebus-consumer-access-check --timeout=180s
kubectl logs -n trafficxx job/servicebus-consumer-access-check -c check
```

Verified SERVICEBUS_CONSUMER_ACCESS_OK from the user: private DNS/TLS peer
10.40.17.4, TLSv1.3, receiver_link_open=true, queue_peek_allowed=true,
dead_letter_peek_allowed=true, send_denied=true and zero broker messages sent
or received, with the expected consumer client ID. If role propagation
causes an authorization failure, wait up to five minutes and recreate only this
short-lived diagnostic Job before retrying; do not broaden the role.

Eight added offline methods cover correct identity/network/TLS, main/DLQ denial,
sender authorization denial at link/batch creation, unexpected send access,
timeouts and raw error redaction. Tests assert zero prefetch and no calls to
receive/send/complete/abandon/dead-letter operations. All 194 offline methods
pass; real Azure broker evidence remains separate. Consumer deployment and
end-to-end queue drain/projection checks follow only after this check passes.


## Deploy the continuous consumer

The Deployment runs one Recreate replica as consumer on an apps node, using
its own PostgreSQL role and queue receiver authorization. It reuses the existing
ACR image digest and runs python -u -m trafficxx.consumer. The broker credential
chain is restricted to WorkloadIdentityCredential; no connection string or human
credential fallback is configured. Security/resource settings match the publisher:
non-root user, read-only root filesystem, dropped capabilities, bounded CPU/memory
and a size-limited writable /tmp. No HTTP Service is created.

**This starts real message processing.** Already-published historical events will
be read from reservation-events, their inbox/projection changes committed, then
acknowledged. A historical HELD event does not reactivate a reservation; this
consumer updates the downstream status view, not the reservation itself. The
outbox-ID guard protects the view against older events arriving after newer ones.
Invalid messages can be dead-lettered; database failures follow bounded delivery
retries. Stop other diagnostic mutations while evaluating this first rollout.

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/deploy-consumer.sh
kubectl logs -n trafficxx deployment/consumer --tail=50
```

Expected CONSUMER_DEPLOYED and event_delivery outcome=processed or duplicate
logs. The user supplied 12 processed-message log lines, including the first
publisher message 151af373-f74d-4bc4-8c14-e3b6cf13aa51. A separate rollout
summary was not supplied; complete persisted processing remains to be checked. A pod that is Running/Ready or
emits some processed logs does not prove complete drain or projection agreement.
The readiness probe checks only a database SELECT 1; it does not verify broker
connectivity, heartbeat freshness or forward progress. There is no loop-progress
liveness probe. Recreate updates can pause this single consumer temporarily;
queued events remain broker work, subject to the configured TTL/delivery policy.

Four mocked deploy-helper scenarios passed: success, wrong subscription, wrong
cluster context and apply failure. Bash syntax/LF, manifest image/module/identity,
credential configuration, security/resource settings and readiness Python syntax
were checked. All 194 offline methods pass. After successful deployment, verify
persisted inbox coverage, projection agreement and a fresh error-free heartbeat
with a read-only observer, then obtain native active/dead-letter queue counts.


## Verify stored inbox coverage and latest projections

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap consumer-drain-check-files -n trafficxx \
  --from-file=check-consumer-drain.py=scripts/check-consumer-drain.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/consumer-drain.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/consumer-drain-check --timeout=180s
kubectl logs -n trafficxx job/consumer-drain-check -c check
```

Verified CONSUMER_DRAIN_OK from the user: total_count=12, pending_count=0,
inbox_count=12, missing_inbox_count=0, payload_mismatch_count=0,
expected_projection_count=6, projection_mismatch_count=0, orphan counts both zero,
failure_record_count=0 and observed_dead_letter_count=0. Consumer heartbeat was
present, error-free and aged 2.807914 seconds. The read-only migrator observer
used verified TLSv1.3. The migrator observer uses
its existing permissions and explicit workload database credential path with
verify-full TLS. Every observation starts a REPEATABLE READ, READ ONLY transaction.
It polls up to 120 seconds, taking a new snapshot per attempt; no SQL writes or
broker operations occur. No extra privileges or image rebuild are needed.

The observer requires a nonempty fully published outbox, inbox coverage for every
outbox message ID, matching envelope fields and occurrence timestamp, no orphan
inbox/projection rows, and projection event/status/outbox ID agreement with each
reservation's newest outbox row. It also requires a present consumer heartbeat
aged 0-30 seconds with no current error. Missing or mismatched state cannot pass.
It reports aggregate historical failure and observed-dead-letter counts without
printing message bodies, customer data or raw heartbeat errors. Those historical
counts do not alone fail an otherwise complete snapshot: they can describe prior
recovered incidents and must be interpreted alongside the native DLQ count.

After success, obtain native Azure queue counts:

```bash
az servicebus queue show \
  --resource-group trafficxx-lab-rg \
  --namespace-name trafficxx-sb-e8708a85 \
  --name reservation-events \
  --query '{active:countDetails.activeMessageCount,dead_letters:countDetails.deadLetterMessageCount,total:messageCount}' \
  --output json
```

The user supplied native counts active=0, dead_letters=0, total=0. This
management-plane observation is separate from SQL evidence. SQL inbox coverage alone does not
prove broker settlement: a message may have committed effects but still await
acknowledgement/redelivery. Counts and stored-state checks are point-in-time
evidence, not a sustained throughput, exactly-once or token-renewal guarantee.

Eight added offline methods cover read-only snapshot queries, missing/pending/
mismatched/orphan state, historical failures, stale/missing/error heartbeats,
wrong identity/TLS/admin, bounded wait/timeout and safe snapshot output. All 194
offline methods pass with simulated database responses; the user-run SQL and
queue observations above subsequently passed in Azure.


The real consumer parses the event, atomically records the deduplication inbox
and projection update, then acknowledges the broker after the SQL commit. A
duplicate can be delivered again after acknowledgement/crash boundaries; its
stable identity and payload must agree with the inbox record. Projection updates
compare outbox IDs to avoid regressing state when older events arrive later.
This records reservation state changes, not automatic booking confirmation.
The real initial 12-event processing checkpoint now has persisted inbox/payload,
latest-projection and empty native queue evidence. The subsequent
[controlled duplicate-delivery experiment](consumer-duplicate.md) also passed:
one acknowledged resend produced a new duplicate log; full inbox/outbox/projection
fingerprints and counts stayed unchanged; native queue counts returned to zero.
The [controlled consumer outage/backlog experiment](consumer-backlog.md) also
passed: two messages accumulated with zero consumer pods, then drained after
restoration to one replica. Read-only SQL verified all 14 inbox records and seven
latest projections with a fresh error-free heartbeat and no mismatch/failure rows.
The [invalid-schema experiment](consumer-poison.md) subsequently verified
schema rejection, unchanged business state, targeted cleanup and normal follow-up
processing: 16 inbox records/eight latest projections, one retained rejection
audit, healthy heartbeat and native queue counts all zero. Crash boundaries,
retry exhaustion, sustained throughput and token renewal across an actual expiry
boundary remain unverified in Azure.
