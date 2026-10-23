# Browse pool metrics and Grafana

Status: ca8 image build, deployed Browse demo HTTP, monitoring configuration,
Prometheus stored-sample proof and Grafana dashboard API verification passed.
The user also confirmed browser rendering with a screenshot of all eight panels
and live data. The ca7 bounded pool/concurrency milestone is separate evidence.

## Verified Azure checkpoint

User-run ACR ca8 succeeded after37seconds, tag browse-20261023154334-7633:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:f09d49ce8044461be8dbde85835888fec67a5ca5a2d848b20ed422da211c1e51
```

Its reported Python base matches ca7. After the deployment instructions, the
user supplied BROWSE_DEMO_HTTP_OK: alive/ready, two events and100available units
per demo event. Monitoring configuration then created the headless Service,
updated the collector ConfigMap, successfully rolled out Prometheus and updated
the two-dashboard ConfigMap. This rollout passed the configured promtool init;
no separate validation log was supplied.

BROWSE_PROMETHEUS_OK correlated two healthy targets with serving pod IPs:

| Target | Stored scrapes | Freshness seconds | Slots / limit | Available | Waiting | Borrows / attempts |
| --- | --- | --- | --- | --- | --- | --- |
| 10.244.3.23:8000 | 2 | 8.545 | 2 / 4 | 2 | 0 | 11 / 2 |
| 10.244.3.246:8000 | 2 | 7.191 | 1 / 4 | 1 | 0 | 6 / 1 |

Both targets reported zero borrow errors, connection errors, lost connections
and bad returns at this checkpoint. These are process-local counters, not a
long-duration performance or renewal result.

GRAFANA_BROWSE_POOL_OK confirmed the provisioned eight-panel dashboard, existing
datasource, two targets and stored samples through its proxy. Original Operations
dashboard verification passed too; unauthenticated dashboard access was denied.
External notification delivery remains unconfigured. Certificate renewal and
persistence/restart recovery were not tested here.

A subsequent ca8 serving-pod test returned BROWSE_CONCURRENCY_OK for pods
browse-6775788b9b-g649w and browse-6775788b9b-k6fdk. Each served30test reads and
reported32pool borrows. New connection attempts were one/two, with sampled peak
pool sizes two/three against maximum four. Inventory was unchanged.

The user then supplied a browser screenshot showing all eight panels with data:
two scrape targets up, pool slots below the limit, zero sampled waiting and
error/lost/bad-return rates, and increased borrow rates following the test.
Visible current-process totals were109borrows and six connection attempts,
including readiness activity. These are not109customer requests, six concurrent
connections, or durable lifetime totals. The busy/opening panel showed one slot
for one target near the end; this does not identify an active SQL query. Scrape
sampling does not rule out brief waits between observations. Browser rendering
and the bounded reuse/monitoring milestone are now verified.

## What is measured

Browse exposes /metrics using its existing process-local pool statistics. It
does not borrow a SQL connection, reset counters or return credentials, SQL,
customer data, host configuration or process identifiers. The endpoint stays
behind internal services; exclude it and /internal paths from any future public
Browse route. Scrape success does not itself establish database health.

Gauges report pool slots, the configured limit, idle available connections and
waiting borrowers. Counters report borrow requests/errors, physical connection
attempts/errors, lost connections and unusable returns. Missing zero-valued
library counters are exported as zero. Counters reset when the process restarts;
these are not durable booking/event totals. Rates use a five-minute window and
need multiple scrape samples before they can be displayed.

Pool size includes connections being prepared. The dashboard's busy/opening
value is size minus idle available slots; it is not the number of active SQL
queries. Connection attempts are not simultaneous open connections. Waiting
counts borrowers inside the pool, not every HTTP request waiting elsewhere.

## Each pod is scraped separately

A private headless browse-metrics Service selects Ready Browse pods. Prometheus
uses DNS A discovery on browse-metrics.trafficxx.svc.cluster.local, port8000,
refresh30s, with job trafficxx-browse-pool and the existing15s scrape interval.
Each target's instance label is its pod IP/port. Scraping the load-balanced
Browse ClusterIP would mix independent process counters under one target.

DNS discovery needs no Kubernetes API token or new RBAC grants. The existing
Prometheus service account still has automount disabled. No new public listener,
Terraform resource, SQL permission or Azure role is added.

References: [Prometheus DNS discovery](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#dns_sd_config),
[Kubernetes headless Services](https://kubernetes.io/docs/concepts/services-networking/service/#headless-services),
and [Psycopg pool statistics](https://www.psycopg.org/psycopg3/docs/advanced/pool.html#pool-stats).

## Dashboard

A separate provisioned dashboard has UID trafficxx-browse-pool and title
trafficXX | Browse database pool. Its eight panels show scrape targets up,
busy/opening slots, slots versus limit, waiting borrowers, borrow/connection
attempt rates, error rates, lost/unusable connection rates and current-process
totals. The existing twelve-panel Operations dashboard stays in the same folder.
Both reference the existing trafficxx-prometheus datasource. No notification
delivery or new alert rule is configured by this step.

## User-run Cloud Shell steps

First build the new Browse source; record the returned immutable image reference:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-browse-image.sh
```

