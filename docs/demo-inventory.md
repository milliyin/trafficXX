# Controlled Azure demo inventory

The real Browse API is deployed and its initial HTTP check returned an empty
event list. This step adds the same two demo events used by local development:
demo-general with 100 general-admission units, and demo-seated with seats A1-A100.
The user verified both in Azure: DEMO_SEED_OK created both events with 100 units
each, followed by BROWSE_DEMO_HTTP_OK with two events and 100 available for each.
This confirms committed seed data is visible through the real Browse API.

Upload the generated three-file trafficxx-demo-inventory.zip to Cloud Shell:

```bash
unzip -o ~/trafficxx-demo-inventory.zip -d ~/trafficxx-demo-inventory
cd ~/trafficxx-demo-inventory

kubectl create configmap postgres-demo-seed-files -n trafficxx \
  --from-file=bootstrap-azure-schema.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f postgres-demo-seed.yaml

kubectl wait -n trafficxx --for=condition=complete \
  job/postgres-demo-seed --timeout=300s &&
kubectl logs -n trafficxx job/postgres-demo-seed -c seed &&
bash check-browse-http.sh --demo
```

The seed Job runs as migrator with its own SDK workload credential, verified TLS
and the existing non-admin identity checks. The --seed-demo action is mutually
exclusive with --grant-browse and does not run schema migrations. The two
events/units are added in one transaction and success is reported after commit.
It takes migration advisory lock 7432002 and locks existing demo event rows,
matching the application's event-lock convention. It does not seed unrelated
events or run the local seed file against existing cloud data.

Existing matching events must have exactly units 1-100 with the expected labels;
matching rows are skipped with no updates. Changed event definitions, extra or
missing units and changed labels fail instead of silently repairing inventory.
Existing reservation_id assignments are deliberately untouched. One conflicting
event aborts the transaction, including any new rows added earlier in the run.
There is no DELETE/TRUNCATE/reset or reservation mutation. The last Azure
inventory observation was empty, so the first run should create both events.

Expected seed output: DEMO_SEED_OK, created=[demo-general,demo-seated], skipped=[],
events_verified=2, units_per_event=100. A later matching retry reports both IDs
in skipped. Delete only the named seed Job before rerunning it while present.
As with the other diagnostic Jobs, inputs contain only public code/settings;
dependency-index egress is required and completed-Job cleanup uses a ten-minute
TTL. The input ConfigMap remains for inspection. The existing API image is not
rebuilt or redeployed for this database-only addition.

The updated HTTP helper's --demo mode checks both IDs/types in /events, calls
both availability endpoints and validates general versus labelled-seat output.
It requires available counts in 0-100 and unique valid A1-A100 seat labels whose
count matches available. On fresh inventory, expect BROWSE_DEMO_HTTP_OK with
event_count=2 and demo_availability showing 100 for each event. Valid lower
availability after later bookings is also accepted; the probe never replenishes
or resets inventory. It checks one service-selected API pod through its own
temporary localhost port forward and cleans that process up afterward.

## Local checks

The offline suite now has 44 passing unittest methods. Eight new seed checks
cover fresh data, repeat without writes, preserved reservation assignments,
conflicting definitions, missing/extra units, changed labels and unrelated data.
Five new HTTP response checks cover full/partially reserved demos, malformed
labels/counts, missing events and compatibility with the original smoke mode.
These simulate driver/HTTP calls, not PostgreSQL or the live service. Bash syntax,
Job structure, Python syntax and the upload-bundle inputs are checked locally.
Actual initial cloud seed/availability checks subsequently passed. A live seed
retry with existing reservations and a deliberately conflicting inventory have
not been exercised; the corresponding safety checks above remain offline tests.
