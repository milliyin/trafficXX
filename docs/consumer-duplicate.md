# Duplicate delivery on Azure

We will resend the already processed outbox row 3, keeping its durable message ID
and payload. This is a controlled duplicate, not a new booking. The current queue
has broker duplicate detection disabled, so the consumer inbox must suppress the
repeated effect. Broker duplicate processing is not yet verified in Azure.

## Step 1: capture the before state

Keep booking requests and other experiments idle throughout this experiment.
The publisher and consumer stay running. The read-only migrator observer records
counts and SHA-256 fingerprints of all inbox, projection and outbox rows, sorted
by their primary keys. All columns are included, including processed_at and
updated_at, so unchanged counts cannot hide a timestamp or field update. Raw
message contents are not printed. This bounded lab has only 12 historical events;
aggregating entire tables is a lab diagnostic, not a production monitoring query.

The observer requires outbox row 3 to be published and already present in the
inbox. It uses existing migrator permissions, certificate-verified TLS and a
REPEATABLE READ, READ ONLY transaction; it performs no sends or database writes.

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap duplicate-baseline-check-files -n trafficxx \
  --from-file=observe-duplicate-baseline.py=scripts/observe-duplicate-baseline.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/duplicate-baseline.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/duplicate-baseline-check --timeout=180s
kubectl logs -n trafficxx job/duplicate-baseline-check -c check | tee ~/trafficxx-duplicate-baseline.json
```

Expected DUPLICATE_BASELINE_OK, with outbox_id=3, its original message ID, table
counts and fingerprints. Save the JSON for the after comparison. The default
lab checkpoint has 12 inbox rows, six projections and 12 outbox rows. Actual
baseline output is pending. No message is resent at this first step.

## Following steps

After inspecting the baseline, send this same event once using the existing
publisher identity and queue Sender permission, without changing outbox metadata.
Observe a new duplicate delivery log from the consumer, then take a new read-only
snapshot and require all three table fingerprints/counts to match the baseline.
Obtain native queue counts to confirm the duplicate was settled with no dead
letters. Those sender/after-check steps will be prepared after the baseline is
captured. A failed/ambiguous send must not silently generate a different ID or
be treated as proof of consumer deduplication.

Six offline methods cover read-only statements, raw-content redaction, stable
fingerprints, timestamp-only changes, identity/TLS/admin rejection, missing or
unprocessed target and empty-source rejection. The existing runtime image is
reused. Real duplicate delivery and unchanged-state evidence remain pending.
