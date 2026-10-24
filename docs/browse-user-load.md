# First in-cluster user-load stage

Status: the live 50-user stage and the subsequent 100-user queue-16 comparison
passed. Earlier failures and diagnostic revisions are retained below. Higher
stages remain pending; generator memory needs review before progression.
The default remains the first 50-user stage. After its live pass and healthy
post-checks, an explicit --users 100 stage is prepared below; 250/500/1,000 users
require separate review and preparation. No scaling or new Azure resource
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


## Prepared 100-user stage

The user authorized preparation after the successful 50-user run and fresh
healthy Browse/Operations checks. Run the same runner with explicit --users 100.
It creates the separate fixed Job browse-users-100 and puts the selected count
in the mounted configuration. Omitting the flag still selects 50; counts other
than 50 or 100 are rejected. There is no automatic progression.

The workload is unchanged except for 100 users and a maximum of 3,000 GETs
(30 per user). It still lasts 60 seconds, staggers initial requests over two
seconds and pauses two seconds after each response. At most 100 workload HTTP
requests plus one observer can be in flight. That is not a 100 requests/s target.
The generator retains one CPU/256 MiB on an existing apps node. Both serving
pools retain four connections and all HTTP/pool/resource stop limits remain.
Both pools reached four slots with sampled waiting of one at 50 users: watch
latency and waiting closely, without assuming spare connection capacity.

All active browse-users-* and read-load-baseline Jobs block startup. Completed
50-user Jobs are permitted and remain intact. Failure stops only the selected
Job using its UID; the gate also targets only that selected Job's owned pod.
The new final success marker is BROWSE_USERS_100_OK; a workload marker alone
still does not establish full Job/resource/stability success. On failure retain
the output and inspect that Job before any retry. All earlier sampled-metrics
limitations, no write-load/SLO claim and no notification tests still apply.

For a fresh browser Cloud Shell session, select the existing subscription,
refresh the existing cluster credentials and convert for the Cloud Shell Azure
login. Clone only if the session has no repository. These commands run in the
user's Cloud Shell; no local Azure connection is needed.

```bash
az account set --subscription 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 &&
az aks get-credentials --resource-group trafficxx-lab-rg --name trafficxx-lab-aks --overwrite-existing &&
kubelogin convert-kubeconfig -l azurecli
```

After that succeeds:

```bash
if [ -d ~/trafficxx/.git ]; then
  git -C ~/trafficxx pull --ff-only origin main
else
  git clone https://github.com/milliyin/trafficXX.git ~/trafficxx
fi
```

After the clone/pull succeeds:

```bash
cd ~/trafficxx &&
bash scripts/check-browse-http.sh --demo &&
bash scripts/check-operations-http.sh &&
python3 scripts/run-browse-users.py --users 100
```

Keep Cloud Shell open for up to seven minutes. No image build is needed. Share
the complete result before considering a higher stage. The original renewal
Job is not changed, and its final verdict still requires separate evidence.

Local validation: all 450 offline methods passed, including real selected-stage
result validation, all 100 users participating, the 3,000 attempt ceiling,
unsupported/mismatched-stage rejection, mounted count propagation, separate
Job render/gate/UID-scoped stop and blocking an active earlier 50-user Job.
Shortened real loopback HTTP tests exercised 50 users (75 valid reads) and
100 users (149 valid reads), then stopped their server. Native kubectl with an
empty isolated kubeconfig rendered both Job names with identical resource and
security settings. This is local correctness evidence. The first live 100-user attempt is
recorded below. Docker stayed stopped; no local Azure commands were run.


## First 100-user attempt: stopped on HTTP 503

Run 5f3e53736ffa / Job browse-users-100 passed startup resource gating after
healthy demo/Operations checks (inventory 100 each, processed 72, no backlog
or active alerts). It then stopped 0.974 seconds into the workload: only 23
users had issued requests, with 23 attempts, 22 HTTP 200s and one HTTP 503.
Sampled peak workload concurrency was 17. No complete 100-user result or
capacity limit was established. The failed Job/files are retained:
browse-users-5f3e53736ffa-files. Do not delete or rerun before diagnosis.

The old shared response validator combined HTTP status and inventory validation
under DEMO_INVENTORY_CHANGED. Thus the supplied label does not demonstrate an
inventory change: the unexpected 503 alone produced it. The validator now
classifies unexpected status first as UNEXPECTED_HTTP_STATUS_503, without
printing response bodies. Successful HTTP 200 payloads still undergo the same
inventory checks. This diagnostic correction needs no serving-image change and
does not change stop limits or authorize a new load run.

The 503 body was not retained, so the exact cause is unknown. Application code
can return 503 for pool queue/timeout/closed conditions (DATABASE_BUSY), while
other availability paths also return 503. Pool pressure is a hypothesis, not
proved by the supplied status. No observer stop was recorded before the short
run ended. Reported resource maxima mostly cover startup; with coarse collection
they cannot characterize this subsecond failure or exclude a transient spike.

The parent reported DIAGNOSTIC_JOB_FAILED and requested a UID-scoped generator
stop. Confirm native Job/pod termination and fresh Browse/Operations health;
inspect both Browse replicas' recent logs and pool counters before choosing any
change. Do not increase pool size, scale replicas or proceed to 250 users from
this result. The 100-user stage has not passed.


