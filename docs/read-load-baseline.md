# Bounded read-only latency baseline

Status: local validation and a live two-phase baseline passed, as recorded
below. This follows the verified Browse/Booking pool and
dashboard milestones. The independent Browse token-renewal Job stays running
on its original image/run.

## Verified Azure baseline

The user supplied READ_LOAD_WORKLOAD_OK and the final READ_LOAD_BASELINE_OK for
run efd5650cc272 / Job read-load-baseline. Both phases ran for 30 seconds, using
Browse image f09d49ce8044461be8dbde85835888fec67a5ca5a2d848b20ed422da211c1e51
and Booking/runtime image c94884448905a0e94f2e4b8502ed120767f8cf46a2aa2f8747862ad5f6a663f0.
The runner confirmed unchanged serving pods; the probe confirmed unchanged pool
processes and demo availability of 100 before/after, with no inventory writes.

| API/profile | Workers per API | Requests | Achieved requests/s | p50 ms | p95 ms | p99 ms | Maximum ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Browse availability | 1 | 60 | 2 | 174.619 | 206.066 | 246.909 | 246.909 |
| Booking missing lookup | 1 | 60 | 2 | 140.114 | 157.462 | 173.429 | 173.429 |
| Browse availability | 4 | 240 | 8 | 170.707 | 203.162 | 235.639 | 370.345 |
| Booking missing lookup | 4 | 240 | 8 | 141.844 | 165.072 | 215.352 | 354.242 |

All 600 workload responses were valid: 300 Browse HTTP 200 responses and 300 expected
Booking HTTP 404 RESERVATION_NOT_FOUND responses. Browse general/seated reads
were evenly split within each phase. There were no unexpected workload errors,
and all four pods' pool error counters remained unchanged.

At the lower rate each pod served 30 test reads with 33 pool borrows (including
readiness). New connection-attempt deltas were 1/0 for Browse and 0/1 for Booking;
sampled peak sizes were 2/1 and 1/2 respectively, with zero sampled waiting.
At the higher rate each pod served 120 test reads with 123 pool borrows. Browse
attempt deltas were 0/1 with peak sizes 2/2; Booking attempts were 2/0 with peak
sizes 3/2. The configured maximum was 4 throughout. One Browse pod
(browse-6775788b9b-k6fdk) and one Booking pod (booking-67b7966d6c-npkcg) each
reported a sampled peak of one waiting borrower. Other pods' waiting peaks were 0.
This can happen while connections are occupied or being prepared; the samples
do not identify an exact cause or establish queue duration.

For this run, four times the per-API request rate did not materially change
Browse p95; Booking lookup p95 increased about 7.6 ms. This is an observation from
one short controlled run, not statistical significance, a capacity limit or a
production SLO. Higher-rate maxima exceeded 300 ms even though both p95 values
were below it. Booking writes, public/Service/HTTPS paths, CPU-throttling causes,
long-duration stability and token renewal were not tested by this workload.

## Workload and boundaries

One temporary Job runs inside AKS and sends GET requests directly to the four
current API pod IPs. This removes the browser/Cloud Shell port-forward path from
the measurement and makes per-pod pool observations possible. It does not test
public ingress, Service load balancing, HTTPS or an end user's network.

| Phase | Duration | Workers per API | Workload concurrency total | Rate ceiling per API |
| --- | --- | --- | --- | --- |
| Baseline | 30 seconds | 1 | 2 | 2 requests/second |
| Increased | 30 seconds | 4 | 8 | 8 requests/second |

The generator spaces each worker's request starts by at least 0.5 seconds, with
no retries or catch-up burst if requests are slow. At most 600 workload attempts
are made across the two phases. There are also 12 initial/final verification GETs
and pool-statistics observations. A separate single observer makes sequential
statistics requests, so there can be one extra HTTP request beyond the eight
workload requests. Statistics reads do not borrow SQL connections.

Browse alternates general/seated availability reads across both replicas. It
requires both demo events to have 100 available units before and after the test,
including all 100 seat labels for demo-seated. Do not run booking experiments or
other inventory writers during this baseline. Unexpected inventory changes stop
the run; it never cancels or resets another customer's data.

Booking looks up a new random reservation UUID with a new simulated customer
header. The expected response is 404 RESERVATION_NOT_FOUND, which counts as a
valid response rather than an application error. This exercises the pooled
database lookup/rollback path. No actual customer's reservation is read, no
hold is created/confirmed/cancelled, and no outbox message is emitted by the
workload. Booking read latency does not establish reservation-write latency,
inventory-lock performance or idempotency behaviour under load.

