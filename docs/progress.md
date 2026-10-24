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
| 100-user single-query comparison | 2,804 successful reads in 60.100 seconds; zero errors; p95 201.078 ms; sampled generator peak process RSS 33.164 MiB. Fresh post-run health checks passed. |

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
- **250-user attempt:** stopped early after one confirmed queue-full rejection
  returned HTTP 503; only 60 users made requests before the stop. Fresh health
  checks after the subsequent instrumentation rollout passed. See the [diagnostic record](browse-user-load.md#first-250-user-attempt-stopped-on-queue-rejection).
  There is no 250-user pass or capacity verdict; 500 and 1,000 remain untested.
  Browse timing instrumentation is deployed and its 24-read sequential
  diagnostic passed. Repeated database round trips are a hypothesis to
  investigate; PostgreSQL CPU saturation or 250-user recovery is not established.
  The single-statement lookup is deployed; its 24-read diagnostic passed.
  Body means fell from 76-84 ms to 52-59 ms across the observed pods, consistent
  with one fewer round trip. Placement/sample differences limit comparison.
  The guarded 100-user single-query comparison also passed with 2,804 valid
  reads and zero rejections; fresh post-run health checks passed. A separate
  guarded 250-user single-query comparison then stopped early: 77 participants,
  75 HTTP 200 and two confirmed queue-full HTTP 503 responses on one pod.
  The diagnostic Job is confirmed terminal and fresh post-failure health checks
  passed. There is still no 250-user pass.
  The read-only capacity observation passed: AKS East US, PostgreSQL Central US;
  recent minute-level maxima CPU 7.89%, memory 20.99%, connections 20.
  Cached demo queries executed in 0.147-0.178 ms on the server, while simple
  client reads averaged 33.5-34.0 ms. This supports investigating round-trip
  overhead and bounded pool concurrency; it does not establish saturation
  during the subsecond failures. A controlled four-versus-eight connection
  comparison is [prepared with a recovery journal and restoration to four](browse-pool-size-comparison.md).
  Its live preflight and workload output are pending; no capacity change has run.
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