### Post-failure recovery and replica evidence

The user supplied native Job status Failed (3m55s), confirming the diagnostic
Job is terminal. A fresh BROWSE_DEMO_HTTP_OK reported alive/ready and both demo
availabilities still 100. OPERATIONS_HTTP_OK reported zero pending/outstanding
work, no active alerts or processing failures, processed total 72 and unchanged
historical publication failures four/dead-letter audit count one. Both worker
heartbeats were present without errors (consumer 5.910125s, publisher 0.727597s).
These checks do not independently measure native broker counts.

The supplied access logs identify browse-6775788b9b-k6fdk returning the 503 for
GET /events/demo-general/availability from diagnostic client 10.244.3.222.
The same pod subsequently returned successful readiness/liveness/metrics
responses; the other serving replica's supplied availability requests were
successful. The access log does not include the error body or pool exception.
No cause is proved by that line alone. Read both existing replicas' numeric
/internal/database-pool counters next; this endpoint does not borrow SQL
connections. Lifetime counters can corroborate pool pressure but cannot by
themselves isolate a single request or establish the precise exception.


### Pool counters after recovery

The user read both existing replicas' /internal/database-pool endpoints:

| Counter | g649w | k6fdk (503 replica) |
| --- | ---: | ---: |
| Pool size / available | 2 / 2 | 2 / 2 |
| Requests waiting | 0 | 0 |
| Requests total | 9092 | 9046 |
| Request errors | 0 | 1 |
| Connection attempts | 58 | 57 |
| Connection errors / lost / bad returns | 0 / 0 / 0 | 0 / 0 / 0 |

Both retained their configured minimum one and maximum four. Instance IDs were
931a3f5c25ea49198ba0c3d41a1715b6 (g649w) and
18100244dbdc4446a0b841942360ec86 (k6fdk).
The sole cumulative request error is on the same replica as the observed 503.
This supports pool acquisition pressure as the likely explanation; it does
not independently distinguish a full waiting queue from acquisition timeout or
identify the original exception. Zero connection-error/lost/bad-return counters
show no recorded failures of those types in these processes. Current idle
capacity and no waiters confirm pool recovery at observation time, not unused
capacity during the initial burst. No pool size or wait-limit tuning has been
applied. Before a controlled retry, preserve the allowlisted service error code
and immediate failure-time pool counters so the next diagnosis is specific.


## Explicit diagnostic retry after the first 100-user failure

The user authorized one separately prepared diagnostic retry. It uses the same
100-user/60-second/two-second-think/3,000-GET limits and unchanged pool/resource
budgets. No pool prewarming, tuning, replica scaling or image rebuild is added.
Select it explicitly with --users 100 --diagnostic-retry. This creates the fixed
Job browse-users-100-diagnostic, leaving browse-users-100 and its logs intact.
An existing diagnostic Job blocks a repeat; any active earlier load also blocks
startup. The flag is invalid for the 50-user stage. It does not retry any HTTP
request or automatically repeat a failed workload.

On workload failure, BROWSE_USERS_PHASE_FAILED now includes the run ID, the
first completed failing request's status/route/latency/phase elapsed time and
an allowlisted service code. Only DATABASE_BUSY and DATABASE_UNAVAILABLE are
retained from error payloads; unrecognized values become UNRECOGNIZED. Response
bodies and headers are not printed. DATABASE_BUSY confirms the application pool
rejection path but still does not distinguish timeout, full queue or closed
pool, which share that application code.

The failure report also includes pool_snapshots_after_workload_stopped for both
replicas: validated numeric counters and process IDs before the workload and
after its in-flight requests and observer finish. Snapshots are sequential
read-only GETs to the existing private statistics endpoint; they borrow no SQL
connections. Counter deltas are emitted only for unchanged process IDs with
nondecreasing counters. Changed processes or reset counters make deltas null;
an unavailable snapshot records only its exception type and does not mask the
original failure. These are post-stop observations, not simultaneous peak or
per-request attribution. They may show recovery by the time they are read.

