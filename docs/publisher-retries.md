# Publisher retry investigation

The user supplied publisher logs after the counter reached3. The same publisher
pod was Ready/Running with zero restarts. Logs contain three retry outcomes,
each followed by a published outcome:

| Retry UTC | Approximate gap since previous published log | Next published log |
|---|---|---|
|2026-10-22 22:08:21.268|42min31sec|2.38sec later|
|2026-10-22 22:35:11.814|18min2sec|4.02sec later|
|2026-10-23 10:10:08.707|11hr34min52sec|4.43sec later|

No publisher_reconnect line was present in the filtered output. These are
outcomes from one running pod, not per-message latency/SLO measurements: the
publisher log has no message ID and the failure may have started before the
retry outcome timestamp. Independent SQL/queue checks confirmed20matching
published/inbox events, ten matching projections and an empty queue afterward.
The historical failure counter counts failed publication attempts, not lost
bookings or three currently stuck messages.

Microsoft documents Service Bus detaching AMQP links after ten minutes of
inactivity and closing connections with no active links after five minutes.
The gaps and subsequent successful sends are consistent with an idle connection
being reopened, but do not prove it. Network disruption, timeouts or other SDK
exceptions cannot be excluded. The original adapter converted AzureError to
generic BROKER_SEND_FAILED without preserving its class, so the exact historical
cause cannot be recovered from these lines. Do not reset the counter or call
the hypothesis a confirmed fix.

See [Microsoft AMQP troubleshooting](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-amqp-troubleshoot).

## Safe diagnostic change

The broker adapter now logs broker_send_failed error_class=CLASS before the
existing generic retryable exception is raised. It logs no exception text,
traceback, credentials, endpoint, message ID or body. Retry policy, send timeout,
database/outbox transaction and sender lifecycle remain unchanged. A class can
distinguish broad failure types; it may still be insufficient to prove an idle
timeout versus another connection failure. Raw HTTP/AMQP logging is not enabled.

Three offline methods verify class-only redaction, unchanged successful send
count and propagation of non-Azure programming errors. All328offline methods
passed. Four mocked ACR build paths and Bash syntax passed. Docker stayed stopped;
database/AMQP integration tests and a new hosted CI result were not checked in
this step. No actual broker failure was generated for validation.

## Build in Cloud Shell

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/build-publisher-image.sh
```

The helper checks the lab subscription/registry, uploads the Docker build context
using the repository's secret exclusions, and builds the runtime target in ACR.
It saves a validated digest reference to ignored publisher-image.txt (also
excluded from Docker context). A build does not deploy or restart the publisher.
Share only IMAGE_BUILD_OK/tag/image metadata. Do not share credential files.

The user supplied successful ACR run ca3 after31seconds, tag
publisher-20261023102713-25933 and image:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:1b1e8fa39f10b31e59c2d340ea11865b71c04ef374eb55b0c4ea3ae42c64fe0a
```

The publisher manifest now pins that exact digest. The build reports Python
3.12-slim-trixie base digesta0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704;
this differs from the earlier runtime base. No new image scan/hosted CI result
is claimed by the ACR success output.

## Deploy the pinned publisher image

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/deploy-publisher.sh
```

This applies only the publisher Deployment. It retains one Recreate replica,
the same publisher identity, SQL/broker grants, non-root security settings and
readiness probe. Recreate briefly stops publication while its replacement starts;
outbox work can wait safely, and a sufficiently long startup may produce a stale
publisher warning. This is not a zero-downtime claim. No other workload is
restarted. The helper checks subscription/context and waits for rollout.

The user supplied PUBLISHER_DEPLOYED and healthy operations:20processed,
zero backlog/warnings, fresh consumer/publisher heartbeats2.909667/1.764152sec
and unchanged publication-failure history3.

A subsequent ordinary booking/cancel check passed for reservation
4ae564aa-3393-4f4d-a8a3-874bc0835b35: duplicate reuse, changed-request rejection,
cross-customer hiding, final CANCELLED status and reuse of that cancelled result.
OPERATIONS_HTTP_OK then reported22processed events, zero outbox/processing lag,
no active warnings or processing-failure records, consumer/publisher heartbeats
3.437415/0.306098sec and unchanged historical publish failures3/dead-letter audits1.
The consumer remained running. This verifies processing of two new events by
aggregate monitoring, not a new full-row SQL fingerprint check or an idle-timeout
root cause. No new send failure/error-class output was supplied.

## Next bounded observation after a quiet interval

Leave the lab without new bookings for at least12minutes, keeping publisher and
consumer running. This is longer than the documented link idle interval; it is
not a recommendation to pause either worker or send keepalive bookings. Then
run one ordinary booking/cancel flow and read its outcomes:

```bash
cd ~/trafficxx
bash scripts/check-booking-flow.sh &&
sleep 5 &&
bash scripts/check-operations-http.sh

