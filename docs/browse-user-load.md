# First in-cluster user-load stage

Status: the live v3 50-user stage passed. The first two attempts stopped on
resource-metric freshness; their evidence is retained below.
This runs only the first 50-user stage; 100/250/500/1,000 users require separate
review and preparation after these results. No scaling or new Azure resource
provisioning is performed.

The existing apps node pool already has cluster autoscaling enabled (two to
four nodes in Terraform). This test does not alter that setting. The generator
is bound directly to an existing ready apps node rather than creating an
unscheduled capacity request, so this Job cannot trigger a new node scale-up.
If that node cannot admit it, the diagnostic fails instead of seeking new
capacity. It shares node resources with applications; it is not a dedicated
load-generator machine. Preflight also rejects an HPA targeting Browse.

## Workload

The revised generator first waits for a per-run file in a private 1 MiB memory
volume. The Cloud Shell parent releases it atomically only after both Browse
pods and the generator have at least two distinct fresh resource samples and
current CPU/memory below the start limits. Until then, no HTTP requests are sent.
The parent needs existing pods/exec permission to write the file in this owned
diagnostic pod; no RBAC or API token is added to the generator. The gate timeout
is 240 seconds. The workload clock starts after this gate, not during warmup.

One temporary Job runs 50 closed-loop virtual users for 60 seconds. Each user
alternates GET availability for demo-general and demo-seated through the internal
Browse Service. First requests are staggered across two seconds; subsequent
requests follow a two-second pause after each response. Each user makes at most
30 attempts, for a maximum of 1,500 workload GETs. Fifty virtual users are not
50 requests/s or necessarily 50 simultaneous HTTP requests. The report includes
achieved requests/s and sampled peak workload requests in flight separately.

Initial/final checks read both demo events directly through each Browse pod and
require 100 available tickets, including the complete 100 seated labels. A
separate sequential observer reads each serving pool's statistics approximately
once per second without borrowing SQL connections. There can be one extra
HTTP observation beyond the 50 workload requests. There are no reservation
requests, inventory writes or automatic retries. Pool borrows include unrelated
readiness traffic; they are not a per-pod count of user requests.

The generator has one CPU/256 MiB limits and uses the currently deployed immutable
Browse image with mounted stdlib scripts. It uses the existing unprivileged
read-load-check ServiceAccount, no Azure workload identity, API token, SQL
credentials, broker credentials or Kubernetes RBAC. The Cloud Shell parent
performs guarded Kubernetes observations using the user's existing session.

## Stop limits and resource observations

The probe stops offering new work on the first unexpected HTTP response,
incorrect inventory, transport error, observed request longer than two seconds,
pool error-counter increase, changed pool process or sampled waiting that
remains positive across observations for ten seconds. Already-running requests
may finish. These are conservative experiment stop limits, not a production SLO.
Pool sampling can miss short queues between observations.

The parent samples the Kubernetes metrics API about every five seconds for
both Browse pods and the generator. Browse is expected to retain 500m CPU and
256 MiB limits. Preflight requires fresh metrics for both serving pods below
80% CPU/85% memory. During the run, sampled CPU at or above 80% of the relevant
container limit across 20 seconds, or memory at or above 85% across ten seconds,
stops the generator. Missing/stale serving metrics stop the run too. A new
generator gets 210 seconds after it is first seen Running for metrics to appear.
Stale generator samples during startup are excluded while the HTTP gate stays
closed. Stale serving samples still fail immediately; stale generator samples
after the gate opens also fail immediately. The reviewed maximum source age is 90 seconds (60-second upstream collector
cadence plus 30 seconds for collection/publication delay); future timestamps
more than five seconds ahead still fail. This is a fixed reviewed budget, not
a measured guarantee of the Azure vendor binary's cadence.
At least two distinct fresh source timestamps are required for each pod before
a final pass; repeated reads of one timestamp are not new samples.

CPU is window-averaged and memory is the reported working set. These are sampled
maxima, not instantaneous peaks, CPU-throttling measurements or a bottleneck
diagnosis. Repeated high readings are a conservative stop observation rather
than proof of continuously high usage. If metrics access is unavailable,
preflight fails before any load Job is created.