Run in the current Cloud Shell session:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/check-browse-http.sh --demo &&
bash scripts/check-operations-http.sh &&
python3 scripts/run-browse-users.py --users 100 --diagnostic-retry
```

Keep Cloud Shell open for up to seven minutes and retain the complete output
on either pass or failure. The runner still stops on the first error and requires
full Job/resource/serving-pod evidence for BROWSE_USERS_100_OK. There is no higher
stage or notification test. The original token-renewal Job stays untouched.

All 453 offline methods passed. Added regression coverage verifies allowlisted
service codes, exclusion of arbitrary payload/counter fields, before/after
request-error deltas, changed-process/unavailable snapshot behavior and separate
retry Job gate/UID-scoped stop/overlap guards. Shortened real loopback HTTP
smokes again exercised all 50 and 100 users (75 and 148 valid reads respectively)
and stopped the server. Native isolated Job renders retained identical security
and resource settings. The live diagnostic retry remains pending.


## Diagnostic retry result: pool rejection during growth

Run 05ba94b69327 / Job browse-users-100-diagnostic passed startup metric gating
and stopped after 1.051 seconds. Thirty-one users issued 31 requests: 29 HTTP
200s and two HTTP 503s, both carrying the allowlisted DATABASE_BUSY code.
Peak sampled workload concurrency was 22. The first rejection was general
availability, 43.141 ms latency, at phase time 0.610 seconds. This is a partial
failed workload; no 100-user pass was established.

Both pools began with size/available one and zero waiting. After in-flight
requests finished, both had size/available four and zero waiting. Same process
IDs were retained. g649w counter deltas were requests 18, connection attempts
three and request errors two; k6fdk deltas were requests 13, connection attempts
three and request errors zero (its historical request-error count remained one).
Both replicas had zero new connection errors, losses or bad returns. Thus the
diagnostic retry's errors occurred on g649w, unlike the first attempt's k6fdk.

DATABASE_BUSY confirms the application's pool rejection path. The quick 43 ms
response, compared with a configured three-second acquisition timeout, together
with pools growing one to four, strongly suggests the eight-place waiting queue
filled during growth. This is an inference: the service code groups queue-full,
timeout and closed-pool exceptions. The sequential post-stop snapshots are not
peak queue measurements and cannot prove saturation exactly at rejection time.
The coarse resource samples mostly cover startup and do not characterize the
subsecond request burst. Do not infer a PostgreSQL or sustained capacity ceiling.

The configured pool minimum one and max_idle 60 seconds allow unused capacity
to shrink before a later burst. The next proposed comparison is keeping four
connections ready per Browse pod, retaining the existing maximum four and wait
limit eight, then repeating the same paced 100-user workload under a separate
reviewed Job. This would raise the idle connection floor to eight across two
Browse pods, within their existing steady maximum; startup/rolling surge and
other workloads still need their own budget. No such pool-policy change or new
load run has been performed yet.

The runner requested a UID-scoped stop and retained files
browse-users-05ba94b69327-files. Confirm terminal diagnostic Job status and fresh
demo/Operations health before a deployment or new experiment. Pre-run checks
were healthy (inventory 100 each, processed total 72, no backlog/alerts); fresh
post-retry health has not yet been supplied. The renewal Job is unchanged.


## Recovery confirmed; warm-pool comparison prepared

The user confirmed browse-users-100-diagnostic Failed (4m5s). Fresh demo checks
reported alive/ready, two events and availability 100 each. Operations remained
healthy: processed 72, pending/outstanding work zero, no active alerts or worker
errors; historical publish failures four and dead-letter audit count one stayed
unchanged. Heartbeats were consumer 3.302005s and publisher 0.781066s. This confirms
the post-diagnostic recovery checkpoint; no native broker count was measured.

Source now adds BROWSE_DB_POOL_MIN, default one for ordinary/local diagnostics.
The Browse Kubernetes manifest sets minimum four and maximum four. When minimum
exceeds one, startup waits up to 30 seconds for the initial pool to fill before
publishing it to request handlers. Failure closes the partial pool and fails
startup. This follows [Psycopg's startup check](https://www.psycopg.org/psycopg3/docs/advanced/pool.html#pool-startup-check).
Booking retains minimum one. The isolated one-slot Browse probe explicitly
sets both minimum and maximum one in its own process. The already-running
token-renewal Job and its mounted scripts are not changed.

This requires a new Browse image and rollout. Two serving Browse pods now aim
to retain eight idle connections in total, with the same existing maximum of
eight. The default rolling update can temporarily have three pods (up to 12
Browse connections), in addition to Booking, workers and diagnostic connections.
No cluster-wide connection ceiling is established by these per-process limits.
Maximum waiting remains eight and acquisition timeout remains three seconds;
request pacing, stop conditions, app resources and replica counts are unchanged.
Fixed minimum does not guarantee every connection is usable at every instant:
replacement/checking after lifetime expiry or failure still occurs.

After the build/rollout and fresh health checks, select the separate comparison:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-browse-image.sh
```

After IMAGE_BUILD_OK:

```bash
bash scripts/deploy-browse.sh &&
bash scripts/check-browse-http.sh --demo &&
bash scripts/check-operations-http.sh &&
python3 scripts/run-browse-users.py --users 100 --warm-pool-comparison
```

The new fixed Job is browse-users-100-warm. Existing failed Jobs are retained;
active load Jobs and an existing warm comparison still block startup. The flag
requires 100 users and cannot be combined with --diagnostic-retry. Preflight
requires the deployed minimum/maximum environment values both four. The in-cluster
probe additionally checks both actual serving pools report minimum four and
size four before user traffic; this prevents an old image ignoring the new
setting from being treated as a warm-pool test. Initial validated statistics
are retained in the workload result, and final evidence validation checks both
expected pods. Existing HTTP/pool failure capture remains active. One-minute
100-user/3,000-GET caps and startup/resource gates are unchanged. Keep Cloud Shell
open for up to seven minutes. No 250-user progression is started.

All 458 offline methods passed, including warm startup wait/cleanup on timeout,
invalid minimum bounds, unchanged Booking policy, refusal of a cold serving
baseline, valid warm baseline, selected Job/config propagation and rejection of
missing warm evidence. Shortened real loopback HTTP smokes exercised all 50 users
(75 reads), all 100 users (149 reads), and a simulated warm baseline with all
100 users (147 reads). Native isolated renders checked the new Job's unchanged
budgets and deployment min/max four. These are local correctness checks, not
real SQL/warm-pool performance evidence. Azure build, rollout and live comparison
are still pending. Docker remained stopped and no local Azure login was used.


