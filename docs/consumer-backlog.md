# Consumer outage and backlog recovery on Azure

This controlled lab pauses only the consumer. Booking, publisher and expiry
remain running. One real demo hold is created and cancelled through the existing
HTTP flow test. Its HELD and CANCELLED events should accumulate in the queue
while no consumer pod exists. Then the consumer is restored, and native queue
counts plus the read-only inbox/projection observer must agree that work drained.
This experiment is prepared; real Azure output is pending.

Run from browser Cloud Shell, keeping other booking experiments idle:

```bash
cd ~/trafficxx
git pull --ff-only origin main
python3 scripts/check-consumer-backlog.py
```

The runner checks subscription/context, one Ready consumer with the reviewed
selector, an empty queue, publisher/booking rollout readiness and the installed
consumer-drain observer ConfigMap before pausing. A precondition failure stops
before creating work. It records the consumer Deployment UID and uses conditional
scales (one replica to zero, then zero to one), waiting for all consumer pods to
terminate before invoking the booking flow.

Expected intermediate output CONSUMER_BACKLOG_OBSERVED reports active=2,
dead_letters=0, total=2 with zero consumer replicas/pods and the diagnostic
reservation CANCELLED. The runner then restores one consumer replica, checks its
rollout, waits for native active/dead_letters/total counts to become zero, and
runs a uniquely named read-only consumer-drain Job using the existing image and
ConfigMap. That observer requires full inbox/payload/projection agreement and a
fresh error-free heartbeat. It is deleted after use; no observer SQL is written.

Expected final output CONSUMER_BACKLOG_RECOVERY_OK includes before/paused/after
queue counts, restored_replicas=1 and stored_state.result=CONSUMER_DRAIN_OK. From
the prior checkpoint, expect 14 outbox/inbox rows and seven projections after
this one hold/cancel flow, although the observer validates actual stored state
rather than hard-coding historical totals. Real final results are pending.

Restoration runs in a finally block, including booking failures, unexpected
queue counts, timeout and KeyboardInterrupt. Deployment replacement or an
unexpected replica count is rejected rather than overwriting another change.
Each CLI call and queue/pod wait is bounded. Terminal loss or a killed Cloud Shell
process can prevent cleanup; if interrupted, inspect and restore the lab:

```bash
kubectl get deployment/consumer -n trafficxx
kubectl scale deployment/consumer -n trafficxx --replicas=1
kubectl rollout status deployment/consumer -n trafficxx --timeout=180s
```

A failed booking request may have an ambiguous outcome; the reused flow helper
attempts cancellation for a known ID and otherwise reports its lab recovery
identifiers. Remaining holds expire after five minutes. Inspect before rerunning:
a rerun creates a new diagnostic reservation and two more events. Booking history,
outbox and inbox records remain intentionally; this is not a data-reset command.

Eight new offline methods cover successful backlog/recovery/SQL observation,
precondition failure without mutation, failed booking restoration, interruption,
ambiguous scale outcome, bounded queue timeout, SQL-observer failure/cleanup and
Deployment replacement. All 217 offline methods pass; mocked CLI tests do not
replace the real Cloud Shell experiment. No roles, images or runtime code change.

This tests a controlled stop/start and durable buffering. It does not kill a
consumer between SQL commit and broker acknowledgement, establish a processing
latency SLO, or prove exactly-once delivery. Poison-message/dead-letter recovery
is a separate future experiment.