To stop externally, the parent tests this diagnostic Job's UID and shortens
only its active deadline. Job/Pod logs are retained for inspection. No serving
Deployment is scaled or deleted. Lost create responses are resolved using the
unique script ConfigMap reference before a Job can be stopped; a concurrent
run's Job must not be modified. The Job itself has a 420-second active deadline,
zero retries, restartPolicy Never and a one-hour completion TTL. Its script
ConfigMap is owned by the Job for eventual cleanup. A preparation failure can
leave the named script ConfigMap for inspection.

Primary references: [resource metrics pipeline](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/)
and [Job deadlines](https://kubernetes.io/docs/concepts/workloads/controllers/job/#job-termination-and-cleanup).

## Browser Cloud Shell

Do not run other booking/load/scaling experiments concurrently. Keep Cloud Shell
open so resource supervision remains active; disconnecting can interrupt the
parent even though the Job's own HTTP/pool limits and deadline remain in place.

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/run-browse-users.py
```

No image build is needed. A read-only --preflight-only mode is available to
check account/context/pod/resource-metric readiness without creating a Job; it
does not establish inventory or workload success. The runner refuses an existing
browse-users-50-v3 Job and any active earlier browse-users-50/read-load-baseline
Job. Both terminal failed earlier Jobs are preserved. Concurrent
starts of this same stage use the same fixed Job name, so only one can create
it. Do not delete an existing diagnostic Job blindly or repeat a failed stage
to diagnose. Preserve its logs first and inspect desired/current conditions.

BROWSE_USERS_WORKLOAD_OK reports workload timings, counts, pool observations
and initial/final inventory checks. The final BROWSE_USERS_50_OK additionally
requires the same successful Job UID, one correctly owned Succeeded generator
pod with zero restarts, resource evidence for all three pods and unchanged
Browse/Booking serving pod/image/restart/generation snapshots. Failures report
their stage, safe error metadata, retained Job/files, resource samples and
whether a generator stop was requested. Stop request success is not independent
confirmation that termination has already completed; inspect that Job on failure.

Nearest-rank latency percentiles include internal Service DNS/network/client
connection/read/validation time. This is a closed-loop, short read-only test,
not public ingress, reservation writes, sustained capacity, autoscaling or a
production latency SLO. A 50-user pass does not imply 1,000-user readiness.
The existing database token-renewal Job remains untouched.

## Local validation

All 445 offline methods passed, including eleven new methods for paced users,
GET-only Service paths, response-body redaction, inventory/pool errors, sustained
waiting, resource quantities/age/container identity, CPU/memory holds, distinct
samples, UID-scoped stops, read-only preflight, resource-stop failures, lost
create responses/concurrent-run isolation, changed serving pods and incomplete
or overbudget results. A real loopback HTTP smoke ran all 50 users with shortened
duration (75 successful GETs), then stopped its server. This was not Azure load
or SQL performance evidence. Native kubectl rendered the Job locally using an
empty isolated kubeconfig; identity/security/image/ConfigMap/deadline/budget
settings passed inspection. Docker stayed stopped; hosted CI and live metrics
API access remain unverified.


## First live attempt and revision

Run 2ef4a23fc118 / Job browse-users-50 started on existing apps node
aks-apps-35966020-vmss000000 using the existing Browse digest. The user supplied
BROWSE_USERS_FAILED at observe_job_and_resources with POD_METRICS_NOT_FRESH,
no accepted resource summary and generator_stop_requested true. Subsequent
Job status confirmed Failed (79 seconds), so it was not left running. No
complete workload/latency result or 50-user capacity pass was established.

The later metrics check showed Browse sample ages 14.51 and 26.51 seconds,
both inside the unchanged 60-second limit. The original failure did not retain
the rejected pod/timestamp/age, so its exact source cannot be reconstructed
from those later samples. Do not claim Browse was overloaded or the generator
was definitely the rejected pod.

Code inspection found a startup gap: missing generator metrics got a grace
period but a stale first sample did not. The revision excludes those startup
samples while the resource gate is closed, logs safe warmup metadata, and
reports the rejected pod, source timestamp and age on freshness failure. It
does not relax the freshness threshold. CPU/memory maxima include this startup
observation window as well as the workload.

The revised Job is browse-users-50-v2; the failed original is not deleted.
Allow about three minutes with Cloud Shell open, including metrics warmup and
the unchanged 60-second load phase. All 447 offline methods passed, including
gate/run matching, no HTTP before release, stale-generator startup recovery
and exact freshness diagnostics. The shortened real HTTP smoke and native Job
render passed again; the local server stopped. That revision was subsequently attempted; see the evidence below.


## Second attempt and collector cadence correction

Run f42786c77b03 / Job browse-users-50-v2 failed after 119 seconds.
The HTTP resource gate remained closed: no workload traffic started.
The exact rejected serving sample was browse-6775788b9b-g649w at
2026-10-24T12:44:26Z, aged 62.375 seconds against the old 60-second limit.
One distinct sample per serving pod had been accepted, with sampled CPU around
0.0014 cores and memory around 59 MiB. This is not an overload diagnosis.

The supplied metrics-server deployment uses the Azure v0.8.0-25 image and has
no metric-resolution override. The [upstream v0.8.0 binary default is 60 seconds](https://github.com/kubernetes-sigs/metrics-server/blob/v0.8.0/cmd/metrics-server/app/options/options.go).
A maximum age equal to that cadence left no allowance for collection delay.
Version v3 therefore uses the fixed 90-second source-age budget above, while
still stopping on missing serving metrics or samples outside that budget.
It does not change the managed collector, application resources or traffic caps.

The new generator may need two collection points before its first CPU metric
and another point for a second distinct timestamp. Startup now allows 210
seconds for generator metrics, with a 240-second HTTP gate timeout, 420-second
Job deadline and 450-second parent deadline. After release, missing/stale
generator metrics fail immediately. Keep Cloud Shell open; allow up to seven
minutes including startup and the unchanged one-minute workload.

The revised Job is browse-users-50-v3; neither failed predecessor is deleted.
All 448 offline methods passed, including the exact 62.375-second regression,
the 90-second boundary and rejection of older/future samples. The real loopback
50-user/75-GET smoke and isolated native Job render passed. Live v3 success was subsequently supplied; see below. Resource summaries span startup and load; two distinct timestamps
can come from startup, and a 60-second collector can miss brief pressure during
a one-minute workload. These observations do not establish sustained capacity.


## Live 50-user result

The user supplied BROWSE_USERS_WORKLOAD_OK and final BROWSE_USERS_50_OK for
run a15b13bf9b4f / Job browse-users-50-v3. The gate required two distinct fresh
samples per pod before traffic started. Parent output reprints child logs at
completion, so the printed WAITING marker after GATE_RELEASED is not a second
wait or an inverted execution sequence.

All 50 users issued requests: 1,382 attempts, all HTTP 200, evenly split into
691 general and 691 seated reads. Elapsed workload time was 60.133 seconds,
achieved rate 22.982 requests/s, sampled peak workload HTTP concurrency eight.
Latency was p50 172.167 ms, p95 209.486 ms, p99 237.460 ms, maximum 327.461 ms.
No workload errors or observer stop checks were reported.

Both serving pools reached their configured maximum of four connections and
sampled waiting of one. Error counters stayed unchanged. Borrow deltas were
705 and 689, with three physical connection attempts per pod; these include
background readiness reads. Waiting did not trigger the sustained-wait stop.
This does not establish that waiting was absent between samples or that larger
loads have connection headroom.

Sampled resource maxima across startup and workload:

| Pod | Distinct source samples | CPU cores | Memory MiB |
| --- | ---: | ---: | ---: |
| browse-6775788b9b-g649w | 4 | 0.019858 | 59.992 |
| browse-6775788b9b-k6fdk | 4 | 0.016576 | 60.711 |
| browse-users-50-v3-gxdxj | 3 | 0.126665 | 124.820 |

Inventory remained 100 for each demo event. No inventory writes occurred;
serving pods and pool processes stayed unchanged. This is a short, paced,
in-cluster Browse read result, not a reservation-write, public-ingress,
sustained-capacity or production latency SLO result. Review a fresh Browse demo
and Operations health check before preparing a separate 100-user stage. No
higher stage has been started. The original token-renewal Job is untouched.


The subsequent user-supplied BROWSE_DEMO_HTTP_OK confirmed alive/ready and
100 available tickets for each of the two demo events. OPERATIONS_HTTP_OK
confirmed zero outbox pending, zero processing outstanding, zero processing
failures and no active alerts. Processed total remained 72; historical publish
failures remained four and the retained dead-letter audit count remained one.
Consumer/publisher heartbeats were present without errors, aged 4.213274 and
1.234180 seconds. Native broker queue counts were not measured by this check.
This completes the post-50-user application-health checkpoint. A separately
reviewed 100-user test is the next candidate; it has not been started.
