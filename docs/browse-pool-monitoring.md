# Browse pool metrics and Grafana

Status: implemented and tested locally; new Browse image build/rollout,
Prometheus scrape proof, dashboard API verification and browser rendering are
pending. The ca7 pool/concurrency milestone remains verified separately.

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
CI is unchecked. The collector's real promtool startup check and all live new
metrics/dashboard results remain pending.
