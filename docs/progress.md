# Verified progress

Updated **27 October 2026** from user-supplied deployment and diagnostic results.
This page summarises milestones; linked guides retain the full evidence and
earlier checkpoints. It is not a live status page.

## Running and checked

Latest repeat (`40064bcca30f`) stopped after 114.629 seconds: 476 participants,
12,986 HTTP 200 responses and one `DATABASE_BUSY` HTTP 503. Four ready pods
were observed before termination. Counters subsequently confirmed approximately
one queue-full rejection, with no timeout or connection-error increases.
Recovery to two healthy pods passed. A read-only per-instance capture is
prepared to investigate pressure distribution and scrape visibility; no higher
stage or capacity increase is prepared.

The preceding earlier-scaling comparison completed 28,347
successful reads with all 500 participants and zero errors; p95 was 185.227 ms.
Separate recovery checks observed four pods naturally return to two, healthy
applications and unchanged inventory. The parent observer exited on a command
error, so the continuous resource audit and full drill remain incomplete.
See the [result and limits](browse-autoscaling.md#500-user-earlier-scaling-workload-and-recovery-passed-observer-audit-incomplete).

| Area | Evidence |
| --- | --- |
| Azure foundation | AKS, ACR, private PostgreSQL, networking, and workload identities provisioned. |
| Browse and Booking | Two internal replicas each; health, permissions, booking flow, replay, and contention checks passed. |
| Expiry | A held seat expired through background work without an API call triggering expiry. |
| Messaging | Publisher and consumer delivered events; duplicate, backlog, and invalid-message drills verified behavior. |
| Observation | Operations API, Prometheus, authenticated Grafana HTTPS, pool dashboards, and native alert evaluation checked. |
| Controlled booking writes | Eighteen create/cancel pairs completed with cleanup verified and inventory restored. |
| Stored event consistency | Read-only audit verified 72 matching outbox/inbox records and 36 latest projections at that checkpoint. |
| Database token renewal | Completed approximately 24 hours and 289 SQL checks; a fresh SQL login succeeded after the original token expired, using a changed token and the same SDK credential instance. Isolated process only. |
| 50-user Browse experiment | 1,382 successful reads in 60.133 seconds; zero errors; p95 209.486 ms. |
| 100-user Browse experiment | 2,734 successful reads in 60.134 seconds; zero errors; p95 313.140 ms with queue limit 16. |
| 100-user client memory comparison | 2,745 successful reads in 60.156 seconds; zero errors; p95 294.158 ms. Sampled generator peak process RSS 33.250 MiB versus the preceding 241.094 MiB. |
| 100-user single-query comparison | 2,804 successful reads in 60.100 seconds; zero errors; p95 201.078 ms; sampled generator peak process RSS 33.164 MiB. Fresh post-run health checks passed. |
| 250-user temporary pool-eight trial | 6,986 successful reads in 60.144 seconds; zero errors; p95 191.658 ms; both pools restored to four and post-trial health verified. |
| 250-user autoscaling verification | v3 completed 21,129 successful reads in 182.14 seconds, zero errors, p95 159.051 ms, automatic two-to-three-to-two ready pods; sampled resource and final health checks passed. One HPA metric gap recovered in 16.78 seconds. |
| 500-user earlier-scaling workload | All 500 users completed 28,347 successful reads in 182.187 seconds, zero errors, p95 185.227 ms. Separate natural four-to-two recovery and health passed; continuous resource audit incomplete after an observer command error. |

The fresh health checks after the restored 250-user trial reported both demo events still
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

- **Complete the 500-user observer audit:** the earlier-scaling workload
  passed, but its parent stopped with `CalledProcessError` after observing
  three ready pods. Safe command/stage diagnostics and preservation of the
  primary error during cleanup are implemented. The distinct same-profile v2
  repeat failed on one HTTP 503 after reaching four ready pods, rather than
  an external command error. Aggregate diagnostics confirmed queue-full rejection
  and natural recovery passed. Capture `diagnose-500-distribution` before
  choosing a further change; retain the failed Job.
  The historical failing command remains unknown. Complete the observer run
  before increasing load.
  Separate four-to-two recovery passed; it does not replace the interrupted audit.
- **Earlier 500-user failures retained:** the first ramped run stopped after
  40.795 seconds, with 338 users participating, 3,359 successful requests and
  one HTTP 503. Two ready pods were still observed. Fresh follow-up checks
  showed healthy applications and no backlog or active alerts; the Job is
  retained. Extended historical diagnostics show roughly one queue-full
  rejection and failed borrow, with post-terminal samples from both pods and
  no connection-error increases. The later 120-second ramp also stopped on one
  queue-full rejection before the third pod became ready. Lowering the pressure
  target from five to three preceded the successful workload recorded above.
  No complete 500-user observer audit or 1,000-user stage is established.
  See [the record and comparison](browse-autoscaling.md#prepared-comparison-slower-arrival-unchanged-capacity).

- **Complete autoscaling observer run passed:** v3 run `f807afdc2940`
  reported `BROWSE_AUTOSCALING_DRILL_OK`. All 250 users completed 21,129
  successful reads, with p95 159.051 ms and no request errors. The observer
  verified scale-up to three and natural return to two ready pods, sampled
  resource limits, healthy inventory and Operations, and database observations.
  One HPA metric gap recovered in 16.78 seconds. This is a bounded read test;
  latency SLOs, continuous peaks, downscale HTTP continuity and higher user
  counts remain separate work.

- **Three-minute pool-eight stability trial passed:** all 250 users completed
  21,133 successful reads, zero errors, p95 167.385 ms; automatic restoration
  to four connections and health checks passed. The [new autoscaling profile](browse-autoscaling.md)
  selects eight connections per pod, two-four pods and a 128-connection lab
  planning budget. The addon and profile are deployed: the first drill reached
  four ready pods in the first drill, with native HPA resize events verified.
  A separate v2 workload completed: 250 users, 21,086 successful reads, zero
  errors, p95 167.385 ms, and observed scale-up from two to three ready pods.
  Its observer stopped on missing resource metrics near Job completion; a
  completion-order race is fixed. The separate read-only recovery check then
  observed natural scale-down from three to two ready pods, active metrics,
  healthy inventory and Operations. The original continuous resource audit
  was incomplete; workload, scale-up and post-run scale-down are verified.
  The later v3 run above completed the corrected observer cycle.

- **Client memory comparison completed:** the HTTP-only, per-thread client
  passed its live Linux comparison with substantially lower sampled process
  RSS. Fresh post-run demo and Operations checks also passed: availability
  100 per event, healthy workers, no backlog or active alerts. One comparison
  does not establish a causal memory profile or higher-load capacity.
- **250-user trial completed:** earlier four-connection attempts stopped on
  queue-full rejections. The [temporary eight-connection trial](browse-pool-size-comparison.md#live-trial-passed-and-baseline-restored)
  completed with all 250 users, 6,986 successful reads and no new pool errors.
  It restored four connections per pod, verified health and retained its
  recovery journal. Four was the baseline at that checkpoint; the pass applies
  to the tested eight-connection configuration. A [separate three-minute trial](browse-pool-size-comparison.md#prepared-three-minute-stability-mode)
  also passed, with its baseline restoration verified. Eight connections are
  selected and deployed for the new two-to-four-pod autoscaling profile.
  A later 500-user workload and separate recovery passed as recorded above;
  the full observer audit remains incomplete. No 1,000-user test ran.
- **Capacity interpretation:** AKS East US and PostgreSQL Central US, cheap
  cached SQL and much longer client calls support investigating round-trip
  cost with bounded pool concurrency. The passing trial's three minute-level
  database samples had maxima CPU 5.51%, memory 21.20% and connections 25.
  Sampling, warmup and placement differences prevent a precise saturation,
  isolated causal, sustained capacity or production SLO verdict.
- **Token renewal completed:** the original Job reported
  `DATABASE_TOKEN_RENEWAL_OK` and Complete 1/1. Its final SQL login occurred
  about 30 seconds after original expiry. Serving-pod continuity and other
  workload identities remain separate checks.
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
