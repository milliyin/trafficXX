# Browse pool alert rules

Status: AKS rule loading and live healthy-baseline evaluation passed in user-run
Cloud Shell. This required no application image build. The running
database token-renewal Job is independent and is not changed by these helpers.

## Verified live checkpoint

The user supplied a successful Prometheus rollout and BROWSE_ALERT_RULES_OK.
All five expected rules had health ok and state inactive, with holds120seconds
for target/metric absence and60seconds for waiting/borrow/connection failures.
Two Browse targets and fresh stored samples were verified. The checker also
validated the installed expressions, labels and evaluation freshness.

Inactive means the warning conditions were absent at that checkpoint, not that
the rules were disabled. Synthetic firing/recovery passed separately in the12
promtool scenarios; a real workload outage and external email/phone delivery
were not tested here. notification_delivery_configured remains false. No new
renewal result was supplied; its saved expiry-boundary check is still pending.

## Rules and interpretation

| Rule | Condition | Hold before firing |
| --- | --- | --- |
| TrafficxxBrowseTargetsUnavailable | Fewer than two healthy Browse scrape targets, including no target series | 2minutes |
| TrafficxxBrowsePoolMetricsMissing | Scrape succeeds but its pool-size metric is absent | 2minutes |
| TrafficxxBrowsePoolWaiting | A pool continuously reports waiting borrowers | 1minute |
| TrafficxxBrowsePoolBorrowErrors | A positive five-minute borrow-error increase remains visible | 1minute |
| TrafficxxBrowsePoolConnectionErrors | A positive five-minute connection-error increase remains visible | 1minute |

All have severity warning and component browse-pool; rules evaluate every15s.
Inactive means the condition is false. Pending means it is true but has not
lasted the full hold time. Firing means the hold has elapsed. A brief wait spike
can remain pending and then disappear. No keep-firing grace period is configured;
once the expression becomes false, the alert resolves at its next evaluation.

The target-count rule deliberately handles an empty vector with or vector(0).
DNS discovery removes unready endpoints, so up==0 alone would miss a vanished
target. The expected healthy count is two for this fixed-replica lab; update the
rule if the intended replica count changes. This is scrape readiness evidence,
not a direct SQL check or a node-resilience guarantee.

Error rules use increase over five minutes. Historical counter totals alone
and normal counter resets do not fire them. One recent error can still cause a
warning while that increase remains in the window; firing does not necessarily
mean every current request is failing. Waiting gauges are sampled and can miss
short events between scrapes. High utilization alone is not made an alert.

These rules evaluate inside the existing Prometheus instance. They do not send
email, phone or chat messages: Alertmanager/routing/delivery are not configured.
The original eight-panel Grafana dashboard showed pool metrics. A follow-up
adds two alert panels, described below; live deployment and API verification
passed. An independent detector would be
needed for Prometheus itself being unavailable.

