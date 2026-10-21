# Workload identity learning step

The human administrator's PostgreSQL login over verify-full TLS 1.3 has been
verified from AKS. Applications need their own identities and SQL permissions.
The user applied the reviewed 16-addition identity plan and supplied concrete
identity IDs and the generated manifest. The manifest has been checked for one
namespace, eight distinct expected service accounts, matching client IDs, and
disabled default API-token automount. The user installed the namespace and all
eight service accounts and verified their listing (plus Kubernetes's default
account). The user then verified real browse token exchange with TOKEN_EXCHANGE_OK.
On 2026-10-22 the browse identity was also registered in PostgreSQL and its real
pod SQL login passed with verified TLS 1.3 and no administrator privileges.
Application-table permissions and the other seven workload exchanges remain unverified.

## Three different questions

1. Who are you? A managed identity identifies a workload to Microsoft Entra.
2. Why should Azure trust this pod? A federated credential matches the AKS OIDC
   issuer, namespace, service-account name, and token audience.
3. What may you do? PostgreSQL SQL grants and Service Bus roles provide access.

This step prepares the first two. It does not grant database permissions,
queue access, Kubernetes administration, or the human administrator's rights.

Terraform's `for_each` creates a managed identity and a federated credential for
each named account. Eight accounts produce **16 Azure resources**, not 16 VMs:

| Account | Intended responsibility; permissions are a later step |
|---|---|
| browse | Read catalog and availability |
| booking | Create and transition reservations |
| expiry | Release expired reservations |
| publisher | Publish committed outbox events |
| consumer | Record receipts and apply reservation projections |
| operations | Read backlog and worker health |
| migrator | Create/update schema; not an API identity |
| reconciler | Explicit manual replay; separate from read-only operations |

Trust subjects have this exact form:

```text
system:serviceaccount:trafficxx:browse
```

An account with the same name in another namespace or cluster does not match
that trust record. Federation cannot grant permissions that the managed
identity/database role lacks. Anyone allowed to create pods using a privileged
service account can potentially use that identity, so namespace access and
service-account use require controls before exposing an environment to others.

These are application identities, separate from the kubelet identity used for
ACR image downloads and the control-plane identity used to manage AKS resources.

## User-run Cloud Shell sequence

Upload `trafficxx-workload-identities-update.zip`. It contains only
`workload-identities.tf`, so backend settings and existing resources are preserved.

```bash
cd ~/trafficxx/foundation
unzip -o ~/trafficxx-workload-identities-update.zip -d ~/trafficxx/foundation
terraform validate
terraform plan -out=workload-identities.tfplan
```

Keep supplying the same human administrator values used for earlier plans.
Review the plan: expect 16 additions and zero existing-resource changes or
destructions. The first user-run plan also tried to clear PostgreSQL's
Azure-assigned zone. The updated postgres.tf ignores that specific field;
upload the provided zone update and generate a new saved plan before applying.
The updated outputs include public identity IDs and a generated
Kubernetes manifest. They contain no passwords, access tokens, or kubeconfig.

After review, the user can apply the new saved plan. Terraform creates only
Azure resources in this step. It does not contact the Kubernetes API to install
service accounts and it does not grant SQL access.

## Kubernetes objects after identity apply

Only after successful apply, export and inspect the generated manifest:

```bash
terraform output -raw workload_service_accounts_manifest > workload-serviceaccounts.json
python3 -m json.tool workload-serviceaccounts.json
```

The manifest contains one trafficxx namespace and eight ServiceAccounts with
the correct client/tenant annotations. After review the user can install it
using `kubectl apply -f workload-serviceaccounts.json`. It is generated from
Terraform outputs and is not independently managed by the AzureRM provider;
regenerate it if identity IDs or namespace definitions change.

Default Kubernetes API token automount is disabled on these service accounts.
The workload identity webhook separately injects the explicit projected token
used for Azure token exchange when a pod opts in. Future pod templates must
select the matching service account and use this label on the pod template:

```yaml
metadata:
  labels:
    azure.workload.identity/use: "true"
spec:
  serviceAccountName: browse
```

The application uses WorkloadIdentityCredential (or an appropriately configured
Azure Identity credential) to exchange the projected token for a short-lived
Azure token. Database token acquisition/refresh, SQL identity mapping, grants,
schema initialization, and successful pod token exchange are still ahead.

