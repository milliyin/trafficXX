# AKS learning step

The foundation and AKS now exist in Azure, according to the user's Cloud Shell
outputs. `infrastructure/foundation/aks.tf` extends the foundation configuration
and state. The user verified four Ready nodes at Kubernetes v1.36.4: two in
`system` and two in `apps`, using Entra sign-in. Application pods are not deployed.

## What the cluster will contain

| Setting | Choice | Reason |
|---|---|---|
| Kubernetes | 1.36.4 | Available in the user's East US version output |
| AKS tier | Standard, KubernetesOfficial support | Follow the README's managed control-plane design |
| System pool | Two Standard_D4s_v5 nodes | Dedicated capacity for Kubernetes support services |
| Application pool | Two to four Standard_D4s_v5 nodes | Start with two; cluster autoscaler can add nodes for unschedulable pods |
| OS disks | Managed, 64 GiB | Explicit node disk configuration |
| Network | Azure CNI Overlay with Cilium | Separate pod address range and support for network policies |
| Outbound internet | Standard Load Balancer, one managed outbound IP | Explicit outbound route for image downloads and Azure service connections |
| API access | Public endpoint, Entra authentication and Azure RBAC | Reachable from the user's browser Cloud Shell |

A node is a VM. Each chosen VM has four vCPUs, so the initial four nodes use
16 vCPUs. At six nodes the pools use 24 vCPUs; upgrades can temporarily add
nodes as well. User-reported East US limits are 705 regional vCPUs and 65
Standard DSv5 family vCPUs, both with zero usage at the time of the check.
Quota permits this starting design but does not guarantee VM SKU capacity.
No availability-zone placement is configured in this first lab cluster.

The system pool has a `CriticalAddonsOnly` taint: ordinary application pods
cannot schedule there without a matching toleration. Application nodes have
the `workload=trafficxx` label. Node autoscaling is different from scaling API
replicas; HPA/KEDA and application resource requests come later. Terraform
ignores changes to the application pool's current node count so that it does
not undo the cluster autoscaler's decisions.

## Identity means who can do what

Two managed identities are created before the cluster:

- The control-plane identity receives Network Contributor on the AKS subnet
  and Managed Identity Operator on the kubelet identity.
- The kubelet identity receives AcrPull on this registry. It can download
  images without enabling the registry's shared admin login.

The intended human administrator is supplied explicitly as
`aks_admin_object_id` and receives Azure Kubernetes Service RBAC Cluster Admin
on this cluster. This avoids silently switching the administrator to a future
CI identity. Entra authentication is enabled and local cluster accounts are
disabled. Keep supplying the same intended administrator on future runs.

OIDC and workload identity are enabled for future applications. No application
federated credentials or database/queue permissions are created in this step.

The public API has no source-IP allowlist yet; authentication is required.
Cilium supports network policy, but no application-deny policies are installed
yet. The logging workspace exists, but this step does not enable log collection.
Kubernetes version upgrades remain explicit; node-image updates use the NodeImage
channel. Hardening, maintenance windows, workload rollouts, monitoring, ingress,
and recovery experiments remain later work.

## User-run Cloud Shell steps

Keep using the existing `~/trafficxx/foundation` folder and its backend.
Upload the generated `trafficxx-aks-update.zip`, which contains only `aks.tf`,
`variables.tf`, and `outputs.tf`. It does not contain or overwrite state,
backend configuration, or private variable values.

```bash
cd ~/trafficxx/foundation
unzip -o ~/trafficxx-aks-update.zip -d ~/trafficxx/foundation
export TF_VAR_aks_admin_object_id="$(az ad signed-in-user show --query id --output tsv)"
terraform validate
```

The exported value lasts only for the current shell session. For continued use,
store `aks_admin_object_id` in a private, ignored `aks-admin.auto.tfvars` file
instead. The value is the user's Entra object ID, not the subscription ID.

Before creating a plan, check SKU restrictions for this subscription:

```bash
az vm list-skus --location eastus --resource-type virtualMachines \
  --size Standard_D4s_v5 --all \
  --query "[?name=='Standard_D4s_v5'].{Name:name,Restrictions:restrictions}" \
  --output json
```

An empty restrictions list is a useful preflight check, not a capacity guarantee.
After reviewing the validation and SKU results, generate a fresh plan:

```bash
terraform plan -out=aks.tfplan
```

Expected Terraform additions: two managed identities, four role assignments,
one AKS cluster (including its system pool), and one application node pool:
**eight additions**, with no changes or removals to the existing seven resources.
AKS also creates backing resources in `trafficxx-lab-aks-nodes-rg`, including
VM scale sets, disks and outbound networking; they are not individual additions
in Terraform's eight-resource count. Read the plan before applying it.

Do not reuse the old `foundation.tfplan`, which predates AKS. After reviewing
the new plan, the user can run `terraform apply aks.tfplan`. Deployment and
permission propagation may take time. If it fails, retain the state, inspect
the specific error, and create a fresh plan after correcting the cause.

## Verification and limits

Local formatting and AzureRM 5.10.0 schema validation passed. Validation first
found missing Entra tenant configuration; this was fixed using the current
provider tenant. No local Azure credentials were accessed. The user then verified
an empty VM SKU restriction list, reviewed an eight-addition plan, applied it,
and connected through `az aks get-credentials` and kubelogin's Azure CLI mode.
All four nodes reported Ready at v1.36.4. The user also supplied a snapshot of
all listed system pods Running with their containers ready. Two workload-identity
webhook replicas each had one earlier restart. A subsequent all-pod Ready wait
succeeded and the two restart counts stayed at one. These observations support
startup recovery; the original restart cause and long-term stability remain
unverified. Image pulling, application scheduling, autoscaling, and failure
recovery still need testing.

Opening the OIDC issuer root in a browser returned a 404. The issuer identifies
the cluster's token issuer; its root is not a normal website. Inspect public
metadata at `<issuer>/.well-known/openid-configuration` (use one slash between
the issuer path and `.well-known`). Root 404 alone does not establish that OIDC
is broken; discovery metadata and workload federation are separate checks.
The user subsequently supplied successful discovery JSON with the expected
issuer, a JWKS URL, and RS256 support. Application token exchange remains untested.

References: [system pools](https://learn.microsoft.com/en-us/azure/aks/use-system-pools),
[managed identities](https://learn.microsoft.com/en-us/azure/aks/managed-identity-overview),
[provider resource](https://registry.terraform.io/providers/hashicorp/azurerm/5.10.0/docs/resources/kubernetes_cluster).