The HTTP client uses fresh connections, explicit socket timeouts and bounded
response bodies. It disables proxies and refuses redirects. Timings include
client connection setup, body reading, JSON parsing and response validation.
They are not server-only handler times. Each API reports nearest-rank p50/p95/
p99/max, attempts, valid responses, status/route counts and achieved requests
per second. The offered load is closed-loop: slow responses reduce actual
traffic and unsent arrivals are not measured. This is not an open-loop capacity
test or statistical proof of a production tail-latency SLO.
There is no equivalent unpooled measurement here, so this cannot establish a
before/after performance improvement from pooling.

Pool observations record per-pod borrow/connection-attempt deltas, sampled size
and waiting peaks, and unchanged error counters. Each phase checks the same
process IDs and pool bounds; sampling may miss short waits. Any unexpected
HTTP result, process change, invalid pool sample or new pool error stops new
work. Already in-flight reads may finish. Failure output retains safe phase
counts/statuses; raw response bodies and command errors are not printed.

## Job and identity

The runner uses the currently deployed, digest-pinned Booking runtime image
with a mounted standalone standard-library Python script. No image build or
package install is needed. The command overrides the application entrypoint;
the probe does not import the application or create an Azure/SQL credential.

The separate read-load-check service account has no Azure identity annotation,
federation or added RoleBinding. Both account and Pod disable API-token mounting;
the Pod has no workload-identity label, application Secret, SQL settings or
broker credentials. It runs UID/GID 10001, read-only root, dropped capabilities
and no privilege escalation on an apps node. Requests are 100m CPU/128Mi; limits
are one CPU/256Mi. Generator limits and HTTP-client overhead can affect results;
CPU throttling and network saturation are not separately measured here.

The Job has no retries, a 240-second active deadline and five-second termination
grace. Completed/failed Job evidence is retained for one hour, then Kubernetes
TTL cleanup removes it and its owned source/target ConfigMap. The unprivileged
service account remains for reuse. No serving deployment, replica count,
monitoring configuration, Terraform setting or credential is changed.

## User-run Cloud Shell command

Have the existing Browse/Booking Grafana dashboards open. Pull the source and
run from the restored clone with the lab subscription/AKS context selected:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/run-read-load.py
```

Keep this Cloud Shell command running for roughly two minutes. The helper
checks the selected account/context, requires exactly two stable current pods
per API with correct Deployment/ReplicaSet ownership, and records their images,
UIDs, restart counts and generation. It refuses an existing read-load-baseline
Job before changing source files. A fixed Job name also prevents two concurrent
creations from starting separate loads.

It prints READ_LOAD_STARTED with the run/image and limits, creates the
HTTP-only Job, waits for completion, retrieves its safe logs, then rechecks all
four serving pods. READ_LOAD_WORKLOAD_OK contains both phases' measurements.
Only the final READ_LOAD_BASELINE_OK confirms matching Job identity, one
successful generator pod with zero restarts and unchanged serving pod records.
Neither result means a latency target has passed; latency_slo_tested is false.
Share the measurements as well as the final marker.

If the helper fails or Cloud Shell disconnects, inspect the retained Job rather
than starting another run:

```bash
kubectl get job read-load-baseline -n trafficxx
kubectl logs -n trafficxx job/read-load-baseline -c check
```

A disconnected Cloud Shell can leave this bounded Job running until completion
or its deadline. If the wrapper's final pod-stability check did not execute,
do not claim READ_LOAD_BASELINE_OK from a workload-only result. Wait for TTL
cleanup or deliberately remove only the inspected finished diagnostic Job before
rerunning; preserve its logs first. No automatic load retry, scaling or serving
pod deletion occurs. If preparation fails before the ConfigMap receives a Job
owner, the failure JSON includes its exact name for later inspection/cleanup.

## Local validation

All 403 offline tests passed, including ten new methods for rate/request bounds,
expected 404 handling, explicit percentile definitions, target-network isolation,
inventory/pool-error stop conditions, response-body redaction, GET/proxy/redirect
behaviour, existing-Job refusal, completion identity, serving-restart rejection
and failure-log retention. Tests use shortened local durations and fake HTTP;
they establish no Azure latency measurement.

A real local standard-library HTTP server/client smoke confirmed expected 404
handling and redirect refusal, then stopped. Native kubectl rendered the Job
locally using an empty isolated kubeconfig; image/ConfigMap substitutions and
the no-identity/security/deadline settings passed inspection. Python compilation,
manifest parsing and diff checks passed. Docker stayed stopped. Actual
in-cluster traffic, pool observations and latency results were supplied
separately by the user above. Hosted CI remains unchecked.
