# Outbox publisher on Azure

The background expiry worker passed the controlled idle-hold experiment.
The subsequent user-run BROWSE_DEMO_HTTP_OK again reported health alive,
readiness ready and 100 available for each demo event. Reservation/outbox
history remains intentionally. Azure Service Bus and publisher deployment
have not been created or verified in this stage.

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

Expected result is PUBLISHER_GRANTS_OK. Actual Azure grant output remains
pending. The existing migrator/table owner grants and validates permissions in
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
