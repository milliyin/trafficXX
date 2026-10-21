# Azure schema bootstrap

The user verified real migrator pod SQL login on 2026-10-22: trafficxx database,
trafficxx-lab-migrator identity, TLSv1.3, admin=false and the expected client ID.
The administrator previously granted CONNECT and public-schema USAGE/CREATE.
The user then ran the bootstrap Job successfully: SCHEMA_BOOTSTRAP_OK, both
001-schema.sql and 002-messaging.sql applied, no skipped files, and ten verified
migrator-owned tables. No demo data or runtime grants were included.

## Run from browser Cloud Shell

The generated local `trafficxx-schema-bootstrap.zip` contains only these four
reviewable project files, flattened for upload:

- `scripts/bootstrap-azure-schema.py`
- `kubernetes/jobs/postgres-schema-bootstrap.yaml`
- `database/001-schema.sql`
- `database/migrations/002-messaging.sql`

Upload the ZIP into Cloud Shell home, then run:

```bash
unzip -o ~/trafficxx-schema-bootstrap.zip -d ~/trafficxx-schema-bootstrap
cd ~/trafficxx-schema-bootstrap

kubectl create configmap postgres-schema-bootstrap-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py \
  --from-file=001-schema.sql \
  --from-file=002-messaging.sql \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f postgres-schema-bootstrap.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-schema-bootstrap --timeout=600s
kubectl logs -n trafficxx job/postgres-schema-bootstrap -c bootstrap
```

Expected first-run result is SCHEMA_BOOTSTRAP_OK, identity
trafficxx-lab-migrator, both filenames in applied, no skipped files, and ten
verified tables. This creates the reservation and messaging tables plus the
migration ledger. It does not seed demo inventory or grant application access.

If the Job fails before the main container starts, inspect
`kubectl logs -n trafficxx job/postgres-schema-bootstrap -c install-dependencies`.
For a later rerun, delete only the named bootstrap Job first (or wait for its
ten-minute cleanup TTL). Keep the exact same SQL inputs when retrying. The input
ConfigMap remains for inspection; it contains reviewed code/DDL, no credentials.
Only trusted operators should be able to modify it or launch migrator pods.

## Safety and limits

The Job uses the migrator service account and explicit Azure workload identity
credentials, with no human/CLI fallback. It verifies the SQL identity, TLS, and
absence of server administrator privileges before DDL. Tokens remain in memory;
failure logs include only exception class and SQLSTATE, not exception text.
The init container without Azure token projection downloads pinned direct
dependencies, as in the login diagnostic. Runtime package-index access is
required and transitive dependencies remain unpinned; production image builds
are later work. The main filesystem and ConfigMap mount are read-only.

One database transaction uses the existing migration advisory-lock key 7432002.
It refuses an untracked nonempty schema, creates or validates the migrator-owned
schema_migrations ledger, checks SHA-256 hashes, applies the base schema before
the additive messaging migration, and verifies ten tables belong to the
migrator. There are no DROP/TRUNCATE/reset or seed steps. A failed transaction
rolls back DDL and ledger updates together; success is printed after commit.
Matching recorded files are skipped on rerun. Changing an applied SQL file is
rejected; future schema changes should use new additive migrations.

The ledger format and normalized-text hashing match trafficxx.migrate, including
the 002-messaging.sql filename, so the existing runner will skip that migration.
It still assumes the base schema already exists. This bootstrap tool covers the
two listed files only; it is not a replacement for future additive migrations.
Ownership/table presence checks do not constitute a complete schema drift audit.

Run the offline decision tests with
`python -m unittest discover -s unit_tests -v`. The suite covers ordered
bootstrap, no-op retry, checksum mismatch, untracked schema, ledger ownership,
missing base records, tracked-base recovery, missing tables, table ownership,
and migration failure propagation, plus column-scoped browse grants, owner
checks, effective-permission rejection, and negative-probe error/rollback handling. These use a simulated connection,
not a PostgreSQL engine. The successful Azure first run verifies actual DDL;
an Azure retry, deliberately failed transaction, and checksum rejection have
not been exercised against the live database.
They are separate from the existing database-resetting tests under tests/ and
now run in the workflow-validation job; a hosted run of this update is pending.

