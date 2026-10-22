# Outbox publisher on Azure

The background expiry worker passed the controlled idle-hold experiment.
The subsequent user-run BROWSE_DEMO_HTTP_OK again reported health alive,
readiness ready and 100 available for each demo event. Reservation/outbox
history remains intentionally. The user subsequently supplied Terraform outputs for the Azure Service Bus
namespace, queue and private endpoint. Live private broker access and one real
application outbox send subsequently passed. Continuous publisher deployment,
backlog drain and consumer processing remain unverified.

## Register the publisher SQL identity

Existing managed identity trafficxx-lab-publisher:

- Client ID: abd012d6-9e05-415d-b3f5-8c24bee54576
- Object/principal ID: 8f031eea-d16c-469f-a82a-5cc46165c398
- Kubernetes service account: publisher, namespace trafficxx

These are public configuration identifiers. SQL registration uses the object
ID; the SDK credential uses the client ID. Run from browser Cloud Shell:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/run-postgres-admin-sql.sh database/azure/004-register-publisher.sql
```

The human administrator helper uses certificate-verified TLS and its own
temporary client. SQL verifies any existing mapping matches the expected
tenant/object/type and non-admin flags, then grants only CONNECT on trafficxx
and USAGE on public. CREATE and administrator privileges are rejected.
Registration and database grants are separate transactions; reruns validate
the existing mapping before retrying. No application-table grants or broker
role assignments are performed. Expected result is PUBLISHER_ROLE_READY with
CONNECT/USAGE true and CREATE/admin false. The subsequent real publisher login confirms the SQL
identity is registered; a separate PUBLISHER_ROLE_READY transcript was not supplied.

## Verify publisher workload login

After registration succeeds:

```bash
kubectl apply -f kubernetes/checks/postgres-publisher-identity.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-publisher-identity-check --timeout=300s
kubectl logs -n trafficxx job/postgres-publisher-identity-check -c check
```

Expected result is SQL_LOGIN_OK, trafficxx-lab-publisher, database trafficxx,
SSL true and admin false. The user supplied real SQL_LOGIN_OK on 2026-10-23:
trafficxx-lab-publisher, trafficxx database, TLSv1.3, admin=false and the expected
client ID. The diagnostic uses the publisher service account's
projected identity, explicit SDK credential and verify-full TLS, with no human
credential fallback. It runs read-only SQL and reports no token or raw SDK
error text. The short-lived diagnostic installs pinned top-level SDK/driver
dependencies and needs outbound package access. It is bounded by a deadline
and completed Job TTL. Table permissions and messaging are separate stages.

Seven mocked scenarios passed: success, wrong client, wrong service account,
missing CA, administrator privilege, SQL failure and token failure, without
fake-token disclosure. YAML command element types and embedded Python syntax
passed; all 122 existing offline methods pass. These are local checks only.

## Remaining work

Derive table grants from publish_one and heartbeat, test allowed/denied SQL as
publisher, then prepare Azure Service Bus infrastructure and narrowly scoped
send authorization. Cloud broker authentication/configuration must be reviewed
before deploying the publisher. The existing broker helper supports local
connection strings and a namespace/DefaultAzureCredential branch, but the latter
has not been verified against live Azure messaging. Review credential selection
and configuration explicitly. Publication is at least once:
a crash after sending but before marking the outbox row can cause a resend,
so stable message IDs and consumer deduplication remain necessary.

The committed outbox rows are not yet sent to a live Azure queue. Database
identity login alone does not establish broker access, publication or delivery.

## Grant publisher SQL permissions as migrator

Run after the publisher login check passes:

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-publisher-grant-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py=scripts/bootstrap-azure-schema.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/jobs/postgres-publisher-grants.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-publisher-grants --timeout=300s
kubectl logs -n trafficxx job/postgres-publisher-grants -c grant
```

The user supplied real PUBLISHER_GRANTS_OK: read_tables=[outbox],
heartbeat_read_columns=[last_error,name], reservation_access=false,
inventory_access=false, outbox_insert=false and destructive_table_access=false.
The existing migrator/table owner grants and validates permissions in
one transaction, printing success only after commit. This grant-only action
skips migration file reads and does not replay migrations or seed data.

