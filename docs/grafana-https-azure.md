# Grafana HTTPS on an Azure-provided hostname

The current Cloud Shell preview cannot retain the server-set test cookie. The
same probe confirms that a browser-written cookie is stored and forwarded. This
narrows the failure to response-cookie handling; it does not prove which relay
or browser component removes/rejects Set-Cookie. Grafana itself returns a
successful login and issues a cookie over the direct connection. Its provisioned
dashboard/datasource API checks passed. The replacement Azure HTTPS route now
passed GRAFANA_HTTPS_OK: trusted certificate, HTTP redirect, Secure HttpOnly cookie
login, unauthenticated dashboard denial, twelve panels, datasource up1 and
diagnostic session logout. The user subsequently confirmed successful browser
login and supplied screenshots of all twelve panels rendering with data. The
visible snapshot has scrape up1, zero outbox/processing backlog and active
warnings, fresh consumer/publisher heartbeats, processed history18, historical
dead-letter audits1 and publication failures2. These historical counters are not
current native queue/DLQ counts. The screenshots do not prove future availability
or notification delivery. An IDE-attached credential selection was exposed
again; the user explicitly declined additional rotation. No further password
change was performed or authorized after that instruction.

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
Certificate state persists on a separate volume. Initial deployment and trusted
HTTPS session checks passed. Certificate renewal and disk recovery have not
been verified yet. Browser rendering is now confirmed.

Before any listener is published, replace the exposed Grafana password in its
persisted database, revoke existing Grafana sessions, synchronize the bootstrap
Secret/private file, and verify the new credential. Merely changing the Secret
does not change an existing Grafana database user's password. Do not paste any
password/token or upload .env.grafana. The exposed preview auth token requires
its own invalidation; Grafana session revocation does not invalidate Azure's
relay session.

## First step: reserve the address, without publishing a listener

The user-run output confirms the address reservation:

- Hostname: trafficxx-grafana-e8708a85.g8ddfjd0h4ewaaf4.eastus.sysgen.cloudapp.azure.com
- IPv4: 20.124.118.213
- Public IP: trafficxx-lab-grafana-ip in trafficxx-lab-rg

The reviewed plan was two additions, zero changes/destroys. Apply outputs were
supplied, without a separate Apply complete line. These outputs do not establish
that a listener, certificate or working website exists.

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

## Rotate the exposed Grafana credential before publishing a listener

In Cloud Shell, with the lab Azure CLI account and AKS context selected:

```bash
cd ~/trafficxx
git pull --ff-only origin main
python3 scripts/rotate-grafana-password.py
```

The user-run result now confirms GRAFANA_PASSWORD_ROTATED, sessions_revoked=true,
remaining_sessions=0, secret_synchronized=true, password_printed=false and the
private .env.grafana path. Do not repeat rotation just to deploy the gateway.

The first user attempt returned GRAFANA_ROTATION_FAILED/RuntimeError without
step information. The helper now reports a safe stage and HTTP status (null for
non-HTTP errors), never raw response bodies/headers or exception messages. Pull
the update, rerun the same command and share only that JSON to locate the failure.
The original result does not establish whether the password was changed.

The next user-run result located HTTP400 at revoke_grafana_sessions, after
password verification. The pinned Grafana13.2.4 source rejects bulk logout of
the current administrator. The helper now lists that user's auth tokens and
revokes each remaining token with revoke-auth-token, then requires an empty
readback. The password-update handler itself also revokes that user's tokens,
so the list may already be empty. No second administrator or extra permission
is introduced. Pull the fix and rerun using the retained pending credential;
the subsequent user result confirmed successful completion.

The helper uses a temporary localhost-only kubectl port-forward on18091, not
Cloud Shell Web Preview or a local PC account connection. It checks the current
subscription/context before starting and closes its forward on completion or
failure. It reads the existing Grafana Secret into memory, validates an
existing private .env.grafana or restores a lost one, authenticates the actual
Grafana administrator, and saves a strong replacement in .env.grafana.next
with0600 permissions. No password is passed in a process argument or printed.
HTTP redirects and environment proxies are disabled for credentialed API calls.

It updates the persisted Grafana password via the authenticated Admin API,
verifies the new credential and rejection of the previous Secret password,
revokes all Grafana auth tokens for that administrator and verifies an empty
token list. It replaces the Kubernetes Secret via stdin with its resourceVersion
to reject concurrent edits, verifies the data, removes any stale last-applied
annotation, and promotes the pending file to .env.grafana. Other users' sessions
and the Azure relay auth token are not managed by this helper. Existing Grafana
Basic authentication is retained; no anonymous mode or auth proxy bypass is used.

