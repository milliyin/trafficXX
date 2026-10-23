# Grafana HTTPS on an Azure-provided hostname

The current Cloud Shell preview cannot retain the server-set test cookie. The
same probe confirms that a browser-written cookie is stored and forwarded. This
narrows the failure to response-cookie handling; it does not prove which relay
or browser component removes/rejects Set-Cookie. Grafana itself returns a
successful login and issues a cookie over the direct connection. Its provisioned
dashboard/datasource API checks passed. Browser dashboard access remains pending.

The user chose an Azure-provided hostname and Cloud Shell-only administration.
No local Azure CLI login/account connection is required.

## Intended route

Browser HTTPS -> Azure Standard Load Balancer -> dedicated HTTPS reverse proxy
inside AKS -> private Grafana Service. Prometheus, Booking, Browse, PostgreSQL
and Service Bus remain private. Grafana anonymous access and signup stay disabled.
The public endpoint adds internet exposure of the Grafana login page. It is a
learning-lab endpoint, not production SSO, a WAF, HA or a completed security audit.

The proposed reverse proxy is Caddy with automated publicly trusted certificates.
It needs public DNS and reachable HTTP80/HTTPS443 for certificate validation and
renewal. HTTP is for ACME/HTTPS redirection, not plaintext credential submission.
Certificate state must persist on a separate volume. No certificate issuance,
renewal or browser access has been verified yet. Gateway manifests/deployment
and credential rotation are subsequent steps, not shipped in the first IP step.

Before any listener is published, replace the exposed Grafana password in its
persisted database, revoke existing Grafana sessions, synchronize the bootstrap
Secret/private file, and verify the new credential. Merely changing the Secret
does not change an existing Grafana database user's password. Do not paste any
password/token or upload .env.grafana. The exposed preview auth token requires
its own invalidation; Grafana session revocation does not invalidate Azure's
relay session.

## First step: reserve the address, without publishing a listener

Terraform adds an opt-in Standard/Regional static IPv4 address in the existing
East US lab resource group. DNS uses SubscriptionReuse protection, which adds
a hash to the Azure hostname. Use the returned fqdn; do not guess it. A Network
Contributor role is added for the AKS control-plane identity at this IP's scope.
No new identity or subscription-wide role is needed. The opt-in defaults false
and is persisted true in an ignored grafana-endpoint.auto.tfvars by the plan
helper. Preserve/recreate that setting in future Cloud Shell sessions: otherwise
a future plan would propose removing the optional resources.

The IP incurs Azure public-IP charges even before an HTTPS listener is attached.
The eventual listener also uses load-balancer capacity and a certificate data
disk. No new Azure resources have been applied by the coding agent.

In the user's Cloud Shell:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/plan-grafana-endpoint.sh
```

The helper checks subscription, Terraform >=1.16.5/<1.17.0 and the recorded lab
administrator. It reconnects the existing Azure AD authenticated foundation
backend and requires AKS/PostgreSQL/Service Bus in state before planning. It
does not use a new state key or recreate bootstrap. Known lab inputs are exported
for this command; existing tfvars retain their normal Terraform precedence, so
inspect every plan for changes to existing resources. If Graph user lookup or
backend access fails, stop rather than changing the identity or backend.

Expected plan: **2 to add, 0 to change, 0 to destroy**, plus the
grafana_public_endpoint output. The additions are:

- azurerm_public_ip.grafana[0]
- azurerm_role_assignment.aks_grafana_public_ip[0]

If existing resources change or disappear, resolve input/drift differences
before applying. Do not use -target to hide them. After review, applying the
saved plan reserves the address only:

```bash
cd ~/trafficxx/infrastructure/foundation
terraform apply grafana-endpoint.tfplan
terraform output -json grafana_public_endpoint
```

These outputs are public address metadata, not a working website. Share the
plan summary/output without secrets, then prepare rotation and the HTTPS
listener against that actual hostname. Public-IP permission propagation may
delay the subsequent AKS load-balancer assignment. This first step does not
change Grafana's existing URL/cookie settings or expose its ClusterIP.

## Sources and local validation

- [AKS static public IP and DNS label](https://learn.microsoft.com/en-us/azure/aks/static-ip)
- [AzureRM5.10.0 public IP schema](https://github.com/hashicorp/terraform-provider-azurerm/blob/v5.10.0/website/docs/r/public_ip.html.markdown)
- [Caddy automatic HTTPS reverse proxy](https://caddyserver.com/docs/quick-starts/reverse-proxy)
- [Grafana password administration](https://grafana.com/docs/grafana/latest/administration/cli/)

Terraform formatting and backend-free isolated validation with the pinned
AzureRM provider passed locally. No Azure state, login or plan was accessed
locally. Live DNS availability, scoped IP attachment, certificate issuance and
browser login remain user-run checks.