The publisher gets SELECT on outbox because publish_one uses SELECT * with
FOR UPDATE SKIP LOCKED. It gets UPDATE on published_at, last_error,
publish_attempts, last_attempt_at, publish_failures and next_attempt_at only.
It cannot INSERT new outbox rows or change their payload, reservation, message
identity or occurrence timestamp. These column grants do not constrain which
rows or values it can update; application SQL enforces the publishing protocol.

Heartbeat SQL receives INSERT(name,last_error), UPDATE(last_seen_at,last_error)
and SELECT(name,last_error) on worker_heartbeats. PostgreSQL ON CONFLICT requires
SELECT on the conflict key and columns read through EXCLUDED; see the
[PostgreSQL 17 INSERT documentation](https://www.postgresql.org/docs/17/sql-insert.html).
It receives no SELECT on last_seen_at and no UPDATE on name. These are column
boundaries, not isolation to the publisher heartbeat row: this role can write
other heartbeat names/errors using the same SQL rights. Narrowing that boundary
would require a separately reviewed SQL interface or row security.

No reservation/event/inventory, consumer/projection/failure/dead-letter,
migration-ledger, DDL, sequence, DELETE/TRUNCATE/TRIGGER or REFERENCES grants
are added. The validator rejects excessive inherited/direct effective access
across all current public table columns, missing required grants/columns,
wrong ownership and elevated role/database/schema flags. It does not silently
revoke existing privileges.

Eight new offline methods cover the contract, ownership, unrelated reads,
protected updates, outbox inserts, missing heartbeat upsert reads/updates or
outbox columns, elevated/DDL flags and destructive/reference rights. All 130
offline methods pass. These tests use simulated driver responses, not PostgreSQL.
Actual publisher-role DML/heartbeat upsert and permission-denial queries are the
next step, before any Azure broker publication is enabled.

## Test actual publisher SQL permissions

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap postgres-publisher-permission-files -n trafficxx \
  --from-file=check-publisher-permissions.py=scripts/check-publisher-permissions.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/postgres-publisher-permissions.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-publisher-permissions-check --timeout=300s
kubectl logs -n trafficxx job/postgres-publisher-permissions-check -c check
```

Expected: PUBLISHER_PERMISSIONS_OK, nine allowed operations, 23 denials with
SQLSTATE 42501, diagnostic_rows_committed=false, broker_messages_sent=0,
SSL true and admin false. The user supplied that real PUBLISHER_PERMISSIONS_OK
result: all nine operations and all 23 denials passed as trafficxx-lab-publisher,
TLSv1.3, admin=false, no diagnostic rows committed and zero broker messages sent.

The probe logs in as publisher with an explicit workload credential and
verify-full TLS. It selects one pending due outbox row with FOR UPDATE SKIP LOCKED,
updates attempt/retry metadata, checks the staged retry state, marks publication
and checks publication state without altering payload/message identity. It then
inserts a uniquely named diagnostic heartbeat with the real upsert SQL, repeats
that same upsert with an error value to exercise the conflict-update path, and
reads only the granted heartbeat columns.

Each denied operation uses a savepoint and must fail with 42501. Other errors
or unexpected successes fail the probe. The outer transaction is explicitly
rolled back before success output or on failure. No outbox row is actually
published or requeued, no diagnostic heartbeat is committed, and no broker
transport is imported/called. Existing messages are not replaced/reset. Missing
pending fixture data causes failure rather than synthesizing an outbox event.
Keep any publisher process stopped while running this pre-deployment check.

Protected operations cover reservation/event/inventory reads or writes, outbox
INSERT/delete and payload/identity/occurrence edits, heartbeat key renaming,
heartbeat timestamp read/insert, unrelated worker tables, migration ledger and
DDL. Eight new offline methods cover required SQL/denials, unexpected successes,
wrong SQLSTATE, missing fixture, zero-row updates, final rollback, failed upsert
rollback and wrong injected client. All 138 offline methods pass with simulated
SDK/driver responses; actual PostgreSQL upsert/denial behavior subsequently passed
in the user-run Job above. Cloud broker access and deployment remain later stages.

## Prepare private Azure Service Bus infrastructure

The foundation now includes servicebus.tf. It adds a Premium namespace
trafficxx-sb-e8708a85 in the existing AKS region (East US), one messaging unit
and one messaging partition. Premium has continuous capacity charges; it is
chosen for private networking, not as evidence that the lab requires that
throughput. Public network access and local/SAS authentication are disabled.
A private endpoint uses the existing private-endpoints subnet, and the private
DNS zone privatelink.servicebus.windows.net is linked to the AKS VNet.

The queue reservation-events uses a one-minute lock, five maximum deliveries,
14-day default message TTL, dead-letter on expiry, no sessions and no broker
duplicate detection. Message TTL is separate from the five-minute ticket hold.
Consumers still need inbox deduplication for stable message IDs. Namespace and
queue have prevent_destroy safeguards. Only the publisher identity gets the
Data Sender role at this queue's scope. No receiver/owner roles are assigned yet.

References: [Premium private endpoints](https://learn.microsoft.com/en-us/azure/service-bus-messaging/private-link-service),
[Entra-only access and queue-scoped sender roles](https://learn.microsoft.com/en-us/azure/service-bus-messaging/authenticate-application).
The regular namespace FQDN is used by clients; DNS resolves it through the
private endpoint from AKS. Browser Cloud Shell is not assumed to have private
data-plane connectivity; subsequent broker checks must run inside AKS.

First register the resource provider from Cloud Shell:

```bash
cd ~/trafficxx
git pull --ff-only origin main
az provider register --namespace Microsoft.ServiceBus --wait
az provider show --namespace Microsoft.ServiceBus --query registrationState -o tsv
```

After Registered, restore foundation variables in this shell as needed:

```bash
export ARM_SUBSCRIPTION_ID=35aaf788-f4cf-40c3-9ea5-bc8fe8708a85
export TF_VAR_subscription_id="$ARM_SUBSCRIPTION_ID"
export TF_VAR_location=eastus
export TF_VAR_registry_name=trafficxxacre8708a85
export TF_VAR_aks_admin_object_id="$(az ad signed-in-user show --query id -o tsv)"
export TF_VAR_postgres_admin_principal_name="$(az ad signed-in-user show --query userPrincipalName -o tsv)"
cd ~/trafficxx/infrastructure/foundation
```

Confirm the existing backend blob before initialization. Do not initialize
against an empty/new key or recreate the foundation:

```bash
az storage blob exists --account-name trafficxxtfstatee8708a85 \
  --container-name tfstate --name lab/foundation.tfstate \
  --auth-mode login --query exists -o tsv
```

It must report true. Initialize the same backend if this shell has no saved
backend configuration; no storage keys or account passwords are needed:

```bash
terraform init -reconfigure \
  -backend-config="resource_group_name=trafficxx-tfstate-rg" \
  -backend-config="storage_account_name=trafficxxtfstatee8708a85" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=lab/foundation.tfstate" \
  -backend-config="use_azuread_auth=true"
terraform state list
terraform validate
terraform plan -out=servicebus.tfplan
```

The state list must contain the existing AKS, PostgreSQL and workload identities.
The intended change is six additions and no changes/deletions: Service Bus
namespace/queue, private DNS zone/link, private endpoint and sender role.
Review the full plan before apply; investigate any existing-resource update,
replacement or deletion rather than using targeting to hide it. The user supplied the saved servicebus.tfplan: exactly these six additions,
zero changes and zero deletions. The namespace is Premium/one unit/one partition,
public and local authentication disabled; the expected private network resources
and publisher principal are present. Plan review passed; the user subsequently
supplied namespace, queue and private-endpoint outputs after the apply command.
Do not reapply the old saved plan after state has changed. RBAC propagation,
private DNS/AMQP connectivity, actual sender/receiver
denial and publication still require later experiments.

Local validation used Terraform 1.16.6 and installed AzureRM 5.10.0 in an ignored
isolated copy, with the backend removed only in that validation copy. No Azure
login/state/plan was accessed locally. Provider schema/fmt/validate and all 138
offline methods pass. An initial DNS-link schema mismatch was corrected to the
AzureRM 5 private_dns_zone_id argument; the committed provider lock is unchanged.

## Check publisher private connectivity and link authorization

The user supplied Terraform outputs after the reviewed plan, including namespace
trafficxx-sb-e8708a85, reservation-events and the private endpoint resource ID.
A separate Apply complete summary was not supplied. These outputs are resource
configuration evidence, not AMQP connectivity or live authorization evidence.
The outputs also reconfirmed the expected publisher client/object IDs.

Run the check inside AKS using publisher service account and the existing runtime
image. The ConfigMap supplies only the diagnostic source, not credentials:

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap servicebus-publisher-check-files -n trafficxx \
  --from-file=check-servicebus-publisher.py=scripts/check-servicebus-publisher.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/servicebus-publisher-access.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/servicebus-publisher-access-check --timeout=180s
kubectl logs -n trafficxx job/servicebus-publisher-access-check -c check
```

Expected SERVICEBUS_PUBLISHER_ACCESS_OK with private_ips/tls_peer in
10.40.17.0/24, verified TLS version, sender_link_open=true,
receive_denied=true and broker_messages_sent=0. The user supplied real
SERVICEBUS_PUBLISHER_ACCESS_OK with private_ips=[10.40.17.4], tls_peer=10.40.17.4,
TLSv1.3, sender_link_open=true, receive_denied=true, broker_messages_sent=0 and
the expected publisher client ID. This verifies live private link authorization,
not actual message publication.
The check validates exact projected publisher identity/configuration, rejects
any connection string, and uses WorkloadIdentityCredential explicitly. It verifies
DNS and certificate-checked TLS on AMQP port 5671, opens a queue sender link and
creates an unsent empty batch to negotiate limits. It then requires an explicit
ServiceBusAuthorizationError on receiver open/peek. Unexpected receive access,
identity failure, timeout and network errors fail rather than count as denial.

No send_messages or receive_messages call occurs, and no message content is
printed. Peeking does not lock/remove queued messages; see the
[SDK receiver documentation](https://learn.microsoft.com/en-us/python/api/azure-servicebus/azure.servicebus.servicebusreceiver?view=azure-python).
This establishes sender-link authorization only if it passes; an actual message
send, outbox publication, consumer processing and sustained renewal remain later
experiments. This does not modify the publisher runtime credential-selection
code or deploy its long-running process.

Azure role assignments can take several minutes to propagate. If sender_link
fails just after apply, inspect the safe check/error type, allow propagation,
then delete only the diagnostic Job and rerun the apply/wait/log commands:

```bash
kubectl delete job/servicebus-publisher-access-check -n trafficxx
```

Do not enable SAS/public access or grant Data Owner to bypass a failure. The Job
has a deadline and completed-job TTL. It needs no package downloads or image
build. Eight new offline methods cover no-send/receive behavior, unsafe identity/
connection-string settings, public DNS/TLS peer, sender denial, receiver denial,
unexpected receiver authorization and timeout classification, with raw-error
redaction. All 146 offline methods pass; network/SDK responses are mocked there.

## Publish one real outbox event

This stage changes real application state: one pending outbox event is sent to
reservation-events and its published marker is committed. It also commits the
publisher heartbeat. Nothing is rolled back as a diagnostic. The consumer is
not deployed; the message remains queued. An event may describe an earlier HELD
state of a reservation that is now cancelled/expired; sending history does not
change reservation state. Keep the continuous publisher stopped during this
one-shot experiment.

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap publisher-send-one-files -n trafficxx \
  --from-file=publish-one-azure.py=scripts/publish-one-azure.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/jobs/publisher-send-one.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/publisher-send-one --timeout=180s
kubectl logs -n trafficxx job/publisher-send-one -c publish
```

Expected PUBLISHER_SEND_OK: send_calls=1, send_acknowledged=true,
publication_committed=true, heartbeat_recorded=true and stable outbox/message IDs.
The user supplied real PUBLISHER_SEND_OK: send_calls=1, send_acknowledged=true,
publication_committed=true, outbox_id=3, message_id=151af373-f74d-4bc4-8c14-e3b6cf13aa51,
event_status=HELD, heartbeat_recorded=true and broker_credential=WorkloadIdentityCredential.
The existing image runs the actual
trafficxx.broker.client/send_event and messaging.publish_one/heartbeat paths,
not a replacement send implementation. Read-only queries then verify the outbox
marker, durable message identity, attempts/cleared error and heartbeat values.
No SQL permissions or runtime image are changed.

The Job sets AZURE_TOKEN_CREDENTIALS=WorkloadIdentityCredential. The installed
azure-identity 1.26.0 supports individual-credential allowlisting, constraining
the broker's existing DefaultAzureCredential to WorkloadIdentityCredential;
see [Microsoft credential-chain documentation](https://learn.microsoft.com/azure/developer/python/sdk/authentication/credential-chains?tabs=dac).
Preflight rejects another SDK version, broad credential settings, connection
strings/URLs, unexpected database/broker identity or projected service account.
Database connections still use the explicit workload-identity code path.

The Job has backoffLimit=0 and calls publish_one once per invocation, rather than
running a retry loop. This is not exactly-once delivery. An acknowledged send
can precede an uncertain SQL commit, or a send timeout can follow broker acceptance.
On failure, output includes safe stage/type, send-call/acknowledgement metadata
and any selected outbox/message IDs; it never prints raw SDK errors or credentials.
publication_committed=null means the call did not confirm its transaction outcome;
true remains true if later readback/heartbeat/client-close fails. Do not recreate
the Job blindly after failure: inspect those IDs and persisted state first, and
keep the stable message ID for any legitimate resend. The bounded Job/TTL does
not guarantee a Pod cannot ever be replaced after infrastructure disruption.

No message is received/settled and no claim of downstream delivery/processing is
made. Eight new offline methods cover the real-function orchestration, empty
outbox, send-then-commit failure, client-close failure, later heartbeat/readback
failure, accidental second send and unsafe credential preflight. All 154 offline
methods pass with mocked broker/database responses. The actual live send and
persisted readback subsequently passed in the user-run Job above.

## Deploy the continuous publisher

This starts publishing all remaining committed outbox history to the live queue,
including older HELD/CANCELLED/EXPIRED events. It marks published rows and updates
the publisher heartbeat. It does not create bookings or rewrite reservation
state. The consumer is not deployed, so messages wait for later processing.

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/deploy-publisher.sh
kubectl logs -n trafficxx deployment/publisher --tail=50
```

Expected PUBLISHER_DEPLOYED and one Ready Running publisher pod. Successful sends
log outbox_publish outcome=published; idle loops do not log a publication. Errors
can log safe reconnect/retry classifications. Actual rollout/drain evidence is
pending. Do not rerun the earlier one-shot Job to start the continuous process.

The one-replica Recreate Deployment uses the same reviewed immutable image,
publisher service account/SQL role, private namespace/queue and
AZURE_TOKEN_CREDENTIALS=WorkloadIdentityCredential. The existing SDK version
supports that allowlist, as proven in the one-shot configuration. No new image,
SQL/broker grants, connection strings, HTTP service or public endpoint is added.
It retains non-root/read-only-root/dropped-capability/resource settings.

The existing runtime calls publish_one repeatedly and records a heartbeat each
iteration. Stable message IDs survive retries; sending and marking publication
cannot be one cross-service atomic transaction, so duplicate sends remain
possible. The later consumer must deduplicate. SQL errors and broker failures
use the existing bounded-backoff/reconnect behavior; this deployment does not
add a new retry implementation.

Readiness only verifies a database connection/SELECT 1, not broker health or loop
progress. No application liveness probe detects a stuck loop; the publisher
has an SQL heartbeat but readiness does not read its timestamp. Verify persisted
outbox counts and heartbeat freshness separately after rollout. One replica is
a lab topology, not publisher HA. Four stubbed deployment scenarios passed
(success, wrong subscription, wrong context and apply failure), Bash syntax/LF
and manifest module/identity/image/security/configuration checks passed, and all
154 offline methods pass. Actual deployment, outbox drain and live queue counts
remain pending; continuous delivery/consumer effects are not claimed.