Primary references: [Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/),
[rule unit tests](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/),
and [rules API](https://prometheus.io/docs/prometheus/latest/querying/api/#rules).

## Validation

The official Prometheus3.15.1 Windows release was downloaded into an ignored
test directory and its SHA256 verified against the maintainer's release sums.
Native promtool check rules found all five rules; config syntax validation passed.
Twelve real promtool scenarios passed: healthy baseline, all-series absence,
one missing/down target, target recovery, missing metrics, short/sustained waits,
borrow and connection failures aging out, historical totals and counter resets.
CI now runs those same scenarios using the matching official Prometheus image.
That version tag is not a digest pin; hosted CI results remain unchecked.

A temporary hidden local Prometheus server with no scrape targets validated the
actual rules API and expression canonicalization, then was stopped. Its native
RFC3339Nano timestamps exposed a Python3.10 parsing incompatibility; the checker
now safely truncates fractional seconds to microseconds and requires a timezone.
Five new offline cases cover installed-rule completeness, expressions, hold
times, state/health/freshness, target evidence and timestamp handling. All373
offline methods passed. Native actionlint validated the CI workflow; Bash syntax
and mocked config-only deployment checks passed. Docker stayed stopped and no
local Azure account, credential or live cluster access was used.

Synthetic firing/recovery is not a claim that a real production failure or
notification delivery was tested. The next step proves the cluster loaded and
evaluated the reviewed rules against actual stored Browse samples.

## User-run Cloud Shell deployment

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/deploy-browse-alerts.sh &&
bash scripts/check-prometheus-http.sh --browse-alerts
```

The account/context guarded deployment updates only the Prometheus ConfigMap
with its config and rule file, then restarts the single collector. Its existing
init container validates the full mounted configuration. Metrics PVC/history
are retained, with a short collection gap during restart. Other Prometheus
deployment helpers include the rule file too, so later updates retain it.
No Browse/Grafana deployment, HTTPS setting, credential or renewal Job changes.

The read-only checker uses an owned localhost-only forward and requires:

- Exactly five expected alert rules in the15second group, with reviewed holds,
  labels and semantically canonicalized expressions.
- Successful, fresh rule evaluations and no pending/firing alerts at the
  intended healthy baseline.
- Two healthy Browse targets, fresh stored samples and valid pool statistics.

Allow discovery and initial evaluations to settle. BROWSE_ALERT_RULES_OK records
loaded healthy rules and state; an active rule prevents that healthy result and
reports a safe check identifier. It is not proof of external notification
delivery. A failing check does not modify thresholds or disable warnings.

## Show pending and firing alerts in Grafana

The Browse pool dashboard now has ten panels, verified through the live API. Two additions show
current pending/firing alert-instance counts and a history of active instances,
labelled by rule, target and state. Pending means the warning has not yet lasted
its full hold time. Firing means that hold has elapsed. Multiple targets can
produce multiple alert instances for one rule.

These panels query Prometheus's native ALERTS series through the existing
datasource, as documented in the [Prometheus alerting guide](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/).
The history has no series when no alerts were active in the selected interval;
this is an expected healthy-baseline display. Counts use zero for absent series.
Zero counts or an empty history alone do not prove rule health: missing rules
would also produce no series. The read-only dashboard checker separately
requires all five reviewed rules, fresh successful inactive evaluations, two
healthy Browse targets, and both zero count queries through the datasource.
It rejects missing/unhealthy rules even if the panels return zero.

Deploy only the dashboard ConfigMap and run the strengthened check:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/deploy-browse-dashboard.sh &&
bash scripts/check-grafana-dashboard.sh --browse-pool
```

Allow a few minutes for projected-file/provider refresh. The checker expects
panel_count10, healthy_inactive_rule_count5, and alert_counts pending0/firing0.
If an earlier grafana-browse-pool-check Job still exists, let its automatic
ten-minute cleanup complete before creating another check with that name.
Refresh the existing Browse pool dashboard and scroll to the last row.

This update needs no image build, collector/application restart, credential
change, new Azure resource or disruption of the token-renewal Job. The
Operations dashboard remains in the same ConfigMap. External notification
delivery is still unconfigured. The user supplied GRAFANA_BROWSE_POOL_OK with
panel_count10, target_count2, pending0/firing0 and five healthy inactive rules.
At least two stored samples per target, the original Operations dashboard,
and denial of unauthenticated dashboard access were verified. This confirms
provisioning and datasource queries; browser rendering of the two new panels
and live firing/recovery remain separate evidence.

Local validation passed all 375 offline tests, Bash syntax checks, and a mocked
dashboard-only deployment/account guard. A temporary hidden native Prometheus
instance with no Azure targets confirmed the actual panel queries returned one
pending unavailable-target alert, zero firing alerts, and the labelled history
series. Its local rule-copy evaluation interval was shortened to one second to
complete startup restoration promptly; deployed rules/holds were unchanged.
The process was stopped after validation. This is query-engine evidence, not
live AKS/browser firing or notification-delivery proof.

## Tomorrow's renewal checkpoint

The user requested the reminder stay in existing memeory.md only. No timed
notification is scheduled. Keep the running renewal run and inspect it after
2026-10-24T16:01:40Z (October24at21:01:40 Pakistan time), as recorded in the
[renewal guide](database-token-renewal.md). Do not restart it for this alert step.
