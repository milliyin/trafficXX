# First real Browse API deployment

The database schema, browse/migrator logins and browse permission boundary have
been verified in Azure. This step packages the real FastAPI Browse API. Image
build subsequently succeeded in user-run Cloud Shell: ACR run ca1, tag
browse-20261021195843-6556, digest
sha256:22a28c8c6cc45354a327b8b7d5423cb4b169daa49a3efe320c392173dc923dbc.
The user then deployed the actual Browse API: two replicas Ready on separate
apps nodes with zero restarts at the initial rollout, followed by BROWSE_HTTP_OK
(health=alive, readiness=ready, event_count=0). Demo inventory and a controlled
[single-pod recovery drill](browse-recovery.md) subsequently passed, with
replacement observed in 12.5 seconds and demo availability intact afterward.
Sustained token renewal, continuous availability, node/zone failure and load
experiments remain unverified.

## How the database login works

The default DATABASE_AUTH_MODE=url preserves local Compose's DATABASE_URL flow.
Cloud manifests select DATABASE_AUTH_MODE=workload_identity, with explicit
PGHOST, PGDATABASE, PGUSER and optional PGPORT/PGSSLROOTCERT. The workload-identity
webhook supplies AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_FEDERATED_TOKEN_FILE.
No DATABASE_URL or saved password is supplied in Azure mode. Conflicting URL
configuration and unknown modes are rejected instead of falling back.

trafficxx.db lazily creates one explicit WorkloadIdentityCredential per process,
with a lock protecting initialization, and closes it on process exit. Every new
PostgreSQL connection calls get_token for the PostgreSQL scope and passes the
returned token as the in-memory login password. The SDK owns its in-memory token
cache/refresh; the application does not freeze a token acquired at startup.
Changed identity settings require restarting the process. Readable projected
token and CA files are required, and verify-full TLS is set explicitly; ambient
PGPASSWORD/PGSSLMODE cannot replace the token or weaken TLS. SDK failures become
a sanitized psycopg OperationalError, which the existing readiness handler
reports as 503 without raw identity error details.

That initial deployment uses SDK token caching and opens a SQL connection per
operation. A [Browse-only connection pool](database-pooling.md) subsequently
passed ACR build ca6, rollout and demo HTTP verification: bounded reuse, fresh
token parameters for replacement connections and application-lifespan cleanup.
An isolated real SQL probe passed reuse, rollback, capacity and replacement
checks. A subsequent ca7 rollout and [bounded serving-pod test](browse-concurrency.md)
verified reuse for60test reads across two pods on separate nodes. Sustained-load
performance and real renewal across an Azure token expiry boundary remain
unverified. Both ca6 pods were on the same apps node in its supplied snapshot;
pod placement is specific to each rollout and does not prove node-failure recovery.

## Build in Azure

Upload the generated trafficxx-browse-release.zip to Cloud Shell. The archive
preserves runtime source/build paths, the three helper scripts and two manifests.
It excludes .env, private variables, state, plans, provider downloads and Git
metadata. The existing Cloud Shell clone has older code until these edits are
published, so use this reviewed bundle for the build.

```bash
unzip -o ~/trafficxx-browse-release.zip -d ~/trafficxx-browse-release
cd ~/trafficxx-browse-release
bash scripts/build-browse-image.sh
```

The helper verifies the selected subscription and expected ACR, queues an ACR
quick build of the runtime target, then records a validated immutable lab image
digest in ignored browse-image.txt. No local Docker daemon, registry admin keys
or human access tokens are sent as build arguments. IMAGE_BUILD_OK means the
image was built/published, not deployed or vulnerability-free. The current
runtime uses python:3.12-slim-trixie and verifies the CA bundle exists at build
time. Top-level Python dependency pins are unchanged. This manual build does
not run the CI vulnerability gate; the updated image still needs that evidence.

## Deploy after the image build succeeds

```bash
cd ~/trafficxx-browse-release
bash scripts/deploy-browse.sh
bash scripts/check-browse-http.sh
```

The deployment helper validates subscription, AKS context and the exact registry/
repository/digest before applying two replicas and a ClusterIP service. It renders
the Deployment with kubectl set image --local; do not directly apply the source
Deployment's build-required placeholder. Pods use browse's existing service
account and workload-identity label, run as non-root on apps nodes, have bounded
resources/read-only root filesystems and temporary scratch space, and expose
health/startup/readiness probes. Readiness queries the real database; liveness
does not depend on database reachability. Scheduling prefers spreading the two
replicas across nodes but does not require separate availability zones.

The HTTP helper starts its own localhost-only port forward, waits for it to bind
and exits if forwarding fails. It checks /healthz, /readyz and /events, reporting
BROWSE_HTTP_OK with an event count. It cleans up only its own process/log. This
checks one pod selected through the service, not load balancing or throughput.
Both replicas' Kubernetes readiness is checked by the rollout helper.

The initial empty-database check returned event_count=0. The user subsequently
ran the [controlled demo seed](demo-inventory.md): event_count=2 and both demo
availability endpoints returned 100. Unknown event IDs still return 404.
There is no external LoadBalancer, public ingress or real customer authentication
in this step. Private API reachability here uses the authenticated kubectl
connection from Cloud Shell; TLS remains required on the database connection.

For deployment failures inspect:

```bash
kubectl get pods -n trafficxx -l app.kubernetes.io/name=trafficxx-browse -o wide
kubectl describe deployment browse -n trafficxx
kubectl logs -n trafficxx deployment/browse -c browse --tail=80
```

## Verification so far

31 offline standard-library unittest methods pass, including local URL
compatibility, explicit cloud credentials, per-connection token lookup, reused
credential construction under concurrent calls, rejected unsafe/missing config,
TLS/password environment overrides and sanitized SDK failure. Six simulated
build/deployment helper scenarios pass (success, wrong subscription, build
failure, invalid digest, wrong context, invalid image). These use stubbed tools,
not Azure. Bash syntax, manifest structure and upload-bundle contents are checked
locally. The runtime image build, two-replica deployment and actual API HTTP
checks subsequently succeeded in Azure. Deployment browse-588f8c4dbf had two
Ready pods, one on each apps node, with zero restarts at the supplied observation.
The HTTP check confirmed health/readiness and an empty event list. This verifies
initial API-to-database integration, not expiry-boundary token renewal, load
balancing, performance or failover behavior.

The workflow now includes the offline unittest command; no hosted run of this
change is claimed. Existing PostgreSQL/broker integration tests and the new
runtime-image vulnerability scan have not been rerun locally because Docker
remains stopped. Successful deployment does not replace those CI/security checks.

References: [WorkloadIdentityCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.workloadidentitycredential?view=azure-python),
[SDK token caching](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/TOKEN_CACHING.md),
[ACR quick build CLI](https://learn.microsoft.com/en-us/cli/azure/acr?view=azure-cli-latest#az-acr-build).
