# Invalid-message and dead-letter experiment

The previous consumer-outage test passed with 14 inbox/outbox rows and seven
latest projections. This new experiment will send ONE identifiable invalid
message and check schema rejection, no business effects, targeted DLQ cleanup
and subsequent valid-message progress. Actual Azure results are pending.

Diagnostic ID: bd7fe2e5-7631-4366-9594-69b82ba8d07a. Its JSON declares unsupported
schema_version=999 and is intentionally missing reservation-event fields.
Expected reason: INVALID_EVENT_SCHEMA. This message is sent directly by the
publisher diagnostic and has no outbox row. The existing consumer should reject
it before process_event/inbox/projection writes. It will intentionally add a
historical observed_dead_letters row; that audit record will not be deleted.

## First capture a fresh business-state baseline

Keep other booking experiments idle and leave all workers running. Require native
queue active=0, dead_letters=0, total=0 before proceeding. The baseline checks all
inbox/projection/outbox columns including timestamps, without SQL writes or sends.
It reuses the installed duplicate-baseline-check-files ConfigMap.

```bash
cd ~/trafficxx
git pull --ff-only origin main
az servicebus queue show \
  --resource-group trafficxx-lab-rg --namespace-name trafficxx-sb-e8708a85 \
  --name reservation-events \
  --query '{active:countDetails.activeMessageCount,dead_letters:countDetails.deadLetterMessageCount,total:messageCount}' \
  --output json
kubectl apply -f kubernetes/checks/poison-baseline.yaml
kubectl wait -n trafficxx --for=condition=complete \
  job/poison-baseline-check --timeout=180s
kubectl logs -n trafficxx job/poison-baseline-check -c check \
  | tee ~/trafficxx-poison-baseline.json
```

Expected DUPLICATE_BASELINE_OK (the reused read-only observer's name), now with
14 inbox/outbox rows and seven projection rows. Preserve this separate file.
Inspect the baseline before sending; no invalid message is sent by this stage.

## Prepared send and peek steps

After inspecting the baseline and zero native counts:

```bash
kubectl create configmap poison-diagnostic-files -n trafficxx \
  --from-file=poison-message-azure.py=scripts/poison-message-azure.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/jobs/poison-send.yaml
kubectl wait -n trafficxx --for=condition=complete job/poison-send --timeout=180s
kubectl logs -n trafficxx job/poison-send -c check
kubectl logs -n trafficxx deployment/consumer --since=5m --tail=50
kubectl apply -f kubernetes/jobs/poison-peek.yaml
kubectl wait -n trafficxx --for=condition=complete job/poison-peek --timeout=180s
kubectl logs -n trafficxx job/poison-peek -c check
```

Expected POISON_SEND_OK (one acknowledged send), new consumer outcome=dead_letter
for the diagnostic ID, then POISON_DLQ_PEEK_OK with INVALID_EVENT_SCHEMA and
settled=false. Peek uses consumer identity and prefetch0 without receive/settlement.
It requires exactly this one matching DLQ message, browsing up to two to detect
unexpected neighbors. Body/ID/reason must match. Native counts should become
active=0, dead_letters=1, total=1. No rows/bodies/tokens are printed by the tools.

All actions use explicit WorkloadIdentityCredential, the expected service account,
CA-verified TLS, no connection strings, zero SDK retries and backoffLimit0. Sending
uses publisher; peek/cleanup use consumer. No SQL access or new grants are needed.
An ambiguous send must be inspected by this same ID before retrying; do not send
more copies or rerun the diagnostic while the previous message is present.

## Prepared after-state and cleanup steps

```bash
kubectl apply -f kubernetes/checks/poison-after.yaml
kubectl wait -n trafficxx --for=condition=complete job/poison-after-check --timeout=180s
kubectl logs -n trafficxx job/poison-after-check -c check \
  | tee ~/trafficxx-poison-after.json
python3 scripts/compare-duplicate-state.py \
  ~/trafficxx-poison-baseline.json ~/trafficxx-poison-after.json
```

The existing comparator reports DUPLICATE_STATE_UNCHANGED for matching fingerprints;
its label is reused, but here it proves no inbox/outbox/projection field changed
because of this invalid message. Historical dead-letter tracking is excluded from
those business-table hashes and intentionally changes. After inspecting the result,
complete ONLY the diagnostic dead letter:

```bash
kubectl apply -f kubernetes/jobs/poison-cleanup.yaml
kubectl wait -n trafficxx --for=condition=complete job/poison-cleanup --timeout=180s
kubectl logs -n trafficxx job/poison-cleanup -c check
```

Expected POISON_CLEANUP_OK with completion_acknowledged=true. Cleanup first peeks,
requires exactly one matching diagnostic message, then receives at most one in
default PEEK_LOCK mode with prefetch0. Before completing, it rechecks ID/body/reason
and sequence against the peeked message; an unexpected received message is
abandoned and the check fails. No bulk purge or deleting audit rows occurs.
Recheck native counts for all zero. An ambiguous completion needs inspection,
not blind replay. This removes only a disposable diagnostic after its evidence
has been recorded. See the [SDK receive/settlement semantics](https://learn.microsoft.com/en-us/python/api/azure-servicebus/azure.servicebus.servicebusreceiver?view=azure-python).

Finally run the existing booking flow once and a fresh read-only consumer-drain
observer to verify valid work still processes. That final evidence and the SQL
observed-dead-letter audit row will be checked separately after the staged results.
Actual invalid send, dead-letter reason, unchanged fingerprints, cleanup and valid
follow-up progress are all pending.

Eight added offline methods cover one send, non-destructive peek, exact cleanup,
wrong/extra messages, unexpected receive abandonment, bounded empty peek, ambiguous
completion and identity/action preflight. All 225 offline methods pass. Existing
application image and runtime code remain unchanged.
