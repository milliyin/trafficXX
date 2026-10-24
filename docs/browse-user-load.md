# First in-cluster user-load stage

Status: source and local validation passed. Live AKS results remain pending.
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
generator gets 40 seconds after it is first seen Running for metrics to appear.
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
run's Job must not be modified. The Job itself has a 180-second active deadline,
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
browse-users-50 Job and an active earlier read-load-baseline Job. Concurrent
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
