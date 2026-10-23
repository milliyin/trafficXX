# Private Prometheus history collector on AKS

Operations already exposes verified monitoring metrics. This step adds a private
Prometheus server to collect and retain samples, so Grafana can later display
history. The user supplied PROMETHEUS_SCRAPE_OK with actual stored operations samples;
ordinary restart/persistence is not yet tested. No notification
routing or Grafana dashboard is created in this step.

## Prepared resources and access

Namespace trafficxx-monitoring contains a dedicated prometheus service account,
one Recreate Deployment, ClusterIP Service9090 and a5Gi ReadWriteOnce PVC using
managed-csi (Azure Disk). The server and init validator use the official
quay.io/prometheus/prometheus:v3.15.1 release tag; this is a version tag, not an
image-digest pin. The release was checked against the
[official v3.15.1 release](https://github.com/prometheus/prometheus/releases/tag/v3.15.1).
The actual node image pull and init promtool validation remain user-run checks.

The config scrapes operations.trafficxx.svc.cluster.local:80/metrics every15seconds
and Prometheus itself every30seconds. These are static targets: no cluster-wide
service discovery, Kubernetes API role, workload federation, database access or
Service Bus send/receive permission is required for the collector. Service-account
token mounting is disabled. Namespace restricted Pod Security, non-root UID/GID/
fsGroup65534, read-only root filesystems, dropped capabilities and bounded CPU/
memory apply. The collector writes only its /prometheus data volume.

Retention is configured for7days or3GB of TSDB blocks, whichever limit is reached
first. The5Gi PVC leaves room for WAL/head/temporary overhead. These are limits,
not a guarantee that7days are always retained. The disk has storage charges under
the existing lab subscription; no new Azure Terraform resources/roles are added.
The existing CSI driver provisions the disk through Kubernetes. See
[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/) and
[AKS Azure Disk storage classes](https://learn.microsoft.com/en-us/azure/aks/create-volume-azure-disk).

Single-instance disk storage is not HA or a backup. Recreate/config updates
pause collection. Keeping the PVC is intended to retain data across ordinary
pod replacement; restart persistence has not yet been tested. Deleting the PVC
can delete its Azure Disk and history under the storage class reclaim policy.
Never delete the namespace/PVC just to stop a preview or collection process.
The internal collector UI/API has no application login configured, and no public
Ingress/LoadBalancer is created. Access remains within the lab cluster and
explicit port-forwarding; this is not a public dashboard deployment.

## Deploy and verify actual collected samples

Stop the earlier interactive Cloud Shell preview with Ctrl+C if it is still
running. Then execute:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/deploy-prometheus.sh
bash scripts/check-prometheus-http.sh
```

Expected PROMETHEUS_DEPLOYED followed by PROMETHEUS_SCRAPE_OK. Keep Cloud Shell
open until completion; new disk provisioning can take several minutes.

The deployment helper checks subscription/context, managed-csi provisioner
(disk.csi.azure.com) and operations rollout before creating resources. It applies
the namespace, ConfigMap, service account/PVC/Deployment/Service and waits for
rollout. promtool check config runs in the init container before the server.
When rerun for an existing collector, the helper restarts that single Deployment
to reload/revalidate the current config; it preserves the PVC and does not touch
application Deployments.

The checker uses a temporary localhost-only port-forward on18090 and reads the
Prometheus health/readiness, targets and query APIs. It requires exactly one
healthy trafficxx-operations target, up=1, a sample age0-45seconds, and at least
two REAL up samples within2minutes using count_over_time (not repeated query
interpolation). It also requires nonempty processed history, zero outbox/
processing backlog and no active alert gauges for the idle baseline. The
historical dead-letter count can remain1 without failing. These are stored
SQL-derived operations signals, not native broker queue depth.

The checker makes up to24attempts at five-second intervals, with15second HTTP
request timeouts. It prints only aggregate numeric results, no raw scrape errors.
The user supplied PROMETHEUS_SCRAPE_OK: operations target up1, latest sample age
5.842seconds, two actual up samples within2minutes, processed_total18, zero
outstanding/unpublished/active alerts and historical_dead_letters1. No separate
PVC Bound/collector rollout transcript was supplied. The real stored sample
result verifies functioning collection, not disk recovery across a restart.

Useful read-only diagnostics if deployment/checks fail:

```bash
kubectl get pods,pvc -n trafficxx-monitoring -o wide
kubectl logs -n trafficxx-monitoring deployment/prometheus -c validate-config
kubectl logs -n trafficxx-monitoring deployment/prometheus -c prometheus --tail=50
```

Seven new offline methods cover healthy stored samples, missing/down/wrong/
duplicate targets, stale/single-sample insufficiency, unhealthy backlog/alerts,
nonfinite/empty query results, successful retry and bounded failure (279 total).
Six mocked deploy cases passed: new/existing collector, wrong subscription,
context/storage class and apply failure. YAML wiring/config/security/PVC/Service,
Python and Bash syntax/LF checks passed. Local promtool/container execution was
not performed. The subsequent user-run PROMETHEUS_SCRAPE_OK confirmed a healthy
target and at least two real stored samples; separate init-container logs were
not supplied.
No local Docker restart or Azure account access occurred.

Scrape evidence passed; the [private Grafana dashboard](grafana-azure.md) is
now API-verified with twelve provisioned panels and a healthy datasource pointing to
http://prometheus.trafficxx-monitoring.svc.cluster.local:9090. Alert rules,
notification routing and native Azure queue metrics remain separate integrations.

## Browse pool extension

The [Browse pool monitoring guide](browse-pool-monitoring.md) adds a separate
DNS-discovered job for each Ready Browse pod and a new Grafana dashboard.
The ca8 Browse image/deployment and monitoring update passed. Separate
BROWSE_PROMETHEUS_OK evidence confirmed two healthy targets matched to serving
pod IPs, each with two stored samples and valid pool statistics. The new Grafana
dashboard API check also passed; browser rendering remains pending. The original
Operations scrape evidence above is separate from this new job's proof.
