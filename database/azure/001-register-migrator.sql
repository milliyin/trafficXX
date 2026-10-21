-- Azure-only setup. Run via scripts/run-postgres-admin-sql.sh as the human admin.
-- Starts in postgres for Entra role management, then connects to trafficxx.
-- These are two transactions: a later grant failure leaves role registration
-- committed. Rerunning checks the mapping and safely retries the grants.
BEGIN;
SELECT pg_advisory_xact_lock(7432003);
DO $setup$
DECLARE
    mapping record;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trafficxx-lab-migrator') THEN
        SELECT * INTO mapping
        FROM pg_catalog.pgaadauth_list_principals(false)
        WHERE rolname = 'trafficxx-lab-migrator';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Existing migrator role has no Entra mapping; refusing to adopt it';
        END IF;
        IF mapping.objectid IS DISTINCT FROM 'b50a542a-5fa6-47b4-ae1f-5491c27221f7'
           OR mapping.tenantid IS DISTINCT FROM 'd2828580-3217-43ad-9f3f-8d430998b86e'
           OR mapping.principaltype IS DISTINCT FROM 'service'
           OR mapping.isadmin IS DISTINCT FROM 0
           OR mapping.ismfa IS DISTINCT FROM 0 THEN
            RAISE EXCEPTION 'Unexpected migrator Entra mapping; refusing to change it';
        END IF;
    ELSE
        PERFORM pg_catalog.pgaadauth_create_principal_with_oid(
            'trafficxx-lab-migrator',
            'b50a542a-5fa6-47b4-ae1f-5491c27221f7',
            'service', false, false
        );
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'trafficxx-lab-migrator'
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
             OR rolbypassrls OR NOT rolcanlogin)
    ) OR pg_has_role('trafficxx-lab-migrator', 'azure_pg_admin', 'MEMBER') THEN
        RAISE EXCEPTION 'Migrator must be a login role without server administrator privileges';
    END IF;
END
$setup$;
COMMIT;

\connect trafficxx
BEGIN;
GRANT CONNECT ON DATABASE trafficxx TO "trafficxx-lab-migrator";
GRANT USAGE, CREATE ON SCHEMA public TO "trafficxx-lab-migrator";
COMMIT;

SELECT 'MIGRATOR_READY' AS result,
       has_database_privilege('trafficxx-lab-migrator', 'trafficxx', 'CONNECT') AS can_connect,
       has_schema_privilege('trafficxx-lab-migrator', 'public', 'USAGE') AS can_use_schema,
       has_schema_privilege('trafficxx-lab-migrator', 'public', 'CREATE') AS can_create_objects,
       pg_has_role('trafficxx-lab-migrator', 'azure_pg_admin', 'MEMBER') AS admin;