If a step fails, keep .env.grafana.next private and rerun the same command. The
helper tries the pending credential so an interruption after the database update
or Secret update can resume without generating another password. If a new Cloud
Shell session loses that pending file before Secret synchronization, automatic
recovery is not guaranteed; preserve both private files until success. Never
commit, upload or paste either file. A mismatched private file without matching
pending recovery data stops before password mutation.

After success, retain the updated private file for the later website login.
Grafana database/session mutation, Secret synchronization and localhost
connectivity passed the user-run helper. No public Service was created by rotation.

## Deploy the HTTPS gateway

The deployment helper requires the correct sponsorship account/AKS context,
actual reserved public-IP metadata, Azure Disk storage class, a healthy Grafana,
the rotation annotation, matching private credentials and no unfinished pending
rotation. It checks the password against the actual administrator over an owned
temporary localhost-only forward. For the initial public Service it also requires
an empty Grafana session list. Later redeploys permit legitimate active sessions.
It can restore a lost private file from the synchronized Secret without rotating.

```bash
cd ~/trafficxx
git pull --ff-only origin main
python3 scripts/grafana-https.py deploy
```

The helper sets Grafana's canonical root_url to the actual Azure hostname,
serve_from_sub_path=false, secure cookies=true, SameSite=lax, anonymous=false and
signup=false. It waits for Grafana's rollout, applies the gateway ConfigMap,
ServiceAccount,5Gi managed-csi certificate PVC and one Recreate Caddy2.11.7-alpine
replica, then publishes its LoadBalancer Service only after gateway rollout.
The Caddyfile is validated in an init container first. Existing gateways restart
to reload configuration; both Grafana and certificate PVCs are retained.

Caddy's unprivileged8080/8443 listeners map to public80/443. Its8085 health
listener is used only by pod probes and is not in the public Service. Readiness
tests process health, not certificate issuance. HTTP redirects to HTTPS;
automatic HTTP/TLS ACME challenges require public connectivity for issuance and
renewal. The configuration selects Let's Encrypt explicitly. The gateway has no
Kubernetes API token/RBAC, Azure identity, database/broker grants or mounted
Grafana password. It runs non-root with a read-only root filesystem, no Linux
capabilities and bounded resources. Certificate/account keys persist on its PVC;
config/temp paths use bounded emptyDir volumes. HTTP/3 is disabled, so only TCP
is published. Unauthenticated Grafana /metrics and /debug routes are blocked at
the gateway. Grafana's internal Service remains ClusterIP; no other app Service
is changed. There is no access-log configuration that prints cookies/passwords.

Expected GRAFANA_HTTPS_GATEWAY_DEPLOYED with the actual reserved IP, url and
tls_verified=false. An Azure Disk/image pull or IP role propagation may delay
rollout. The helper checks the Service attaches exactly the reserved IP. Its
deployment result does not prove certificate trust or successful login.
The first user-run deployment stopped at rollout_gateway. Its image pulled and
certificate PVC bound/attached, but validate-config entered Init:CrashLoopBackOff
with exec /usr/bin/caddy: operation not permitted. The official image Dockerfile
sets cap_net_bind_service=ep on that binary; executing it with drop:ALL can be
rejected before Caddy starts. The first Windows configuration validation did not
test this Linux file-capability interaction.

The manifest now starts the init container with /bin/sh, copies only Caddy's
bytes into a fresh file in a128Mi bounded emptyDir, sets mode0555 and executes
that copy for validation. The gateway runs the same copy from a read-only
mount. Reinitialization removes only that temporary binary before recreating it.
The new file has no inherited file capability. No root, privilege escalation or
additional capability is granted; only high ports are bound inside the pod.
The subsequent GRAFANA_HTTPS_GATEWAY_DEPLOYED confirmed corrected Linux startup
and attachment to20.124.118.213. GRAFANA_HTTPS_OK then confirmed trusted HTTPS and
cookie authentication. Existing PVCs/Secret were retained by the helper.

## Verify trusted HTTPS and the session cookie

```bash
python3 scripts/grafana-https.py check
```

The check waits with a bounded retry for DNS to match the reserved IP, a normal
CA/hostname-verified HTTPS connection, healthy Grafana, a correct HTTP-to-HTTPS
redirect, protected user/dashboard APIs returning401 without login, and blocked
diagnostic routes. It never uses -k or an unverified SSL context and disables
environment proxies/automatic redirects. Plain HTTP is restricted to the
credential-free redirect probe. No credentials are submitted while TLS/DNS is
unready; only the read-only readiness phase retries.

