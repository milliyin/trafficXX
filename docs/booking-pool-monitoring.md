# Booking pool metrics and Grafana

Status: source/local validation, Booking image build caa, rollout, HTTP pool,
live Prometheus stored-sample, Grafana API and browser-rendering checks passed.
The preceding ca9 pool/functional milestone passed;
see [Booking pooling](booking-database-pooling.md). The existing Browse renewal
Job continues on its original image and run, without restart or source changes.

## Verified image build

User-run ACR caa succeeded after35seconds, tag booking-20261024093201-5112:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:c94884448905a0e94f2e4b8502ed120767f8cf46a2aa2f8747862ad5f6a663f0
```

The reported Python base digest matches ca9. The helper saved the immutable
reference to booking-image.txt in Cloud Shell; the committed Booking manifest
now pins this reviewed image too. Build success is not rollout, metrics scrape,
dashboard or hosted CI proof.

## Verified rollout and HTTP checkpoint

The user supplied a successful caa Booking rollout. New pods
booking-67b7966d6c-npkcg and booking-67b7966d6c-tmw88 were Ready1/1 with zero
restarts on different apps nodes. One ca9 pod was still Terminating in the
snapshot; this is not evidence of node-failure resilience.

BOOKING_HTTP_OK confirmed alive/ready, the expected API and customer-scoped
missing-reservation404. The same forwarded process reported four pool borrows,
zero new connection attempts, sampled size1/max4 and successful readiness after
the lookup rollback. No inventory writes occurred. This bounded check supports
one serving process's reuse, not both-replica sustained-load behaviour.
Monitoring configuration and live datasource verification subsequently passed
as recorded below.

## Verified monitoring checkpoint

The user supplied a successful Prometheus rollout, BOOKING_MONITORING_CONFIGURED,
BOOKING_PROMETHEUS_OK and GRAFANA_BOOKING_POOL_OK. The two healthy targets matched
the new Booking pods and exposed fresh stored samples:

| Target | Stored scrapes | Freshness seconds | Size / limit | Available | Waiting | Borrows / attempts |
| --- | --- | --- | --- | --- | --- | --- |
| 10.244.2.66:8000 | 2 | 7.515 | 1 / 4 | 1 | 0 | 13 / 1 |
| 10.244.3.138:8000 | 3 | 2.989 | 1 / 4 | 1 | 0 | 10 / 1 |

Both current processes reported zero borrow errors, connection errors, lost
connections and bad returns. These are sampled process-local observations,
not a sustained-load result or durable totals. Readiness contributes borrows.

The Grafana Job verified the provisioned Booking dashboard UID, eight panels,
the trafficxx-prometheus datasource and real stored samples for two targets.
Operations and the Browse dashboard/rules also passed, and unauthenticated
dashboard access was denied. This confirms API/data-source integration. Booking-specific alert
rules and external notification delivery remain unconfigured. The independent
Browse token-renewal run has no new final result in this checkpoint.

The user subsequently supplied browser screenshots showing all three dashboards
listed in the trafficXX folder, then all eight Booking panels rendering with
data. Visible values included two scrape targets, size1 against limit4 for each
process, zero sampled waiting/error/lost/bad-return rates, and current-process
totals of47borrows/two connection attempts. Readiness contributes borrows; these
are not47customer bookings or two concurrent sessions guaranteed for every
instant. Busy/opening slots were near zero in the displayed samples, not a
continuous proof that no query ran or no brief wait occurred. Samples occupy
the right edge of the one-hour window because this scrape job was newly
enabled; earlier Booking history was not collected by this job. This completes
the bounded Booking collection/datasource/browser milestone, not a load SLO,
Booking alert-rule test, external notification or token-renewal result.

## Measurements and limits

Booking now exposes /metrics from its own pool statistics, using the same
numeric-only renderer as Browse with a different trafficxx_booking_pool_ prefix.
It exports size, limit, available and waiting gauges, plus borrow, borrow-error,
connection-attempt, connection-error, lost-connection and bad-return counters.
Unknown fields are ignored; negative/non-integer values and unknown components
are rejected. No credentials, SQL, customer data or metric labels are emitted.
Reading statistics/metrics does not borrow a SQL connection or reset counters.
The route is excluded from OpenAPI and remains behind private services; exclude
/metrics and /internal paths from any future public Booking route.

A headless booking-metrics Service selects Ready Booking pods. Prometheus
discovers their individual IPs through DNS A records, port8000, job
trafficxx-booking-pool, discovery30s and scrape15s. This avoids mixing two
independent pools behind the load-balanced Booking Service. No Kubernetes API
token, new role, Terraform resource or public endpoint is required.

The separate dashboard UID is trafficxx-booking-pool, titled
trafficXX | Booking database pool. Eight panels show healthy targets,
busy/opening slots, slots versus limit, waiting borrowers, borrow/connection
rates, error rates, lost/unusable rates and current-process totals. Both the
Operations dashboard and ten-panel Browse dashboard are retained.

Size includes connections being prepared; size minus available is not active
SQL queries. Borrow counts include readiness. Connection attempts are neither
concurrent connections nor successful logins. Counters reset on process restart,
and short waits can occur between scrapes. These measurements establish no
latency/throughput SLO or whole-cluster connection ceiling. Booking-specific
alert rules and external notifications are not added in this stage; the existing
Browse alert rules remain installed.

References: [Prometheus DNS discovery](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#dns_sd_config),
[headless Services](https://kubernetes.io/docs/concepts/services-networking/service/#headless-services),
and [Psycopg pool statistics](https://www.psycopg.org/psycopg3/docs/advanced/pool.html#pool-stats).

## User-run Cloud Shell release

First pull and build Booking. This saves a new ignored booking-image.txt and
does not deploy anything:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-booking-image.sh
```