After reviewing IMAGE_BUILD_OK, deploy Browse and confirm the demo HTTP baseline:

```bash
bash scripts/deploy-browse.sh &&
bash scripts/check-browse-http.sh --demo
```

Next update only monitoring configuration:

```bash
bash scripts/deploy-browse-monitoring.sh &&
bash scripts/check-prometheus-http.sh --browse-pool &&
bash scripts/check-grafana-dashboard.sh --browse-pool
```

The guarded update creates the headless Service, replaces Prometheus's ConfigMap
and restarts only the collector to run its existing promtool validation and load
the new job. The existing metrics PVC is retained; there is a brief collection
gap during this single-replica restart. It also updates the dashboard ConfigMap
with both JSON files. Grafana projects and polls it automatically; allow a few
minutes. This helper does not apply Grafana's base Deployment, restart Grafana,
read/change Secrets, rotate passwords or change the working HTTPS/root URL.

The Prometheus check requires two distinct healthy targets matching the Ready
serving pod IPs, every expected pool series for both instances, fresh samples,
at least two stored scrapes, valid pool bounds and no queued borrowers at the
healthy baseline. Historical process error counters may be nonzero; rates and
their context need inspection. It prints BROWSE_PROMETHEUS_OK only after this
evidence, with a bounded wait for discovery and samples.

The separate grafana-browse-pool-check Job uses the existing Secret mounted
inside AKS, never prints credentials and makes read-only Grafana queries. It
requires the eight-panel provisioned dashboard, protected unauthenticated access,
the original Operations verification and real pool samples through Grafana's
datasource proxy. It prints GRAFANA_BROWSE_POOL_OK. The Job has a360s deadline
and a600s completion TTL. If the name already exists, inspect its logs before
deleting only that diagnostic Job for a fresh rerun; no PVC/Secret deletion is
needed. This private API check does not replace browser chart-rendering proof.

After successful checks, open the existing authenticated HTTPS Grafana address,
then Dashboards > trafficXX > trafficXX | Browse database pool (path
/d/trafficxx-browse-pool). To observe activity, run the already-verified bounded
read test while watching the graphs:

```bash
python3 scripts/check-browse-concurrency.py
```

Readiness checks also create borrows; Prometheus scrapes do not. A fast burst
can fall between scrapes, so a low waiting gauge or slot sample does not prove
that no short-lived wait occurred. Counter increases retain evidence of activity;
this is not continuous per-request tracing, a performance SLO or token-renewal
proof. Do not claim live dashboard success until the user supplies those results.

## Local validation

Ten offline cases cover metric types/redaction, invalid values, two-target/sample
correlation, missing/stale/nonfinite/duplicate data, bounds and datasource/dashboard
requirements. All360offline methods pass. Actual pinned-library/FastAPI smoke
checks passed /metrics output with no pool borrowing. Bash syntax, YAML/JSON
structure and a mocked config-only deployment passed. The real SQL HTTP test was
extended with no-borrow metrics assertions; Docker remains stopped and hosted
CI is unchecked. The subsequent live stored-sample and dashboard API checks
and browser rendering passed as recorded above. Sustained-load and token-renewal
results remain pending.
