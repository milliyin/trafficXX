# Single Browse pod recovery drill

The real Browse API has two Ready replicas and both demo events report 100
available units. This drill checks replacement of one normally deleted pod.
It does not delete a node, Deployment, service, database or stored inventory.
The user ran it successfully: BROWSE_POD_RECOVERY_OK reported two Ready replicas
and 12.5 observed seconds. Deleted pod browse-588f8c4dbf-frjfx was replaced by
browse-588f8c4dbf-x8lvc; browse-588f8c4dbf-xwvvl survived. The subsequent
BROWSE_DEMO_HTTP_OK check reported healthy/ready, two events and 100 available
for each. Inventory remained intact across the pod replacement.

Upload the generated two-file trafficxx-browse-recovery.zip, then run in
browser Cloud Shell:

```bash
unzip -o ~/trafficxx-browse-recovery.zip -d ~/trafficxx-browse-recovery
cd ~/trafficxx-browse-recovery
python3 check-browse-recovery.py && bash check-browse-http.sh --demo
```

The helper verifies the selected subscription/context, an existing stable
two-replica Browse Deployment, the expected service account and an immutable
image from the lab registry. It looks up ReplicaSets owned by the Deployment
and verifies two healthy pods with matching controller ownership. It refuses
an unhealthy or scaled baseline instead of changing replica counts.

It chooses exactly one owned pod by name and deletes it normally with
--wait=false; there is no forced deletion or zero-second grace period. The
desired replica count stays two. It records the original pod UIDs and polls
for the deleted UID to disappear, the other original pod to remain Ready at
observations, and exactly one new owned UID to become Ready. Final Deployment
readiness/status must converge without a concurrent generation change.

Expected first output is BROWSE_POD_RECOVERY_OK with old/new/surviving names,
ready_replicas=2 and observed_seconds, followed by BROWSE_DEMO_HTTP_OK. The latter
checks real API health, readiness and both demo availability endpoints. This
uses one service-selected pod; replacement readiness separately checks the
replacement's real database readiness probe.

The helper waits up to 180 seconds for replacement, with bounded individual
kubectl calls. It aborts if the surviving pod is not Ready at a poll or if the
Deployment changes. Failures after deletion do not undo that deletion or force
scaling; the controller still has the existing desired count of two. Inspect
the named Deployment/pods rather than repeating deletion blindly:

```bash
kubectl get pods -n trafficxx -l app.kubernetes.io/name=trafficxx-browse -o wide
kubectl describe deployment browse -n trafficxx
```

## What this demonstrates

The Deployment's ReplicaSet reconciles desired replica count after loss of a
pod. The replacement is a new pod with a different UID, not the old pod being
resurrected. It may schedule on the same node; this is not a node/zone failure
or disaster-recovery test. Inventory remains in PostgreSQL independently of
pod lifetime. ReplicaSet behavior is documented in the
[Kubernetes ReplicaSet guide](https://kubernetes.io/docs/concepts/workloads/controllers/replicaset/).

The duration is one observed lab replacement time, not an SLA or HTTP downtime
measurement. The script polls readiness and checks HTTP afterward; it does not
continuously send requests during disruption, prove zero downtime/load balancing,
or exercise token renewal across expiry. Those require separate experiments.

## Local checks

The offline suite has 53 passing unittest methods. Nine new recovery methods
cover correct ownership/single deletion, wrong subscription/context, unhealthy
or changed baseline, foreign pods, survivor failure, stale UIDs, replacement
readiness/ownership, and concurrent Deployment changes. Driver responses and
time are simulated; no Azure commands or real pod deletions run locally.
The ZIP contains only this helper and the existing demo HTTP helper, without
credentials, state, manifests or an application rebuild. The initial live
single-pod drill and post-recovery HTTP check subsequently passed as recorded
above; other failure modes and continuous availability remain untested.
