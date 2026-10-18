# Workload identity learning step

The human administrator's PostgreSQL login over verify-full TLS 1.3 has been
verified from AKS. Applications need their own identities and SQL permissions.
The user applied the reviewed 16-addition identity plan and supplied concrete
identity IDs and the generated manifest. The manifest has been checked for one
namespace, eight distinct expected service accounts, matching client IDs, and
disabled default API-token automount. The user installed the namespace and all
eight service accounts and verified their listing (plus Kubernetes's default
account). The user then verified real browse token exchange with TOKEN_EXCHANGE_OK.
SQL permissions and the other seven workload exchanges are not yet verified.

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

References: [AKS Workload ID](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview),
[deployment steps](https://learn.microsoft.com/en-us/azure/aks/workload-identity-deploy-cluster),
[Terraform federated credentials](https://registry.terraform.io/providers/hashicorp/azurerm/5.10.0/docs/resources/federated_identity_credential).