## Grant browse read access as the table owner

The bootstrap script's separate `--grant-browse` action runs as migrator and
does not apply schema files. `kubernetes/jobs/postgres-browse-grants.yaml` selects
that action. It verifies migrator ownership and grants SELECT on public.events,
column SELECT on ticket_units(event_id, unit_number, seat_label, reservation_id),
and column SELECT on reservations(id, status, expires_at). These match the
current browse API SQL. No customer_id, request or idempotency_key access is
granted; there are no broad default privileges for future tables.

The action checks effective CONNECT/public-schema USAGE and rejects effective
CREATE on the schema/database, administrator membership, insert/update/delete/
truncate access on the three source tables, or reads of other reservation
columns. An unexpected existing grant causes failure/transaction rollback;
the Job does not silently revoke unrelated access or change PUBLIC privileges.
The browse login already passed with the current database/schema defaults.

Upload the generated trafficxx-browse-grants.zip and run:

```bash
unzip -o ~/trafficxx-browse-grants.zip -d ~/trafficxx-browse-grants
cd ~/trafficxx-browse-grants
kubectl create configmap postgres-browse-grant-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f postgres-browse-grants.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-browse-grants --timeout=300s
kubectl logs -n trafficxx job/postgres-browse-grants -c grant
```

Expected output is BROWSE_GRANTS_OK, grantee trafficxx-lab-browse, writes=false,
private_reservation_columns=false. The user supplied this exact successful result
from the Azure Job. The allowed/denied query test below also subsequently passed. Neither
this action nor the preceding bootstrap starts an API or seeds demo inventory.

## Test allowed and forbidden browse queries

Upload the generated two-file trafficxx-browse-permissions.zip, containing
scripts/check-browse-permissions.py and the corresponding Kubernetes check Job.
Run:

```bash
unzip -o ~/trafficxx-browse-permissions.zip -d ~/trafficxx-browse-permissions
cd ~/trafficxx-browse-permissions
kubectl create configmap postgres-browse-permission-files -n trafficxx \
  --from-file=check-browse-permissions.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f postgres-browse-permissions.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-browse-permissions-check --timeout=300s
kubectl logs -n trafficxx job/postgres-browse-permissions-check -c check
```

The pod uses browse workload credentials and verify-full TLS, verifies its SQL
identity/non-admin flags, and explicitly selects a read-write transaction.
It runs three required event/readiness/availability reads, then expects nine
operations to fail: event insert/update/delete, table/schema creation, customer
details, full reservation rows, outbox and migration-ledger reads. The availability
query uses a deliberately nonexistent event and proves column access, not demo
inventory correctness. No data rows are printed.

Every forbidden operation has a savepoint. The probe rolls back and releases it
whether the operation is denied or unexpectedly succeeds; unexpected success
fails the Job. Only SQLSTATE 42501 (insufficient_privilege) counts as a denial.
Missing objects, syntax errors, or read-only transaction failures fail the test.
The outer transaction is also rolled back on success. Expected output is
BROWSE_PERMISSIONS_OK with three allowed names, nine denied names, denial_sqlstate
42501, and safe identity/TLS metadata. The user supplied this result from Azure:
all three allowed reads passed; all nine forbidden operations were denied with
42501; database trafficxx, identity trafficxx-lab-browse, ssl=true, TLSv1.3,
admin=false. This verifies browse's actual effective permission boundary for
these operations. It does not verify seeded availability, row-level customer
isolation, untested operations, sustained token renewal, or a deployed API.

Five offline test methods exercise successful classification, unexpected allowed
operations, missing-table/syntax/read-only errors, required-read failure and
savepoint cleanup for every forbidden operation. These simulate driver calls;
they do not replace the live database test. The same diagnostic dependency
installation/Job cleanup limitations apply as in the login probe.

References: [PostgreSQL SQLSTATE codes](https://www.postgresql.org/docs/17/errcodes-appendix.html),
[savepoint rollback](https://www.postgresql.org/docs/17/sql-rollback-to.html).

Browse deployment and [demo inventory](demo-inventory.md) have subsequently
passed their live checks. [Booking](booking-api-azure.md)/worker SQL permissions,
their API/worker deployments and sustained token-renewal verification remain
separate work.
