# Browse pool alert rules

Status: implemented and validated locally; AKS rule loading and live evaluation
proof remain pending. This requires no application image build. The running
database token-renewal Job is independent and is not changed by these helpers.

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
The existing eight-panel Grafana dashboard continues to show metrics; this step
does not add a browser notification widget. An independent detector would be
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

## Tomorrow's renewal checkpoint

The user requested the reminder stay in existing memeory.md only. No timed
notification is scheduled. Keep the running renewal run and inspect it after
2026-10-24T16:01:40Z (October24at21:01:40 Pakistan time), as recorded in the
[renewal guide](database-token-renewal.md). Do not restart it for this alert step.
