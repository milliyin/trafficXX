# Duplicate delivery on Azure

We will resend the already processed outbox row 3, keeping its durable message ID
and payload. This is a controlled duplicate, not a new booking. The current queue
has broker duplicate detection disabled, so the consumer inbox must suppress the
repeated effect. This controlled duplicate-delivery experiment passed in Azure:
one acknowledged resend, a new consumer duplicate log, unchanged stored state
and native queue counts all zero.

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
baseline passed: message 151af373-f74d-4bc4-8c14-e3b6cf13aa51; counts
12/6/12; inbox SHA256 12390ff30702acd48b0a33cd8972acf0545522e0e4f6eb0e40cd26f6c99e0551,
projection SHA256 b3645941e0d9a4337b7b98aacba2588767366e6c9622f5a37519d4439a1e92fe,
outbox SHA256 3d8a0ec47fdb5e9050a090ce9b827b25a2c3a96cf5ffe0e55577b38fa5b25368.
The user-run read-only migrator observer used TLSv1.3. No message was resent
by that first step.

## Step 2: resend the original event once

```bash
cd ~/trafficxx
git pull --ff-only origin main
kubectl create configmap publisher-send-duplicate-files -n trafficxx \
  --from-file=send-duplicate-azure.py=scripts/send-duplicate-azure.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/jobs/publisher-send-duplicate.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/publisher-send-duplicate --timeout=180s
kubectl logs -n trafficxx job/publisher-send-duplicate -c publish
kubectl logs -n trafficxx deployment/consumer --since=5m --tail=50
```

Expected DUPLICATE_SEND_OK, send_calls=1, send_acknowledged=true and
outbox_edited=false. The sender reads published row3 as publisher with verified
TLS and reconstructs its original envelope through the runtime helper. It uses
the runtime workload-only broker credential/send path, keeping the exact ID,
payload and occurrence time. It does not call publish_one, mark_published or
heartbeat, and makes no SQL writes. The Job has backoffLimit0; SDK transport
retries are disabled. An ambiguous send failure is not proof of duplicate
handling; inspect the stable message ID/logs before retrying.

Require a NEW event_delivery outcome=duplicate log for
151af373-f74d-4bc4-8c14-e3b6cf13aa51 after the send_started_at timestamp printed
by the sender. An old log or send acknowledgement alone does not prove this
resend was processed. Allow a few seconds and reread logs if needed. Actual
Azure resend and duplicate outcome passed: send_started_at was
2026-10-22T22:03:25.376282Z, one send call was acknowledged with outbox_edited=false,
and the consumer logged outcome=duplicate for the same message at
2026-10-22 22:03:25,727. This correlates the new duplicate outcome to the resend.

## Step 3: compare the after state and native queue counts

Once the new duplicate log is observed:

```bash
kubectl apply -f kubernetes/checks/duplicate-after.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/duplicate-after-check --timeout=180s
kubectl logs -n trafficxx job/duplicate-after-check -c check \
  | tee ~/trafficxx-duplicate-after.json
python3 scripts/compare-duplicate-state.py \
  ~/trafficxx-duplicate-baseline.json ~/trafficxx-duplicate-after.json
az servicebus queue show \
  --resource-group trafficxx-lab-rg \
  --namespace-name trafficxx-sb-e8708a85 \
  --name reservation-events \
  --query '{active:countDetails.activeMessageCount,dead_letters:countDetails.deadLetterMessageCount,total:messageCount}' \
  --output json
```

The after Job reuses the same baseline script/ConfigMap under a distinct Job
name. Expected DUPLICATE_STATE_UNCHANGED with all table counts/fingerprints
matching, including inbox processed_at and projection updated_at. The comparator
validates target/identity/read-only markers and hash/count shape. Native queue
counts must separately show zero active/dead-letter/total messages. These checks
passed: the user supplied DUPLICATE_STATE_UNCHANGED with
counts_and_all_columns_unchanged=true for consumer_inbox, outbox and
reservation_updates, followed by native active=0, dead_letters=0, total=0. Keep the original
baseline file; never overwrite it with the after output.

Six baseline, five sender and four comparator offline methods cover read-only
SQL, stable message selection, one send attempt, ambiguous send/close errors,
credential constraints, timestamp-sensitive hashing, changed counts/hashes and
invalid diagnostic inputs. All 209 offline methods pass. No role changes or
runtime image rebuild are needed. This deliberate resend tests the successful
deduplication path; it does not simulate a crash or prove exactly-once delivery.


## What this proved and what follows

For this deliberately repeated valid message, the consumer inbox suppressed the
second effect. Counts and fingerprints for all inbox, projection and outbox
columns (including timestamps) were unchanged, and the resend was settled.
This is application deduplication with broker duplicate detection disabled.
It does not establish exactly-once transport or simulate a process crash.

Next, test a controlled consumer outage: pause the consumer, create a bounded
amount of valid work, observe the queue accumulate it, then restore the consumer
and verify inbox/projection agreement plus queue drain. Prepare recovery commands
before pausing and retain the original replica count. This Azure outage experiment
has not yet been run; poison-message/dead-letter recovery follows separately.
