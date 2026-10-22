# Expiry worker on Azure

The user reconnected on 2026-10-23 and verified all four AKS nodes Ready.
Browse and Booking each still had two Ready Running replicas and zero restarts.
The previous real five-minute expiry experiment passed using a booking
transaction. The background expiry worker has not been deployed.

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
the real worker SQL login subsequently passed. Table grants and worker deployment
are still pending.

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
actual Azure output remains pending.

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