The current migration runner assumes the base outbox table already exists;
Azure's empty database still needs a deliberately ordered bootstrap rather
than blindly running that existing local migration command.

Local Terraform formatting and AzureRM 5.10.0 validation passed. Browse federation
has user-run evidence; application SQL authorization, the other workload
exchanges, and negative permission tests remain unverified.

## Verify the browse identity before SQL grants

Upload `kubernetes/checks/workload-identity.yaml` to Cloud Shell, then run:

```bash
kubectl apply -f ~/workload-identity.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/workload-identity-check --timeout=120s
kubectl logs -n trafficxx job/workload-identity-check
```

The temporary Job uses the browse service account, opts into the workload
identity webhook, and runs as a non-root user on apps nodes. The selected Python
image's published Dockerfile includes its CA trust bundle. The diagnostic uses
Python's standard library to exchange the projected Kubernetes token for an
Entra token scoped to PostgreSQL. It verifies the injected client/tenant IDs
against the actual browse identity outputs and checks the expected subject.
Entra performs signature, issuer, audience, and trust validation; merely
decoding the local subject is not treated as cryptographic verification.

Success logs contain TOKEN_EXCHANGE_OK, client ID, subject, and expiry metadata.
No Kubernetes or Azure bearer token is printed, written to a Secret, or added
to manifest settings. HTTP failure output contains only status and numeric
Entra error codes; raw error bodies are excluded to avoid accidental disclosure.
No database connection or data mutation is attempted. The Job has a two-minute
active deadline, zero retries, and cleanup configured ten minutes after finishing.
If rerunning before cleanup, delete only this named diagnostic Job first.

Embedded Python syntax and four simulated checks passed: success, wrong injected
identity, wrong service-account subject, and a token-server error. The mismatch
cases stop before requesting an Azure token; no test token appeared in output.
These simulated checks alone do not establish real cluster federation. The user
subsequently supplied real TOKEN_EXCHANGE_OK output with the expected browse
subject/client ID and expiry metadata. That result covers browse only and does
not establish SQL permissions. Runtime applications will
use Azure Identity SDK credentials/token renewal rather than this small
protocol-level diagnostic.

## PostgreSQL login with the browse workload identity

On 2026-10-22 the user reconnected to the existing remote Terraform state and
verified administrator SQL login with TLS 1.3. The database public schema had no
tables, and Entra role inventory initially listed only the human administrator.
The administrator then successfully registered `trafficxx-lab-browse` with
`pgaadauth_create_principal_with_oid`, binding service principal object ID
`5fce820f-ac7a-4cc0-9315-36b8386d5cea` with both administrator and MFA flags false.
This is the principal/object ID, not the client ID used on the service account.
No application table grants or schema bootstrap have been applied yet.

Upload `kubernetes/checks/postgres-workload-identity.yaml` from this workspace
to the Cloud Shell home directory. It is a new local file until published to
GitHub; the earlier Cloud Shell clone will not contain it automatically. Run:

```bash
kubectl apply -f ~/postgres-workload-identity.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-workload-identity-check --timeout=300s
kubectl logs -n trafficxx job/postgres-workload-identity-check -c check
```

This temporary diagnostic installs the application's pinned Azure Identity and
Psycopg versions into a shared volume using an init container without projected
Azure credentials. This requires outbound package-index access; it is a lab
diagnostic, not the production image build process. Transitive dependencies
are resolved at run time. The main container has a read-only dependency mount
and explicitly uses `WorkloadIdentityCredential`, with no administrator or CLI
fallback. It checks the expected identity and projected subject, requests a
PostgreSQL-scoped token, and connects as `trafficxx-lab-browse` with
`sslmode=verify-full`. Only read-only session metadata is queried. Success
requires the expected database/user, TLS, no superuser/CREATEDB/CREATEROLE flags,
and no membership in `azure_pg_admin`.

Expected output is `SQL_LOGIN_OK` with identity and TLS metadata. Failure output
contains only exception type and SQLSTATE, never exception text or bearer tokens.
The Job has a five-minute deadline, no retries, and a ten-minute cleanup TTL.
If it fails before starting the check container, inspect dependency-install logs:

```bash
kubectl logs -n trafficxx job/postgres-workload-identity-check -c install-dependencies
```

Delete only this named diagnostic Job before rerunning it while it still exists.
Local YAML/Python validation and seven mocked scenarios passed: successful login,
wrong client, wrong subject, missing CA, unexpected administrator membership,
SQL failure, and token failure. Identity/CA failures stop before token acquisition;
failure text does not expose the simulated token. The user subsequently supplied
real `SQL_LOGIN_OK` logs: database trafficxx, identity trafficxx-lab-browse,
ssl=true, TLSv1.3, admin=false, and the expected browse client ID. This verifies
workload federation through SQL authentication without the human CLI credential.
This diagnostic does not test table permissions, token
refresh over time, or the other seven identities.

## Register the schema migrator

`database/azure/001-register-migrator.sql` registers `trafficxx-lab-migrator`
against its recorded service-principal object ID
`b50a542a-5fa6-47b4-ae1f-5491c27221f7`. It verifies an existing role's Entra object,
tenant, service type and non-admin flags before reuse; an unexpected existing
role is rejected. Server administrator, superuser, CREATEDB, CREATEROLE,
replication and bypass-RLS privileges are rejected. It grants CONNECT to
trafficxx and USAGE/CREATE on its public schema, so later migrations can create
and own application objects without using the human administrator at runtime.
It does not create tables, grant browse table access, grant role memberships,
or change PUBLIC privileges. Migrator access is intentionally powerful within
the application schema and should only be used by migration Jobs.

Use the reviewed administrator helper `scripts/run-postgres-admin-sql.sh` from
browser Cloud Shell. It validates subscription/context, copies the public CA
bundle and reviewed SQL into a temporary non-root apps-node client, checks those
files before requesting a token, and streams the human Entra token over stdin
to psql with verify-full TLS and ON_ERROR_STOP. Cleanup runs on success/failure;
the pod also has a ten-minute deadline. SQL files execute with administrator
permissions and may include psql commands; only use reviewed project files,
never credential-bearing SQL. SQL output/errors are visible in Cloud Shell.

Upload both files into Cloud Shell home and run:

```bash
bash ~/run-postgres-admin-sql.sh ~/001-register-migrator.sql
```

Alternatively, upload the generated `trafficxx-migrator-setup.zip` containing
those two files, extract into `~/trafficxx-migrator-setup`, and run the helper
there. The bundle is an ignored local delivery artifact, not a tracked secret.
Entra registration runs in postgres and commits before connecting to trafficxx
for a separate grant transaction. A later failure can leave registration
committed; rerunning validates the mapping and retries the grants. Expected
final output is MIGRATOR_READY with all three access flags true and admin false.
This confirms SQL configuration only. Real migrator pod login, schema ownership
and allowed/denied operation tests are subsequent steps.

Bash syntax and four mocked helper-flow scenarios passed (success, preflight
failure, SQL failure, and missing local CA), checking cleanup, exit status,
token-request ordering and absence of a simulated token in output. This SQL
file was subsequently executed by the user against Azure PostgreSQL: both
transactions committed and MIGRATOR_READY reported all three access flags true
and admin false. The helper reported temporary client cleanup. This verifies
registration/grants. The user then supplied real migrator Job SQL_LOGIN_OK:
database trafficxx, identity trafficxx-lab-migrator, TLSv1.3, admin=false and
client ID 91f335cf-9de4-4264-9493-eec3cf86f941. This verifies its own workload login.
The user subsequently completed [schema bootstrap](schema-bootstrap.md),
browse/booking grants and real API experiments. See the
[Booking guide](booking-api-azure.md) for the current verified checkpoint.
The expiry worker's SQL registration, grants and deployment remain next work.

References: [PostgreSQL schema privileges](https://www.postgresql.org/docs/17/ddl-schemas.html),
[Entra role management](https://learn.microsoft.com/en-us/azure/postgresql/security/security-manage-entra-users),
[WorkloadIdentityCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.workloadidentitycredential?view=azure-python),
[AKS Workload ID](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview),
[deployment steps](https://learn.microsoft.com/en-us/azure/aks/workload-identity-deploy-cluster),
[Terraform federated credentials](https://registry.terraform.io/providers/hashicorp/azurerm/5.10.0/docs/resources/federated_identity_credential).
