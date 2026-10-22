# Private Grafana dashboard for trafficXX

Prometheus collection passed in Azure: target up1, two actual samples, latest
sample5.842seconds old,18processed events, zero pending/outstanding/active warnings
and one historical dead-letter audit count. Dashboard deployment/browser results
are still pending. The next step provisions twelve Grafana panels against the
existing private Prometheus server, not a separate metrics or booking database.

## What is prepared

The dashboard trafficXX | Operations (UID trafficxx-operations) has panels for
scrape health, unpublished/processing outstanding work, active warnings, backlog
trends and age, worker heartbeat age/presence/errors, warning history, processed
events, historical dead-letter audit records and historical publication failures.
Historical counters are labelled as history, not current native queue/DLQ depth.
Charts use only the trafficxx-operations scrape job and refresh every15seconds.
They start with recent data only; metrics from before the collector started
cannot be reconstructed. Native Azure queue counts and notification delivery are
not integrated by this dashboard.

Grafana13.2.4 uses an official version tag, not a digest pin. Its
[release](https://github.com/grafana/grafana/releases/tag/v13.2.4),
[file provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
and [secret-file configuration](https://grafana.com/docs/grafana/latest/setup-grafana/configure-docker/)
were checked. Actual image pull/server startup/provisioned API compatibility
will be verified in the user-run rollout. No vulnerability scan is claimed here.

There is one Recreate replica in trafficxx-monitoring with a dedicated5Gi
managed-csi PVC for the Grafana SQLite database/settings. The datasource/dashboard
files are ConfigMaps tracked in Git. The container runs as UID/GID472 with fsGroup,
read-only root filesystem, dropped capabilities and bounded resources. Writable
paths are its data volume and bounded /tmp. The Service is ClusterIP3000; no
public IP/Ingress, cloud workload identity, Kubernetes API RBAC or database/broker
grant is added. Anonymous access and self-signup are disabled. This is a single
lab instance, not HA or a backup; preserve the PVC and credential Secret.

## Bootstrap credentials

The deploy helper calls ensure-grafana-secret.py inside the user's Cloud Shell.
It generates a strong admin password, saves .env.grafana with mode0600, and creates
the exact grafana-admin Secret through kubectl stdin, never password command
arguments or a printed dry-run. Existing Secrets are reused; if a new Cloud Shell
lost the local file, that exact Secret can restore the private file. A local/Secret
mismatch is rejected rather than silently rotating or overwriting credentials.
Symlinks are rejected. .env.grafana is already ignored by Git and Docker context.
No real credentials are created or read in the shared local workspace by this step.

Grafana reads the username/password from mounted Secret files using __FILE
configuration. The diagnostic Job uses the same Secret read-only and logs safe
metadata only. The Secret contains a bootstrap password, not a rotation system:
changing an environment file or Secret alone does not reset an existing Grafana
SQLite user's password. Do not delete the Secret/PVC pair or change credentials
while completing initial validation. Never paste the password or Secret JSON
into chat or commit the file.

## Deploy and validate the provisioned dashboard

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/deploy-grafana.sh
bash scripts/check-grafana-dashboard.sh
```

Expected GRAFANA_CREDENTIALS_READY (password_printed=false), GRAFANA_DEPLOYED and
GRAFANA_DASHBOARD_OK. A new Azure Disk/image pull can take several minutes. The
validation Job runs inside AKS with the Secret mounted; it does not require
credentials in the shell command or local port-forwarding. It checks database
health, the exact provisioned datasource/12-panel dashboard, datasource health,
an actual Prometheus up query through Grafana and rejection of an unauthenticated
dashboard API request with401. The result must contain no login/password values.
Actual rollout, auth checks, provisioning and query output remain pending.

The deploy helper prechecks subscription/context/storage class and Prometheus
rollout, then applies config and resources. It restarts an existing single Grafana
Deployment to reload provisioning, retaining its PVC. The dashboard source is
not editable via the UI; future changes should be made in Git provisioning files.
The helper does not restart Prometheus or business workloads.

Eleven added offline methods cover bootstrap creation/private-file reuse/lost-file
recovery/mismatch rejection/weak credentials and dashboard/database/datasource/
query errors (290total). Seven mocked deploy cases pass, including credentials
setup failure before applying Grafana resources. Bash/Python syntax, YAML image/
Secret/identity/storage/Service wiring and dashboard references were validated.
A Grafana container/browser was not run locally; actual cloud UI remains pending.

## Opening the browser comes after API validation

The bootstrap login will be in ~/trafficxx/.env.grafana in Cloud Shell; view it
privately when logging in, and do not paste its contents. We will use an explicit
port-forward and Cloud Shell Web Preview for the private Service. Cloud Shell's
preview URL can include a reverse-proxy path; Grafana may need its root_url
configured for that actual URL/path so login redirects/assets work correctly.
Do not guess a public hostname or assume localhost links work through the preview.
See [Grafana reverse-proxy configuration](https://grafana.com/tutorials/run-grafana-behind-a-proxy/).
The exact browser preview route will be checked after GRAFANA_DASHBOARD_OK; no
public dashboard URL has been provisioned. External notifications remain future
work and no messages are sent to any email/chat service by this setup.
