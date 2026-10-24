# Verified progress

Updated **24 October 2026** from user-supplied deployment and diagnostic results.
This page summarises milestones; linked guides retain the full evidence and
earlier checkpoints. It is not a live status page.

## Running and checked

| Area | Evidence |
| --- | --- |
| Azure foundation | AKS, ACR, private PostgreSQL, networking, and workload identities provisioned. |
| Browse and Booking | Two internal replicas each; health, permissions, booking flow, replay, and contention checks passed. |
| Expiry | A held seat expired through background work without an API call triggering expiry. |
| Messaging | Publisher and consumer delivered events; duplicate, backlog, and invalid-message drills verified behavior. |
| Observation | Operations API, Prometheus, authenticated Grafana HTTPS, pool dashboards, and native alert evaluation checked. |
| Controlled booking writes | Eighteen create/cancel pairs completed with cleanup verified and inventory restored. |
| Stored event consistency | Read-only audit verified 72 matching outbox/inbox records and 36 latest projections at that checkpoint. |
| 50-user Browse experiment | 1,382 successful reads in 60.133 seconds; zero errors; p95 209.486 ms. |
| 100-user Browse experiment | 2,734 successful reads in 60.134 seconds; zero errors; p95 313.140 ms with queue limit 16. |
| 100-user client memory comparison | 2,745 successful reads in 60.156 seconds; zero errors; p95 294.158 ms. Sampled generator peak process RSS 33.250 MiB versus the preceding 241.094 MiB. |

The fresh health checks after the 100-user pass reported both demo events still
at 100 available tickets, healthy workers, no backlog, and no active alerts.
Historical publication failures and a retained dead-letter audit are distinct
from an active incident or current native broker depth.

## How the 100-user result was reached

1. Early requests received HTTP 503 responses.
2. Preparing four database connections per pod removed startup connection growth.
3. Rejections remained, so separate counters were added.
4. Counters confirmed the bounded waiting queue was full.
5. A 16-slot waiting queue, with the same four connections, passed the one-minute test.

Read the [full experiment record](browse-user-load.md) for failed runs, sample
limits, node changes between trials, and why one pass does not isolate causality
or establish sustained production capacity.

## Next checkpoints

- **Client memory comparison completed:** the HTTP-only, per-thread client
  passed its live Linux comparison with substantially lower sampled process
  RSS. Fresh post-run demo and Operations checks also passed: availability
  100 per event, healthy workers, no backlog or active alerts. One comparison
  does not establish a causal memory profile or higher-load capacity.
- **Higher user counts:** 250, 500, and 1,000 users have not been tested. Each
  requires a separately reviewed workload and adequate generator headroom.
  The guarded 250-user command is now prepared in the
  [load guide](browse-user-load.md#prepared-250-user-stage); no live result yet.
- **Token renewal:** the original expiry-boundary Job has no final verdict in
  the supplied record. Its running/progress output is not a renewal pass.
- **Notifications:** warning evaluation is tested; external notification
  delivery is not configured or tested.
- **Production readiness:** longer load, broader failure domains, customer
  authentication, and other production requirements remain separate work.

## Useful evidence trails

[Browse load tests](browse-user-load.md) ·
[Booking writes](booking-write-baseline.md) ·
[Duplicate delivery](consumer-duplicate.md) ·
[Backlog recovery](consumer-backlog.md) ·
[Invalid messages](consumer-poison.md) ·
[Alert recovery](booking-alert-recovery.md)
