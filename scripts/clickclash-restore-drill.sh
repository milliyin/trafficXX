#!/bin/sh
# Executed only inside the short-lived operator pod. JWT arrives on stdin.
set -eu
umask 077
scratch=${1:?scratch database required}
work=${2:-/scratch}
suffix=${scratch#clickclash_restore_}
[ "$scratch" != "$suffix" ] && [ "${#suffix}" -eq 12 ] || exit 2
case "$suffix" in *[!0-9a-f]*) exit 2 ;; esac
mkdir -p "$work"
stage=read_token
created=false
cleanup=null
finish() {
  code=$?
  trap - EXIT
  if [ "$created" = true ]; then
    if dropdb --maintenance-db=postgres -- "$scratch" >>"$work/private-errors.log" 2>&1; then cleanup=true; else cleanup=false; fi
  fi
  if [ "$code" -eq 0 ] && [ "$cleanup" = true ]; then
    printf '%s\n' '{"result":"CLICKCLASH_LOGICAL_RESTORE_OK","players":1,"score":3,"receipts":3,"image_restored":true,"session_restored":true,"audit_sequence_verified":true,"scratch_database_removed":true,"production_writes":false,"managed_pitr_tested":false}'
  else
    printf '{"result":"CLICKCLASH_LOGICAL_RESTORE_FAILED","stage":"%s","scratch_database_removed":%s}\n' "$stage" "$cleanup"
    code=1
  fi
  exit "$code"
}
trap finish EXIT
IFS= read -r PGPASSWORD
[ -n "$PGPASSWORD" ]
export PGPASSWORD
# All tool diagnostics remain private in the memory-backed temporary volume.
exec 2>>"$work/private-errors.log"
stage=check_empty_source
[ "$(psql -X -w -d trafficxx -Atqc 'SELECT count(*) FROM clickclash.players')" = 0 ]
stage=dump_source_schema
pg_dump -w -d trafficxx --schema=clickclash --format=custom --no-owner --no-acl --file="$work/source.dump"
pg_dump -w -d trafficxx --table=public.clickclash_migrations --format=custom --no-owner --no-acl --file="$work/ledger.dump"
stage=create_scratch
createdb -w --maintenance-db=postgres -- "$scratch"
created=true
psql -X -w -d postgres -v ON_ERROR_STOP=1 -qc "REVOKE CONNECT ON DATABASE \"$scratch\" FROM PUBLIC"
stage=restore_source_schema
pg_restore -w -d "$scratch" --exit-on-error --no-owner --no-acl "$work/source.dump"
pg_restore -w -d "$scratch" --exit-on-error --no-owner --no-acl "$work/ledger.dump"
stage=seed_scratch_only
psql -X -w -d "$scratch" -v ON_ERROR_STOP=1 -q >"$work/private-seed.log" <<'SQL'
BEGIN;
TRUNCATE clickclash.operator_audit RESTART IDENTITY;
INSERT INTO clickclash.players(id,email,password_hash,nickname,score,bio,website)
VALUES ('00000000-0000-4000-8000-000000000001','restore@example.invalid','not-a-login-credential','Restore fixture',3,'Temporary restore test','https://example.invalid');
INSERT INTO clickclash.receipts(id,player_id)
SELECT gen_random_uuid(),'00000000-0000-4000-8000-000000000001'::uuid FROM generate_series(1,3);
INSERT INTO clickclash.sessions VALUES ('restore-fixture-token-hash','00000000-0000-4000-8000-000000000001','restore-csrf',clock_timestamp()+interval '1 hour');
INSERT INTO clickclash.images VALUES ('00000000-0000-4000-8000-000000000001',decode('524946461e000000574542505650384c110000002f0000000007d0fffef7bfff8188e87f0000','hex'),'restore-image-digest');
UPDATE clickclash.control SET image_bytes=(SELECT octet_length(content) FROM clickclash.images);
INSERT INTO clickclash.operator_audit(action) VALUES ('restore-fixture');
COMMIT;
SQL
cat >"$work/fingerprint.sql" <<'SQL'
SELECT md5(string_agg(label || ':' || coalesce(doc,'[]'::jsonb)::text,'|' ORDER BY label)) FROM (
 SELECT 'players' label,jsonb_agg(to_jsonb(t) ORDER BY id) doc FROM clickclash.players t UNION ALL
 SELECT 'receipts',jsonb_agg(to_jsonb(t) ORDER BY id) FROM clickclash.receipts t UNION ALL
 SELECT 'sessions',jsonb_agg(to_jsonb(t) ORDER BY token_hash) FROM clickclash.sessions t UNION ALL
 SELECT 'images',jsonb_agg(to_jsonb(t) ORDER BY player_id) FROM clickclash.images t UNION ALL
 SELECT 'control',jsonb_agg(to_jsonb(t) ORDER BY id) FROM clickclash.control t UNION ALL
 SELECT 'buckets',jsonb_agg(to_jsonb(t) ORDER BY key,bucket_start) FROM clickclash.buckets t UNION ALL
 SELECT 'reports',jsonb_agg(to_jsonb(t) ORDER BY reporter,target) FROM clickclash.reports t UNION ALL
 SELECT 'audit',jsonb_agg(to_jsonb(t) ORDER BY id) FROM clickclash.operator_audit t UNION ALL
 SELECT 'ledger',jsonb_agg(to_jsonb(t) ORDER BY version) FROM public.clickclash_migrations t
) AS contents;
SQL
before=$(psql -X -w -d "$scratch" -At -v ON_ERROR_STOP=1 -f "$work/fingerprint.sql")
stage=dump_fixture
pg_dump -w -d "$scratch" --schema=clickclash --format=custom --no-owner --no-acl --file="$work/fixture.dump"
pg_dump -w -d "$scratch" --table=public.clickclash_migrations --format=custom --no-owner --no-acl --file="$work/fixture-ledger.dump"
stage=clear_scratch_only
# This database was newly created above, with a validated random drill name.
psql -X -w -d "$scratch" -v ON_ERROR_STOP=1 -qc 'DROP SCHEMA clickclash CASCADE; DROP TABLE public.clickclash_migrations' >"$work/private-clear.log"
stage=restore_fixture
pg_restore -w -d "$scratch" --exit-on-error --no-owner --no-acl "$work/fixture.dump"
pg_restore -w -d "$scratch" --exit-on-error --no-owner --no-acl "$work/fixture-ledger.dump"
stage=verify_records
after=$(psql -X -w -d "$scratch" -At -v ON_ERROR_STOP=1 -f "$work/fingerprint.sql")
[ -n "$before" ] && [ "$before" = "$after" ]
[ "$(psql -X -w -d "$scratch" -Atqc "SELECT count(*)::text || ':' || sum(score)::text FROM clickclash.players")" = '1:3' ]
[ "$(psql -X -w -d "$scratch" -Atqc 'SELECT count(*) FROM clickclash.receipts')" = 3 ]
[ "$(psql -X -w -d "$scratch" -v ON_ERROR_STOP=1 -Atqc "INSERT INTO clickclash.operator_audit(action) VALUES ('sequence-check') RETURNING id")" = 2 ]
stage=verify_source_untouched
[ "$(psql -X -w -d trafficxx -Atqc 'SELECT count(*) FROM clickclash.players')" = 0 ]
stage=cleanup
