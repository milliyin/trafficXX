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

Expected PUBLISHER_DEPLOYED with one Ready publisher pod. Actual new-image
rollout and future runtime error-class output remain pending. After rollout,
check monitoring health and publication/delivery separately; DB-only readiness
does not prove a successful broker send. Do not repeat the consumer outage to
diagnose publication. A future bounded booking/cancel check can generate two
normal events while keeping the consumer running.