After reviewing IMAGE_BUILD_OK, deploy and check the application:

```bash
bash scripts/deploy-booking.sh &&
bash scripts/check-booking-http.sh --pool
```

Then apply monitoring configuration and verify both data paths:

```bash
bash scripts/deploy-booking-monitoring.sh &&
bash scripts/check-prometheus-http.sh --booking-pool &&
bash scripts/check-grafana-dashboard.sh --booking-pool
```

The account/context-checked helper applies only the new private metrics Service,
the collector ConfigMap (retaining Browse rules), and the three-dashboard
ConfigMap. It restarts the single Prometheus instance to validate/load the new
job, retaining its PVC/history with a brief collection gap. It does not apply
Grafana's base Deployment, restart Grafana, read/change Secrets, rotate passwords
or change the working HTTPS configuration. All dashboard deployment helpers now
retain the three files, including when updating Browse later.

BOOKING_PROMETHEUS_OK requires exactly two healthy targets matching the stable,
current Booking Deployment/ReplicaSet-owned pod IPs; all ten metrics for each;
fresh samples, at least two stored scrapes in two minutes; and valid pool bounds
with maximum4 and waiting0. It checks actual stored series rather than relying
on a successful HTTP scrape alone. The helper uses its own localhost-only
forward and stops only that process on exit.

The separate grafana-booking-pool-check Job uses the existing mounted Grafana
Secret only inside AKS, makes read-only API queries and prints no credentials.
GRAFANA_BOOKING_POOL_OK requires the provisioned eight-panel Booking dashboard,
correct datasource/job references, real Booking samples through the datasource,
and preserved Operations/Browse dashboards plus healthy inactive Browse rules.
Unauthenticated access must be denied. The Job has a360second deadline and
ten-minute completion TTL; inspect an existing Job or wait for automatic cleanup
before rerunning the same name. No credential file upload is needed.

After the API result, open the existing HTTPS Grafana endpoint and navigate to
Dashboards > trafficXX > trafficXX | Booking database pool
(/d/trafficxx-booking-pool). Browser rendering needs separate confirmation.
The existing booking flow can generate activity, but it creates/cancels a lab
reservation and retains its outbox/history; no new load experiment is included.

## Local verification

All393offline tests passed. Six new methods cover Booking-only metric names,
secret-field omission, component isolation, matching pod/series data, stale or
invalid samples, wrong datasource/query/dashboard, missing existing Browse
rules, and Booking deployment-name/owner selection. Existing Browse checks
retain their default component and continue to pass.

A smoke test using the pinned FastAPI/psycopg-pool packages verified Booking's
metrics response without an extra borrow (fake SQL). Native Prometheus3.15.1
validated the metrics exposition, config syntax and all13Booking panel queries in a temporary hidden
local server with no Azure targets, then the process was stopped. Bash syntax,
manifest structure and mocked config-only/account-guard deployment checks passed.
The disposable real-database HTTP test now includes the no-borrow assertion;
it was not executed locally because Docker remains stopped. Hosted CI remains
unverified. The live Prometheus/Grafana API and browser results
above were supplied separately by the user. No notification delivery or renewal
pass is claimed.