### Warm image built; deployment and comparison pending

The user supplied ACR run cab, successful after 37 seconds, with
IMAGE_BUILD_OK and tag browse-20261024141922-13349. The immutable reference saved
by the build helper to browse-image.txt is:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:85b47ca41a2d1add3fd7f1deac95a63d9cf44e08c412ba07d23e341d5c62361e
```

The Python 3.12-slim-trixie base digest remained
a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704.
Build success establishes the release artifact, not deployed pool readiness or
100-user performance. Next deploy using the saved immutable reference, run demo
and Operations checks, then the explicit --users 100 --warm-pool-comparison
command above. No warm rollout or live comparison result has yet been supplied.


### Warm rollout healthy; first launch blocked before Job creation

The user supplied BROWSE_DEPLOYED with new Ready, zero-restart pods
browse-594b8f9998-9pww6 (10.244.3.88, apps node vmss000000) and
browse-594b8f9998-wm7ln (10.244.2.183, apps node vmss000001). An old k6fdk pod was
still Terminating in that rollout snapshot. Demo checks passed with both
availabilities 100. Operations remained healthy at processed 72, no backlog or
active alerts, unchanged historical publication failures four and audit count
one; heartbeats consumer 3.118647s/publisher 1.664247s without errors.

The immediate comparison launch failed at baseline_cpu_memory with
BROWSE_RESOURCE_METRICS_MISSING. Job/files were null, the gate remained closed
and no generator stop was needed: no load Job or workload was started. New pods
were only seconds old in the rollout output, making collector startup delay
the likely explanation. The warm-pool live workload is still untested.

Allow about two minutes for collection after the rollout, then check:

```bash
kubectl top pods -n trafficxx -l app.kubernetes.io/name=trafficxx-browse
```

Once both new pods have resource entries, rerun the same explicit comparison:

```bash
python3 scripts/run-browse-users.py --users 100 --warm-pool-comparison
```

The runner itself still validates selected pod identity, metric completeness
and source age; kubectl top is only a readiness hint. No freshness or resource
limit was relaxed. Because this launch created no Job, the fixed comparison
name remains available; no deletion or rebuild is required. If metrics remain
missing, retain the new failure rather than bypassing the guard.


## Live warm-pool comparison: failed near the end of the workload

The user supplied run 657965cc61f8 / Job browse-users-100-warm on image
85b47ca41a2d1add3fd7f1deac95a63d9cf44e08c412ba07d23e341d5c62361e.
Resource gating passed and both actual serving pools began at minimum/maximum
four, size/available four, no waiters and no request errors.

All 100 users issued requests. The workload stopped after 56.940 seconds:
2,567 attempts, 2,565 HTTP 200 responses and two HTTP 503s carrying DATABASE_BUSY.
Achieved rate across this partial run was 45.082 requests/s; sampled peak workload
HTTP concurrency was 23. Route attempts were 1,283 general and 1,284 seated.
Mixed-response latency was p50 188.795 ms, p95 323.629 ms, p99 388.640 ms and
maximum 493.550 ms. The first failing request was seated availability, 37.893 ms,
at phase time 56.550 seconds. These are partial failed-run measurements, not a
successful 60-second workload, capacity verdict or production latency SLO.

Post-stop counters retained the same process IDs and showed:

| Pod | Requests delta | Connection attempts delta | Request errors delta | Final size / available |
| --- | ---: | ---: | ---: | ---: |
| browse-594b8f9998-9pww6 | 1318 | 0 | 2 | 4 / 4 |
| browse-594b8f9998-wm7ln | 1260 | 0 | 0 | 4 / 4 |

Both had zero new connection errors, losses or bad returns and zero post-stop
waiters. No connection creation was needed during the workload. The new floor
was therefore in effect, but it did not eliminate rejections. The earlier
startup-growth hypothesis cannot explain the whole remaining behavior. Brief
saturation of the bounded waiting queue is the leading inference from fast
rejection with warm pools; DATABASE_BUSY still groups queue-full, timeout and
closed-pool exceptions. Post-stop idle capacity does not establish peak queue
length or exact per-request attribution. No observer stop was recorded.

Sampled maxima across startup and load were:

| Pod | Distinct source samples | CPU cores | Memory MiB |
| --- | ---: | ---: | ---: |
| browse-594b8f9998-9pww6 | 5 | 0.040612 | 59.598 |
| browse-594b8f9998-wm7ln | 5 | 0.037071 | 59.965 |
| browse-users-100-warm-nplls | 3 | 0.291812 | 227.008 |

The generator sample is about 88.7% of its 256 MiB limit, above the configured
85% sampled-pressure threshold. The run reported an HTTP failure, not a
sustained-resource stop; the coarse API samples do not establish how long that
usage persisted. Do not describe the generator as proven to have ample memory
headroom. Review its overhead and collection timing before larger user counts.

The parent reported DIAGNOSTIC_JOB_FAILED and requested a UID-scoped stop;
files browse-users-657965cc61f8-files are retained. Confirm native terminal Job
status and fresh demo/Operations health next. Successful pre-load inventory
checks do not replace a post-failure check. No final BROWSE_USERS_100_OK or
post-run serving-pod stability verdict was produced. Do not delete/rerun the
fixed comparison blindly or progress to 250 users. A future experiment should
review bounded waiting capacity and the generator while preserving an explicit
request latency stop; no further tuning or new run has been applied yet.


### Recovery confirmed after the warm comparison

The user confirmed browse-users-100-warm terminal Failed (5m55s). Fresh demo
checks reported alive/ready, two events and 100 available units each. Operations
reported processed 72, zero pending/outstanding work and ages, zero processing
failures, no active alerts and unchanged historical publication failures four
and dead-letter audit count one. Consumer/publisher heartbeats were present
without errors (5.252435s and 1.076952s). Native broker counts were not measured
by this check. This completes the post-warm-comparison health checkpoint.

The current measured boundary remains: the bounded 50-user workload passed;
the tested 100-user workload did not meet its zero-error stop policy, even with
a warm four-connection pool. It is not a precise maximum user count or a
production SLO. Do not infer an ongoing outage from the terminal Failed test Job.
No new load run, pool/queue tuning or resource change is started by this result.
Before further tuning, the useful next preparation is per-exception pool
rejection metrics (queue-full versus timeout/closed), alongside a review of the
load generator's observed memory overhead. Another identical retry alone would
not resolve those diagnostic gaps. The original renewal Job stays unchanged.


## Prepared per-exception rejection and generator-memory diagnostics

Following the recovered warm-run checkpoint, the user authorized better
observability before further tuning. Source now counts caught pool rejection
exceptions separately, under a lock, in process-local monotonic counters:

| Statistics field | Caught exception | Browse Prometheus metric |
| --- | --- | --- |
| rejections_queue_full | TooManyRequests | trafficxx_browse_pool_queue_full_total |
| rejections_timeout | PoolTimeout | trafficxx_browse_pool_acquire_timeouts_total |
| rejections_closed | PoolClosed | trafficxx_browse_pool_closed_rejections_total |

The public HTTP response remains 503 / DATABASE_BUSY. No raw exception text,
connection parameters, SQL or response payloads are added. Statistics reads
remain nondestructive and borrow no SQL connection. Counters reset on process
replacement and are compared only with the same process ID. The shared renderer
also supports Booking prefixes in future images, but no Booking rollout is
performed by this work. Current Prometheus scrape configuration will collect
additional Browse series after deployment; the existing stored-sample checker
still covers its original metrics and does not separately prove these new
series. No dashboard panel, alert rule or external notification is added.

The generator already bounds receipt storage to 30 attempts per user (at most
3,000 records at 100 users). That review does not identify a memory leak or
attribute the observed 227 MiB to Python objects, native allocations or thread
stacks. It uses 100 user threads plus its observer. Instead of changing the
resource limit or concurrency, the probe now samples its own Linux
/proc/self/status before the workload, during observer iterations (roughly one
second apart), and after in-flight work ends. It reports numeric process RSS,
thread counts and sampled maxima in generator_memory on success and failure.
Process RSS differs from container/cgroup memory, includes resident thread
stacks, and can miss peaks between samples. Unsupported/unavailable readings are
null; Kubernetes resource gates remain required. No extra load thread or
unbounded memory profile is added, and no memory reduction is claimed.

A separate explicit mode retains the existing warm policy and adds the new
counter requirement:

```bash
python3 scripts/run-browse-users.py --users 100 --warm-pool-comparison --rejection-diagnostics
```

It creates fixed Job browse-users-100-rejections, preserving all previous failed
Jobs. The flag requires the warm 100-user comparison. Active-load/existing-Job
checks, ownership-scoped gate/stop, unchanged serving snapshots and final evidence
checks remain. The actual initial pool statistics must contain all three numeric
rejection counters or the probe fails before offering user traffic. Failure
snapshots/deltas include these counters; increases also trip the existing
pool-error stop. Final success requires verified initial counter evidence.
All pool/resource/wait/timeout/request-count/pacing limits remain unchanged.

This needs another Browse image because the serving process owns the counters:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-browse-image.sh
```