kubectl logs -n trafficxx deployment/publisher --since=20m --timestamps \
  | grep -E 'broker_send_failed|publisher_reconnect|outbox_publish outcome='
```

Starting from22processed with no other activity, expect24after this check and
zero outstanding work. Report any new broker_send_failed error_class and whether
the failure counter increased; do not fabricate an error if none occurs. One
successful send after idle would show that failure is not inevitable, not prove
all idle cases safe. Even a connection-class error may not establish its exact
AMQP condition; distinguish observed evidence from the idle-timeout hypothesis.
The user first supplied successful flows at10:34:02 and10:39:30UTC, reaching
24then26processed events without increasing the failure count. Their quiet
gaps were only about2minutes and5minutes26seconds, so they did not test the
requested interval. A single countdown block was then used to wait12minutes
before generating another booking/cancel pair.

That run passed BOOKING_FLOW_OK for reservation
887eac3b-2373-458c-b1c4-89fa5407c539 and OPERATIONS_HTTP_OK:28processed,
no outbox/processing backlog, no alerts or processing-failure records,
healthy consumer/publisher heartbeat ages0.615515/1.015241sec, historical
dead-letter audits1 and publish_failures_total4. The relevant log sequence is:

| UTC | Observation |
|---|---|
|10:39:33.040|Last preceding published outcome|
|10:52:41.720|broker_send_failed error_class=ServiceBusConnectionError|
|10:52:42.109|outbox_publish outcome=retry|
|10:52:46.360 and10:52:47.251|Two published outcomes|

The connection failure followed about13min9sec without a published outcome.
The new instrumentation confirms a connection-class error and subsequent
recovery. It strengthens the idle-link explanation but does not identify the
exact AMQP detach condition or exclude a coincident network problem. The old
three failure causes still cannot be reconstructed. Full-row SQL consistency
was last checked at20events; the28event result is aggregate monitoring evidence.

## Prepared idle-connection refresh

The publisher now retires a sender after480seconds without a successful
publication. It checks a monotonic clock between publish_one transactions,
before selecting another row. Returning from the inner session closes sender
and client through their existing context managers; the outer loop opens fresh
ones. No keepalive bookings or broker messages are generated during idle time.
Successful sends reset the idle age, so continuously active senders are retained.
Connection refresh emits publisher_connection_refresh reason=idle with elapsed
seconds. Heartbeats continue during normal idle polling.

The eight-minute threshold leaves a margin before the documented ten-minute
link idle timeout. This is a targeted preventive change, not proof that all
connection errors are fixed. An explicit reconnect can still fail and is handled
by the existing sanitized outer error/retry loop. Periodic reconnects add a small
amount of connection/authentication work; a slow reconnect can affect heartbeat
freshness. Durable outbox backoff, stable message IDs, send timeout, consumer
deduplication and historical failure counters are unchanged.

Five offline methods test the exact retirement boundary before another row
selection, successful activity resetting age, retained retry/heartbeat/sleep
behavior, propagated database failure, and sender/client closure before reopening.
All333offline methods passed. The previously tested ACR helper can build this
source with the same command:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/build-publisher-image.sh
```

This creates another image, not a deployment. Pin the returned digest before
rollout. The running ca3 image has class logging but not this new refresh logic
until that rollout. Afterward, repeat the single12minute quiet-interval test and
read refresh/error/outcome logs without pausing the consumer. If there is no
unrelated work, one further pair from28events should reach30. A healthy result
with unchanged failure count4 and an idle-refresh log would support this
mitigation for the observed case, not establish a general availability SLO.
Build, rollout and post-fix quiet-interval results remain pending.
