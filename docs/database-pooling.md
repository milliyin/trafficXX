# Browse database pooling

Status: Azure image build ca6, Browse rollout, demo HTTP check and isolated real
SQL pool probe passed. A subsequent ca7 rollout and bounded serving-pod
concurrency check also passed. No Terraform or database grants changed.
Sustained load and token renewal across expiry remain unverified.

The user restored a fresh Cloud Shell clone after the previous browser session
expired. ACR run ca6 succeeded after33seconds, tag browse-20261023150647-3520:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:44f5a740913e28861f8549e0324356705731a84b5dcc557d8257c286e59e0969
```

The reported Python base digest matches publisher ca4. The validated immutable
reference was saved in browse-image.txt; the deployment helper uses that file.
Build success alone does not establish live pooling, a vulnerability scan or
hosted CI success. Live rollout and probe evidence follow below.

## User-run Azure results

The new Browse pods browse-858f7d4fff-nbznz and browse-858f7d4fff-v9mnv were
Ready1/1 with zero restarts. Both were on aks-apps-35966020-vmss000000 in the
supplied snapshot; this does not demonstrate node separation or node-failure
tolerance. BROWSE_DEMO_HTTP_OK confirmed liveness/readiness, two events and100
available units each for demo-general and demo-seated.

The separate browse-pool-check Job completed with BROWSE_POOL_OK, identity
trafficxx-lab-browse and TLSv1.3. It verified physical connection reuse, reuse
after transaction rollback,503 capacity rejection with a one-slot diagnostic
pool, and replacement of a deliberately closed connection. No inventory writes
were performed. The log warning "discarding closed connection" is expected from
that deliberate close; the subsequent replacement check passed.

The serving pods retain their configured maximum of four connections per
process. The probe's one-slot limit applies only to the temporary process. This
probe does not establish serving-pod connection reuse, throughput improvement,
load behaviour, long-duration renewal or a whole-cluster connection ceiling.

Earlier work reused one WorkloadIdentityCredential and its SDK token cache.
That saved authentication work but did not reuse PostgreSQL connections. Neither
the source nor infrastructure had a SQL pool or PgBouncer configuration.

## First stage: Browse only

Browse now owns one synchronous psycopg-pool3.3.3 pool per Uvicorn process. The
FastAPI lifespan opens it and closes it at shutdown; imports create no threads
or connections. Both Browse endpoints and database readiness borrow from it.
Booking, workers, migrations and diagnostic scripts using db.connect keep their
existing direct connections.

| Setting | Value | Purpose |
| --- | --- | --- |
| Minimum | 1 per process | Keep one connection ready |
| Maximum | BROWSE_DB_POOL_MAX=4, allowed1..8 | Bound simultaneous SQL connections |
| Waiting queue | 8 requests | Avoid unlimited queued borrowers |
| Acquisition timeout | 3 seconds | Return503 DATABASE_BUSY when capacity is unavailable |
| Maximum lifetime | 1800 seconds, library jitter | Replace old sessions when returned |
| Extra idle connection age | 60 seconds | Shrink toward the minimum |
| Checkout check | ConnectionPool.check_connection | Check a connection before handing it out |

With two Browse pods and one process per pod, the steady-state ceiling is eight
connections. Extra rollout replicas and terminating pods can add connections;
this is not a whole-cluster cap. Additional Uvicorn workers also multiply pools.
Other services and administrator connections need their own database budget.
No throughput gain or latency target has been measured yet.

Pool.connection commits on normal exit, rolls back on exceptions and returns
the connection. A closed/broken connection is replaced. This does not reset
arbitrary session settings; Browse must not add persistent SET commands or
session-level advisory locks without a reset policy. Existing transaction-level
locking elsewhere is unchanged. A connection failure during a query is not
automatically replayed.

## Azure tokens and TLS

Pool kwargs are a callable evaluated for every new physical connection attempt.
It invokes the same validated db.connection_parameters used by direct logins.
Workload identity requests a usable SDK-managed token each time; no startup
token is saved in pool kwargs. Existing sessions are reused without obtaining
a token on each checkout. New sessions retain explicit verify-full CA validation,
the Browse SQL identity and the existing connection/statement/lock timeouts.
URL mode still supports local development; conflicting cloud DATABASE_URL is
rejected. Real renewal across an Azure token expiry boundary remains unverified.

See the primary [Psycopg pool API](https://www.psycopg.org/psycopg3/docs/api/pool.html)
and [pool release notes](https://www.psycopg.org/psycopg3/docs/news_pool.html) for
callable parameters, transaction handling and queue/lifetime semantics.

## Build first in Cloud Shell

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/build-browse-image.sh
```

Record IMAGE_BUILD_OK and its returned digest before rollout. Build success is
not deployment or vulnerability-scan evidence. The shared runtime image now adds
psycopg-pool; deploying Browse does not update the other services' pinned images.

After reviewing the successful build, deploy with the existing guarded helper:

```bash
bash scripts/deploy-browse.sh &&
bash scripts/check-browse-http.sh --demo
```

The helper substitutes the validated digest from browse-image.txt. Keep the old
Browse revision for rollback: kubectl rollout undo deployment/browse -n trafficxx,
then wait for rollout and repeat the demo HTTP check if rollback is needed.

Next run the isolated live pool check:

```bash
bash scripts/check-browse-pool.sh
```

It uses the deployed immutable Browse image and existing Browse service account
in a temporary Job, with a single-connection pool only in that diagnostic process.
It checks sequential reuse via backend PID, a deliberate SELECT1/0 followed by
rollback/reuse, bounded exhaustion returning503, and replacement after closing
its own connection. It reads the current identity/TLS state and writes no ticket
inventory. BROWSE_POOL_OK is separate from the serving pods' HTTP proof; it does
not establish serving-pod reuse under load or token renewal across expiry.

The Job has no retries, a180second deadline and600second completed-Job TTL. If
it fails, inspect logs before retrying. For an existing Job name, delete only
job/browse-pool-check in namespace trafficxx before rerunning. No PVCs or Secrets
need deletion.

## Verification limits

The [bounded serving-pod concurrency check](browse-concurrency.md) passed after
the ca7 rollout with internal pool statistics. Each pod served30test reads with
32pool borrows and two new connection attempts; highest sampled size was three
against a configured maximum of four. This supports serving-process reuse for
the bounded workload, not sustained-load performance or a latency SLO.

Offline tests cover pool bounds, fresh parameter callbacks, cloud authentication
and TLS preservation, sanitized503 responses, shutdown/startup cleanup and
readiness using the same pool. A local smoke check using the actual pinned
Psycopg/Pool/FastAPI packages passed callback compatibility, lifespan, HTTP
success and capacity errors without a database or Azure access.

Five real PostgreSQL tests were added to the existing dedicated trafficxx_test
CI suite: physical reuse, failed-write rollback, bounded exhaustion/recovery and
closed-connection replacement, and serving HTTP requests updating the same pool
statistics. They were not run locally because Docker remains
stopped. Their hosted CI result remains unchecked. The separate Azure probe and
Browse HTTP and bounded serving-pod checks above passed; sustained-load and
token-expiry experiments remain pending.