After IMAGE_BUILD_OK, deploy and check fresh health. Following a rollout, wait
for both new pods to have resource metrics before launching the diagnostic.
A missing baseline metric still fails before Job creation; do not bypass it.
The original renewal Job and its mounted scripts stay untouched. Do not start a
higher user stage based on this preparation.

Local validation: all 464 offline methods passed, including separate rejection
types with unchanged HTTP codes, 100 concurrent increments without loss,
numeric-only metric rendering, Linux memory parsing/missing sources, per-type
failure deltas, new-counter presence guards and separate diagnostic Job/config
selection. Shortened real loopback HTTP checks exercised 50 users (75 reads),
100 users (148 reads) and warm/counter-enabled 100 users (149 reads). All passed,
and the local server stopped. Native kubectl with an isolated empty kubeconfig
rendered the new Job with unchanged resource/security settings. These are local
correctness checks, not SQL performance or a live memory verdict. The new image
build, rollout, stored rejection-series checks and live diagnostic are pending.
Docker stayed stopped and no local Azure connection was used.


### Rejection-diagnostic image build succeeded

The user supplied ACR run cac, successful after 31 seconds, tag
browse-20261024144301-2030, with IMAGE_BUILD_OK and this immutable reference saved
to browse-image.txt:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:826c54fdaf77dba533b77779b8e6e127b871c9684a4045be58d29f4c42002fa4
```

Python base digest remained
a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704.
This is build evidence only. Deploy with scripts/deploy-browse.sh, run demo and
Operations health checks, then allow collection for both newly created pods
and confirm their CPU/memory entries with kubectl top pods. The explicit
--users 100 --warm-pool-comparison --rejection-diagnostics mode still checks
metric completeness and actual warm/counter evidence itself. It uses the new
fixed Job browse-users-100-rejections and retains earlier failed Jobs.
No cac rollout, new metric-series proof or live rejection diagnostic result
has yet been supplied. The original renewal Job remains untouched.


## Live rejection diagnostic: queue-full confirmed

The user supplied run 34c3537847e8 / Job browse-users-100-rejections on image
826c54fdaf77dba533b77779b8e6e127b871c9684a4045be58d29f4c42002fa4.
Warm and counter guards passed on the new serving pods
browse-545765768f-8gvvl and browse-545765768f-ntfhl. This confirms the diagnostic
image was serving with the required numeric fields; separate new-series
Prometheus storage proof was not supplied.

All 100 users issued requests: 913 attempts, 911 HTTP 200s and two HTTP 503s
carrying DATABASE_BUSY. The run stopped at 20.592 seconds, with achieved partial
rate 44.337 requests/s and sampled peak HTTP concurrency 22. Mixed latency was
p50 212.362 ms, p95 364.239 ms, p99 409.523 ms, maximum 456.424 ms. The first
failure was seated availability at 20.217 seconds, taking 30.361 ms.

Both pools started and ended at size/available four, no waiters, and the same
process IDs. No physical connections were created during the workload.
8gvvl deltas were 464 borrows and zero rejection/connection-error counters.
ntfhl deltas were 453 borrows, two request errors, two rejections_queue_full,
zero rejections_timeout and zero rejections_closed. Neither pod recorded new
connection errors, losses or bad returns. The exact caught rejection type is
therefore now confirmed as TooManyRequests / queue-full, rather than merely
inferred from DATABASE_BUSY latency. Counters do not reveal the precise lifetime
of each queue peak or attribute every borrow to a workload rather than readiness
request. The queue limit remains eight per pod; connection maximum remains four.

Generator /proc/self/status sampled process RSS started at 28.609 MiB with one
thread, peaked at 209.008 MiB/102 threads across 22 samples, and ended at
206.301 MiB with one thread. Significant resident memory remained after worker
shutdown, but these observations alone cannot distinguish retained allocator
memory from a leak or attribute bytes to Python/native objects or thread stacks.
Do not equate process RSS with the 256 MiB cgroup limit or claim a memory fix.
The coarse Kubernetes resource maxima in this short run mostly cover startup
and substantially underrepresent the in-process workload memory observation.
Existing resource limits/stop conditions have not been changed.

The Job failed and its parent requested a UID-scoped stop, retaining files
browse-users-34c3537847e8-files. Native terminal status and fresh demo/Operations
checks are the next checkpoint; no post-run inventory/stability verdict or
BROWSE_USERS_100_OK was produced.

The next proposed controlled tuning comparison is a waiting limit of 16 rather
than eight, retaining four ready/max connections per Browse pod, the same paced
100-user workload and the two-second request stop. A larger bounded queue may
absorb brief contention but can increase latency; it is not more database
execution capacity and does not promise a successful run. The generator still
needs its current resource supervision. No queue change or new load has been
implemented or run at this checkpoint. Do not progress to 250 users.


## Recovery confirmed; bounded queue comparison prepared

The user confirmed browse-users-100-rejections terminal Failed (5m57s).
Fresh demo checks reported alive/ready, two events and 100 available units each.
Operations remained healthy at processed 72, no pending/outstanding work,
processing failures or active alerts, and unchanged historical publication
failures four/audit count one. Both worker heartbeats were present without
errors (consumer 2.266782s, publisher 1.492495s). Native broker counts were not
measured. This completes the recovery checkpoint after queue-full confirmation.

Prepared source adds optional BROWSE_DB_POOL_MAX_WAITING, default eight when
unspecified. Its only accepted values are eight and 16; zero/unlimited and
unreviewed sizes fail before creating a pool. The Browse deployment sets 16.
Booking keeps its existing queue limit eight. Browse minimum and maximum remain
four, with the same startup wait, three-second acquisition timeout, checked
connections and callable fresh identity parameters. No replica, query, database
grant, request pacing or generator resource setting is changed.

The actual limit is exposed in numeric pool_waiting_limit and the Prometheus
gauge trafficxx_browse_pool_waiting_limit. Previous images without this field
can still be read as the old reviewed eight-place policy by generic diagnostics;
the explicit 16-place comparison requires the new actual field. Borrow/counter
snapshot helpers validate observed waiters against the reported reviewed cap,
rather than incorrectly rejecting a queue of nine as a configuration error.
The same original sustained-wait and latency stop conditions remain.

The explicit new mode is:

```bash
python3 scripts/run-browse-users.py --users 100 --queue-comparison
```

It implies warm-pool and rejection diagnostics and creates fixed Job
browse-users-100-queue16. It requires 100 users and excludes the earlier generic
retry flag. Preflight requires the deployed queue environment setting 16; the
in-cluster probe independently verifies both actual pool limits before user
traffic. Initial and final success evidence retain and validate the selected
limit. Old eight-place experiment modes are not substitutes for this comparison.
Earlier failed Jobs remain intact, and active loads/existing fixed Jobs still
block startup. No HTTP or workload retry happens automatically.

The comparison keeps 100 users, 60 seconds, two-second pauses, maximum 3,000
GETs, four ready/max connections per Browse pod, two-second observed request
stop, original error/wait/resource gates, and generator one CPU/256 MiB limits.
All rejection counters and sampled Linux RSS/thread reporting remain active.
A larger queue increases the number of requests permitted to wait; it does not
increase SQL execution capacity and can worsen latency. No successful 100-user
result, memory improvement or higher-stage readiness is claimed.

A new Browse image is needed for the configurable queue and actual-limit field:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-browse-image.sh
```

