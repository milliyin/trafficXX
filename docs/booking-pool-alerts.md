# Booking pool alert rules

Status: source, local validation and live AKS loading/evaluation passed.
This follows the verified Booking pool dashboard and the
[read-only load baseline](read-load-baseline.md).

## Verified live checkpoint

The user supplied a successful Prometheus rollout, BOOKING_ALERT_RULES_OK and
BROWSE_ALERT_RULES_OK. Both components had five installed rules, all healthy
and inactive, with two healthy targets and fresh stored samples verified.
Target/metric-absence rules had 120-second holds; waiting/borrow/connection
rules had 60-second holds. The checker validated expressions, labels and
evaluation freshness as well as rule completeness.

Inactive means no warning condition was present at that checkpoint; the rules
are enabled. The existing Browse rules remained healthy after the update.
Synthetic firing/recovery passed separately; this checkpoint did not inject
a real Booking outage or test external notification delivery. The Booking
dashboard at that checkpoint had eight panels; the ten-panel follow-up below
has since passed live API verification. No new token-renewal result was supplied.

## Conditions

| Rule | Condition | Hold before firing |
| --- | --- | --- |
| TrafficxxBookingTargetsUnavailable | Fewer than two healthy Booking scrape targets, including no target series | 2 minutes |
| TrafficxxBookingPoolMetricsMissing | Successful scrape without its pool-size metric | 2 minutes |
| TrafficxxBookingPoolWaiting | Waiting borrowers reported continuously at rule evaluations | 1 minute |
| TrafficxxBookingPoolBorrowErrors | Positive five-minute borrow-error increase remains visible | 1 minute |
| TrafficxxBookingPoolConnectionErrors | Positive five-minute connection-error increase remains visible | 1 minute |

Rules evaluate every 15 seconds, with severity warning and component booking-pool.
Inactive means the condition is absent; pending means the condition has not yet
lasted the hold time; firing means it has. A brief sampled wait in the load
baseline does not establish a sustained waiting condition.

The target rule includes the case where all target series disappear. Headless
DNS removes unready pods, so checking only individual up values would miss
vanished targets. Two is the intended replica count for this lab; revisit the
threshold before changing that count. Scrape health is not a direct SQL check.

Error rules inspect increases, so historical totals and normal counter resets
do not themselves trigger warnings. A single recent error can remain visible
in the five-minute window even after requests recover. Waiting samples can miss
short queues between scrapes. High pool occupancy alone does not fire a rule.

These rules evaluate in Prometheus. External email/phone/chat delivery is not
configured. A dashboard follow-up adds alert counts and history below; live
API verification passed and browser rendering remains unconfirmed. An independent
monitor is needed to detect Prometheus itself being unavailable.

## Deploy from browser Cloud Shell

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/deploy-booking-alerts.sh &&
bash scripts/check-prometheus-http.sh --booking-alerts &&
bash scripts/check-prometheus-http.sh --browse-alerts
```

No application image build is needed. The account/context guarded helper updates
the Prometheus ConfigMap with both rule files and restarts the single collector.
Its startup init container validates the mounted configuration. The metrics PVC
and history remain, with a short collection gap during restart. All other
Prometheus configuration deployment helpers also retain both rule files.
Serving APIs, Grafana, worker deployments and the original token-renewal Job
are untouched.

BOOKING_ALERT_RULES_OK requires exactly five installed Booking rules, matching
expressions/labels/hold times, fresh successful evaluation, inactive state and
two healthy targets with fresh stored Booking pool samples. It cannot pass
using Browse rules or samples. The second command separately verifies that
the existing Browse rules still evaluate against healthy Browse samples.
Do not treat deployment completion alone as live evaluation proof.

## Local validation

All 408 offline methods passed, including five Booking rule-evidence methods
covering component isolation, active/unhealthy/stale rules, altered holds,
missing/duplicate rules and insufficient stored samples. The native Prometheus
3.15.1 promtool passed 12 Booking scenarios and the existing 12 Browse scenarios:
healthy baseline, absent/missing/down targets, target recovery, missing metrics,
short/sustained waits, error increases aging out, historical totals and resets.
The full configuration and both five-rule files passed native syntax checks.

Bash syntax, mocked config-only deployment/account/context guards, workflow
actionlint and diff checks passed. CI now runs both scenario files; hosted CI
has not been checked. Docker stayed stopped. Synthetic firing/recovery does
not establish a real outage or notification delivery test. No local Azure
login, cluster operation or credential read was performed.

## Show pending and firing alerts in Grafana

The Booking dashboard now has ten panels in source. Panels 9 and 10 add current
pending/firing alert-instance counts and alert-state history, filtered to
component booking-pool and job trafficxx-booking-pool. Counts describe alert
instances, not distinct rules. History shows value 1 while an instance is
pending or firing. No history is normal when no alerts occurred in the selected
interval; zero counts alone cannot prove rules are loaded or evaluating.

Deploy this dashboard-only update from Cloud Shell:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/deploy-booking-dashboard.sh &&
bash scripts/check-grafana-dashboard.sh --booking-pool
```

The helper updates only the dashboard ConfigMap, preserving all three dashboard
files. Grafana refreshes the projected files/provider without a restart; allow
a few minutes. No image build or Prometheus restart is needed. The checker
requires all ten Booking panels, exact alert queries, five fresh healthy inactive
Booking rules, zero active counts and actual stored pool samples through the
datasource. Existing Operations/Browse dashboard and Browse rule checks remain
required, along with denied unauthenticated access. This is a healthy-baseline
check; active warning conditions deliberately prevent a pass.

If the existing grafana-booking-pool-check Job still exists, preserve its logs
and wait for its ten-minute completion TTL before rerunning the same name.
Do not recreate the unrelated token-renewal Job. After GRAFANA_BOOKING_POOL_OK,
refresh trafficXX | Booking database pool and scroll to the two bottom panels.
Browser rendering still needs separate confirmation.

All 410 offline methods passed, including missing-rule/zero-count rejection and
wrong/missing/duplicate panels, active counts and active-rule rejection. A
temporary hidden local Prometheus instance with no scrape targets produced a
real pending Booking unavailable-target alert: the new count queries returned
pending 1 and firing 0, and its history query found the pending series. All 16
Booking panel queries executed successfully, then the process stopped. This
was local query validation, not an AKS outage test. Bash syntax, mocked
dashboard-only/account/context guards and diff checks passed. Docker stayed
stopped; hosted CI remains unverified.

The user subsequently supplied a completed grafana-booking-pool-check Job and
GRAFANA_BOOKING_POOL_OK: ten panels, datasource trafficxx-prometheus, two targets
with at least two stored samples each, pending 0 and firing 0, and five healthy
inactive Booking rules. Operations and Browse dashboard/rule verification also
passed, and unauthenticated dashboard access was denied. This proves live
provisioning and datasource checks for the new panels; their browser rendering
still needs separate confirmation. External notifications remain unconfigured.
