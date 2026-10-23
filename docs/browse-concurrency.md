# Bounded concurrency check of the serving Browse pods

Status: helper and internal statistics endpoint implemented locally; a new Browse
image build, rollout and live experiment remain pending. The deployed ca6 image
has pooling but does not have the statistics endpoint required for this check.

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
Docker remains stopped and its hosted result is unchecked. Live results remain
pending.