After IMAGE_BUILD_OK:

```bash
bash scripts/deploy-browse.sh &&
bash scripts/check-browse-http.sh --demo &&
bash scripts/check-operations-http.sh
```

Allow collection for the new pods, confirm both have CPU/memory readings with
kubectl top pods, then run the explicit queue comparison. Keep Cloud Shell open
for up to seven minutes and share the complete pass/failure output. The build,
rollout and live queue comparison are pending; the original renewal Job stays
untouched. No 250-user experiment is started.

All 468 offline methods passed. Added cases cover bounded queue configuration,
zero/unreviewed-value rejection, unchanged Booking limit, actual-limit guards,
refusal of the old deployment policy, selected Job/config/UID-scoped stop,
waiting values above the former cap, and final evidence matching queue 16.
Shortened real loopback HTTP checks exercised all 50 users (74 valid reads),
100 users (149), and warm/counter-enabled queue-16 100 users (149), with the
server stopped afterward. Native isolated Job renders preserved resource and
security settings and validated the deployment environment. These establish
local correctness, not Azure SQL capacity or a latency SLO. Docker stayed
stopped and no local Azure connection was used.


### Queue-comparison image build succeeded

The user supplied ACR run cad, successful after 31 seconds, tag
browse-20261024145900-12748, with IMAGE_BUILD_OK. The build helper saved this
immutable reference to browse-image.txt:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:d383adee4fef71364a5bc3fe579a900ff02de14a36d44795176b2040dc56f640
```

Python base digest stayed
a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704.
Build success does not establish deployment, actual serving queue limit or
100-user performance. Next deploy using the saved reference, run fresh demo
and Operations checks, wait for resource metrics on both replacement pods,
then run --users 100 --queue-comparison. The new fixed Job will be
browse-users-100-queue16, with actual queue-limit, warm-pool and rejection-counter
guards active. The cad rollout and live comparison are still pending.


## Live 100-user queue-16 comparison passed

The user supplied both BROWSE_USERS_WORKLOAD_OK and final BROWSE_USERS_100_OK
for run 14a565ae316f / Job browse-users-100-queue16 on the cad image
d383adee4fef71364a5bc3fe579a900ff02de14a36d44795176b2040dc56f640.
The generator ran on existing apps node aks-apps-35966020-vmss000001.
Warm-pool, rejection-counter, actual queue-16 and resource gates passed.

All 100 users issued requests. Across 60.134 seconds, all 2,734 attempts returned
valid HTTP 200 responses, with no workload or observer errors. Routes were
1,369 general and 1,365 seated reads. Achieved rate was 45.465 requests/s and
sampled peak workload HTTP concurrency 23. Latency was p50 172.271 ms,
p95 313.140 ms, p99 393.370 ms and maximum 469.010 ms.

Both pools began at minimum/maximum four, size/available four, queue limit 16,
zero waiting and zero rejection counters. Process IDs remained unchanged:
9b2677bed6a74d488be132ac9174e5dc (browse-5648774c-b45zk) and
b812e5c107c14201b69ffbe409ae0d1b (browse-5648774c-xqzc5).

| Pod | Borrow delta | New connection attempts | Sampled peak pool size | Sampled peak waiting |
| --- | ---: | ---: | ---: | ---: |
| browse-5648774c-b45zk | 1381 | 0 | 4 | 7 |
| browse-5648774c-xqzc5 | 1365 | 0 | 4 | 6 |

All pool error and per-exception rejection counters stayed unchanged. Borrow
deltas include readiness/background reads. Sampled waiting below eight does
not contradict earlier queue-full evidence: snapshots can miss short peaks.
A passing run under queue 16 is consistent with the proposed tuning, but it
does not isolate that setting as the sole cause: serving pods were rolled out,
the generator used a different node than prior attempts, and trials vary.

The final parent verdict confirmed the same serving pods throughout the run,
matching successful Job/owned generator evidence, actual warm/counter/queue
configuration and resource gates. No inventory writes occurred; both demo
availabilities stayed 100. This is a successful bounded, paced in-cluster Browse
read experiment, not reservation-write/public-ingress/sustained-capacity or
production latency SLO proof. No higher stage was started.

Generator memory still warrants attention. Process RSS started at 28.371 MiB
with one thread, sampled a maximum 241.094 MiB/102 threads across 61 samples,
and ended at 236.566 MiB with one thread. Kubernetes resource samples were:

| Pod | Distinct source samples | CPU cores maximum | Memory MiB maximum |
| --- | ---: | ---: | ---: |
| browse-5648774c-b45zk | 4 | 0.041126 | 59.680 |
| browse-5648774c-xqzc5 | 4 | 0.031217 | 59.887 |
| browse-users-100-queue16-zhhdn | 3 | 0.380182 | 228.059 |

These are different memory measures and collection times; process RSS cannot
be substituted for cgroup usage or aligned exactly with an API sample.
Nevertheless, both observations leave limited headroom against the generator's
256 MiB container limit. The reported run passed its sampled supervision;
a high maximum alone does not establish the configured ten-second sustained
memory-stop condition. Do not describe the generator as adequately sized for
250 or 1,000 users, and do not silently relax the memory guard.

Next obtain a fresh demo/Operations health checkpoint, then investigate or
reduce generator overhead before preparing a separately reviewed larger stage.
No new full SQL/broker audit is implied by this read-only success, and no
post-success Operations result has yet been supplied. The original renewal
Job remains untouched and its final expiry-boundary verdict is still separate.


## Post-100 health confirmed; HTTP client memory comparison prepared

The user supplied fresh BROWSE_DEMO_HTTP_OK with alive/ready and 100 available
tickets for each event. OPERATIONS_HTTP_OK confirmed processed 72, no pending
or outstanding work, zero processing failures and no active alerts. Historical
publication failures four and dead-letter audit count one stayed unchanged;
consumer/publisher heartbeats were present without errors (3.385009s/0.172123s).
This completes post-success application health checks; it is not a fresh native
broker-depth or full SQL audit.

Code review found that each load request constructed a default urllib opener.
[Python 3.12's HTTPSHandler constructs a TLS context at initialization](https://github.com/python/cpython/blob/v3.12.15/Lib/urllib/request.py#L1275),
and build_opener includes that default handler even for the probe's internal
HTTP requests. The local Windows Python 3.10 handler behaves differently, so
local memory figures cannot reproduce or establish the Linux/Python 3.12 cause
of the earlier 241 MiB process peak. Repeated handler/TLS-context construction
is avoidable work; its contribution to actual live RSS remains to be measured.

The shared read client now explicitly constructs only HTTP handlers, with an
empty proxy configuration, the existing redirect refusal, numeric/status/body
handling and no cookie/auth/cache/file/FTP/HTTPS handlers. Non-HTTP schemes fail
before network access. An opener is retained only within its own worker thread.
Each call still creates a fresh Request and HTTP connection, sends the customer
header for that call, reads at most 64 KiB and closes the response. This is not
HTTP keep-alive, response caching, a database change or a TLS-verification
exception; the fixed workload targets were already internal HTTP endpoints.
Application HTTPS access and the original renewal Job are unaffected.

A bounded local comparison used separate fresh client processes, 100 users and
600 loopback reads per run. Default per-request construction built 600 openers;
the new source constructed 100 HTTP-only openers. The Windows working-set peaks
were about 28.6 versus 29.0 MiB, respectively: no measured local RSS reduction.
That platform/runtime differs from the Azure job; do not present this as a
memory fix already verified in Linux. A handler-reuse-only intermediate also
failed to show a Windows memory benefit and was replaced by the explicit
HTTP-only design. The intended live comparison keeps the same workloads and
uses the existing in-process/Kubernetes memory observations.

The explicit new command is:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/check-browse-http.sh --demo &&
bash scripts/check-operations-http.sh &&
python3 scripts/run-browse-users.py --users 100 --client-memory-comparison
```

