# Bounded concurrency check of the serving Browse pods

Status: ACR build ca7, Browse rollout, demo HTTP and the bounded serving-pod
experiment passed in user-run Cloud Shell. Sustained load and token-expiry
renewal remain unverified.

## Verified Azure result

ACR run ca7 succeeded after42seconds, tag browse-20261023153010-470, image:

```text
trafficxxacre8708a85.azurecr.io/trafficxx@sha256:300d1de5b7e625ed3da2b38e2a38816b4cbe9fd4565f014cf2dec5be18ddd16f
```

The reported Python base digest matches ca6. The user deployed this image and
confirmed BROWSE_DEMO_HTTP_OK with two events and100available units each.
New pods browse-79b5d8cd78-4b6fc and browse-79b5d8cd78-z287l were Ready1/1,
zero restarts, on separate apps nodes. An old ca6 pod was still Terminating in
the rollout snapshot; the subsequent experiment's stable two-pod preflight
passed. These snapshots do not establish node-failure recovery.

BROWSE_CONCURRENCY_OK confirmed48burst requests with eight workload clients and
12sequential reads. Each serving process reported:

| Observation | Value per pod |
| --- | --- |
| Successful test reads | 30 |
| Pool borrows delta | 32 |
| New connection attempts delta | 2 |
| Highest sampled pool size | 3 |
| Configured maximum | 4 |

These counters support reuse in the actual serving processes. The test also
verified unchanged demo responses, no increased pool error counters, no waiting
borrowers at completion, healthy HTTP endpoints and stable process/pod state.
Inventory writes were false. The extra borrows include readiness work; they are
not additional test reads. A sampled peak of three does not prove that four
was never reached between samples, nor establish a throughput or latency SLO.

## Experiment design

The first pool Job verified an isolated process. This next check sends GET
requests to the actual two serving Browse processes, using separate localhost
port forwards. It does not create reservations, edit inventory, scale workloads,
change grants, restart pods or inject slow SQL. Run without concurrent bookings
or demo-data changes; both demo events must start with100available units.

## Evidence

Browse adds /internal/database-pool, omitted from its OpenAPI schema and available
through the existing private ClusterIP. It returns a per-process random instance
identifier and an allowlist of numeric pool statistics. It does not borrow a SQL
connection or reset counters; credentials, connection strings, hostnames, SQL
payloads and customer data are not included. This is an operational endpoint;
keep it private if a public Browse route is added later.

The helper checks the subscription/context, Deployment readiness and ReplicaSet
ownership of exactly two Ready pods. Both pods may be on the same node. It opens
only its own localhost forwards on18041/18042 and cleans them up on success or
failure without killing unrelated listeners. A busy local port fails the check.

It verifies demo responses agree, snapshots each process's pool, then sends48
read requests using eight workload clients (the first eight start at a barrier).
A separate observer samples pool size every roughly200ms; those statistics
requests do not consume SQL connections. It follows with six sequential reads
per pod and checks readiness/liveness again. All workload responses must match
the baseline; a503 or any HTTP failure prevents success, with no automatic retry.

For each pod, at least31new pool borrows must be reported (30workload reads and
one readiness check). Fewer than30new physical connection attempts supports
reuse; readiness probes may add borrows. The process identifier must remain
unchanged, error/lost/bad-return counters must not increase, and no borrowers
may remain waiting afterward. The configured pool maximum is four; sampled
sizes must remain within it. Sizes are sampled, so they are not continuous
measurement of every connection or proof of a whole-cluster maximum.

Finally, original pod UIDs, readiness and container/restart state must remain
unchanged. A failed assertion reports its constant check description and stage;
raw subprocess errors, HTTP bodies and credentials are not printed.

See [Psycopg's statistics definitions](https://www.psycopg.org/psycopg3/docs/advanced/pool.html#pool-stats).
Connection attempts are not the same as simultaneous open sessions; the helper
reports them separately from sampled pool size. This is a bounded experiment
through kubectl port-forward, not a throughput benchmark, latency SLO, sustained
token-renewal test, node-failure exercise or evidence for thousands of users.

## User-run Cloud Shell steps

First build the new source, then record the returned immutable image reference:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/build-browse-image.sh
```

After reviewing IMAGE_BUILD_OK, use the existing deployment helper and demo check:

```bash
bash scripts/deploy-browse.sh &&
bash scripts/check-browse-http.sh --demo
```

After healthy rollout, run the actual serving-pod experiment:

```bash
python3 scripts/check-browse-concurrency.py
```

BROWSE_CONCURRENCY_OK must contain two per-pod counter comparisons and the pod
names. Record actual output before claiming success. A failure leaves no test
reservations to clean up; inspect the reported stage and do not reset inventory.

Offline tests cover success, connection churn preventing a pass, bad data,
pool errors, oversize snapshots, process restart detection, ownership/readiness
guards, and owned-forward cleanup with error redaction. Actual-package local
smoke tests verify the endpoint's allowlist, no SQL borrowing and OpenAPI
omission. A real PostgreSQL HTTP/counter regression test was added to CI;
Docker remains stopped and its hosted result is unchecked. The bounded live
result above passed; long-duration renewal and sustained load remain pending.