It then submits one login using the private credentials, requires the root
redirect and a Secure HttpOnly grafana_session cookie, and uses that cookie
(not Basic auth) to read the current administrator and verify all twelve
provisioned panels, datasource health and the real Prometheus up query. It logs
out only its diagnostic session in finally and requires the subsequent /api/user
request to return401. No password/header/cookie values are printed. The actual
user-run GRAFANA_HTTPS_OK confirmed certificate_verified, http_redirect_verified,
cookie_session_authenticated, session_cookie_secure_httponly,
unauthenticated_dashboard_denied and diagnostic_session_logged_out all true,
panel_count12 and datasource_up1. Notification delivery remains unconfigured.
This proves an HTTP cookie session; the subsequent user screenshots separately
confirmed browser rendering.

After GRAFANA_HTTPS_OK, open the returned https URL in a normal browser tab and
select Dashboards -> trafficXX -> trafficXX | Operations. Use the updated
~/trafficxx/.env.grafana in Cloud Shell; an old PC/IDE copy is not automatically
updated. Do not paste credentials into chat. No Cloud Shell preview or persistent
port-forward is needed for this public route.

## Inspect or stop the listener

If deployment fails, share only the safe GRAFANA_HTTPS_FAILED stage/type/status.
Keep the Secret and all PVCs. Useful user-run infrastructure checks are:

```bash
kubectl get pods,pvc,service -n trafficxx-monitoring
kubectl describe service grafana-https -n trafficxx-monitoring
kubectl logs -n trafficxx-monitoring deployment/grafana-https -c validate-config
kubectl logs -n trafficxx-monitoring deployment/grafana-https -c gateway --tail=60
```

Review logs before sharing; never dump Secret JSON, credential files or
certificate private keys. If IP assignment cannot find/access the reserved IP,
inspect the exact Service annotations and the scoped control-plane role before
changing permissions. Certificate failures require checking DNS/public reachability
and CA responses, not bypassing TLS validation or repeatedly deleting certificate
state (which can trigger issuance limits).

To intentionally remove public access, delete only the gateway Service:

```bash
kubectl delete service grafana-https -n trafficxx-monitoring
```

This is different from stopping a local port-forward. It removes the public
listener while retaining application data/certificate state. The Terraform IP
reservation and its charges remain; detach the Service before planning removal
of that reservation. Do not delete the namespace/PVCs as a troubleshooting step.

## Sources and local validation

- [AKS static public IP and DNS label](https://learn.microsoft.com/en-us/azure/aks/static-ip)
- [AzureRM5.10.0 public IP schema](https://github.com/hashicorp/terraform-provider-azurerm/blob/v5.10.0/website/docs/r/public_ip.html.markdown)
- [Caddy automatic HTTPS reverse proxy](https://caddyserver.com/docs/quick-starts/reverse-proxy)
- [Grafana password administration](https://grafana.com/docs/grafana/latest/administration/cli/)

Terraform formatting and backend-free isolated validation with the pinned
AzureRM provider passed locally. No Azure state, login or plan was accessed
locally. Live DNS availability, scoped IP attachment, certificate issuance and
browser login remain user-run checks.

Fourteen offline rotation methods cover successful password/session/Secret/file
updates, interrupted synchronization and recovery, retained-session failure,
non-admin/stale/mismatched credentials, old-password rejection and redirect
refusal, safe failure stages and redacted HTTP status reporting, rejection of
self-logout, multiple/zero tokens and malformed token metadata. Sixteen additional
offline gateway methods cover credential/deployment ordering, failures before
publication, IP checks, existing rollouts, rotation/private-file gates, DNS,
cookie login/redirect/logout, bounded retry and no plaintext credentials. All320
offline methods pass; no real Grafana mutation was run locally.

The official Caddy2.11.7 Windows validator was downloaded to ignored test tools
and matched the release SHA512 checksum. It validated the actual Caddyfile with
isolated writable test paths. Kubernetes manifest selectors/ports/ConfigMap/
identity/security/PVC wiring and Python syntax/LF checks passed. No local Caddy
web server, container, ACME issuance, Docker restart or Azure account access ran.
The official Docker-library metadata confirms the2.11.7-alpine image tag; this
version tag is not a digest pin and no image vulnerability scan is claimed.

The self-logout restriction and password-update revocation are checked against
[Grafana13.2.4 source](https://github.com/grafana/grafana/blob/v13.2.4/pkg/api/admin_users.go),
in addition to the Admin API documentation.

- [Caddy global ports and protocols](https://caddyserver.com/docs/caddyfile/options)
- [Caddy automatic HTTPS requirements](https://caddyserver.com/docs/automatic-https)
- [Official Caddy image metadata](https://github.com/docker-library/official-images/blob/master/library/caddy)
- [Pinned official image Dockerfile and file capability](https://github.com/caddyserver/caddy-docker/blob/9234f5785658cae8129bfae366e196d0670468ac/2.11/alpine/Dockerfile)