It selects the new fixed Job browse-users-100-clientmemory and implies the
existing warm, rejection-counter and queue-16 guards. The existing deployed
cad image is reused as the runtime; the new stdlib scripts are mounted from a
per-run ConfigMap, so no image build, application rollout or infrastructure
change is required. All prior Jobs are preserved. Active-load/existing-Job,
identity/ownership/stability and original resource/error/wait/latency gates
remain. The workload is still 100 users for one minute, two-second pauses and
at most 3,000 GETs, with generator one CPU/256 MiB, four database connections and
16 waiting slots per Browse pod. No resource limit or stop threshold is relaxed.

The probe requires the new helper policy before user traffic and reports
http_client_policy=thread-local-http-only-fresh-connections-v1. The final parent
verdict requires that policy and actual Linux RSS/thread samples as well as
all existing successful workload/Job/pod evidence. A successful workload does
not automatically prove memory reduction: compare its observed memory to the
prior same-size trial before preparing a higher user stage. Keep Cloud Shell
open for up to seven minutes and retain complete output on pass or failure.
No live client-memory-comparison result has been supplied yet.

All 473 offline methods passed, including per-thread isolation, fresh request
headers, response closure on success/oversize, HTTP-only/no-TLS construction,
unsupported-protocol refusal, selected Job/config/UID-scoped stop and required
client/memory evidence. Actual shortened loopback HTTP tests exercised all
50 users (75 valid reads), 100 users (148), and warm queue-16 100 users (149).
HTTP/1.1 requests used Connection: close; a 302 was not followed. The server
stopped. Isolated native Job renders confirmed unchanged budgets/security.
These are correctness checks, not Azure SQL, memory or latency SLO proof.
Docker remained stopped and no local Azure login or credential file was used.
