# Invalid-message and dead-letter experiment

The previous consumer-outage test passed with 14 inbox/outbox rows and seven
latest projections. The invalid-message rejection, unchanged business state and
targeted DLQ cleanup stages have now passed. Valid-message progress and the retained SQL audit were subsequently verified,
and final native queue counts were all zero.

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
The user supplied the fresh baseline with 14 inbox/outbox rows, seven projections
and TLSv1.3; native counts were zero. This stage made no sends.

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
active=0, dead_letters=1, total=1. The user supplied POISON_SEND_OK with one acknowledged send, a new consumer
dead_letter outcome at2026-10-22 22:13:42.359 and POISON_DLQ_PEEK_OK for the exact
diagnostic ID/reason, sequence_number=16, settled=false. No bodies/tokens are printed.

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

The user supplied DUPLICATE_STATE_UNCHANGED with all business-table counts and
full-row hashes unchanged (14 inbox/outbox, seven projections), plus native
active=0, dead_letters=1, total=1 before cleanup. Here the reused label proves no inbox/outbox/projection field changed
because of this invalid message. Historical dead-letter tracking is excluded from
those business-table hashes and intentionally changes. After inspecting the result,
complete ONLY the diagnostic dead letter:

```bash
kubectl apply -f kubernetes/jobs/poison-cleanup.yaml
kubectl wait -n trafficxx --for=condition=complete job/poison-cleanup --timeout=180s
kubectl logs -n trafficxx job/poison-cleanup -c check
```

Verified POISON_CLEANUP_OK: exact diagnostic ID/reason/sequence16,
completion_attempted=true, completion_acknowledged=true, no sends. The user
subsequently supplied native counts active=0, dead_letters=0, total=0. Cleanup first peeks,
requires exactly one matching diagnostic message, then receives at most one in
default PEEK_LOCK mode with prefetch0. Before completing, it rechecks ID/body/reason
and sequence against the peeked message; an unexpected received message is
abandoned and the check fails. No bulk purge or deleting audit rows occurs.
Recheck native counts for all zero. An ambiguous completion needs inspection,
not blind replay. This removes only a disposable diagnostic after its evidence
has been recorded. See the [SDK receive/settlement semantics](https://learn.microsoft.com/en-us/python/api/azure-servicebus/azure.servicebus.servicebusreceiver?view=azure-python).

## Verify a normal booking still processes and its audit remains

Run the existing booking flow ONCE, then the new read-only recovery observer:

```bash
cd ~/trafficxx
git pull --ff-only origin main
bash scripts/check-booking-flow.sh
kubectl create configmap poison-recovery-check-files -n trafficxx \
  --from-file=check-poison-recovery.py=scripts/check-poison-recovery.py \
  --from-file=check-consumer-drain.py=scripts/check-consumer-drain.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/poison-recovery.yaml
kubectl wait -n trafficxx --for=condition=complete job/poison-recovery-check --timeout=180s
kubectl logs -n trafficxx job/poison-recovery-check -c check
```

Expected BOOKING_FLOW_OK with final_status=CANCELLED, then POISON_RECOVERY_OK.
The observer reuses the full inbox/payload/projection/heartbeat drain checker,
which polls up to120seconds. From this controlled14event checkpoint, it requires
16 total/inbox rows and eight latest projections after exactly one follow-up
flow. If other tests added work or you reran the flow, inspect the resulting
history instead of rerunning again. It then reads the retained diagnostic audit
row and requires queue reservation-events, sequence16, reasonINVALID_EVENT_SCHEMA,
with no diagnostic inbox row. All SQL observations are read-only under migrator
identity/verify-full TLS; no audit rows are removed. Native queue counts must be
checked separately again for all zero after the valid flow.

Verified POISON_RECOVERY_OK from the user: 16 total outbox rows, zero pending,
16 matching inbox records, eight matching latest projections, no missing/payload/
projection/orphan mismatches and zero failure records. The diagnostic has zero
inbox rows; its one retained audit record has sequence16 and INVALID_EVENT_SCHEMA.
Consumer heartbeat age was3.842056seconds, present and error-free. The observer
was read-only migrator over TLSv1.3. Native counts after the valid follow-up were
active=0, dead_letters=0, total=0. A separate BOOKING_FLOW_OK transcript was not
supplied; the observer verified the expected additional two events and projection.
The historical observed_dead_letter_count=1 is a retained SQL audit count, not a
live broker dead-letter count. This completes the controlled experiment. Four new offline methods cover read-only success, missing/wrong/
duplicate audit, absent new valid work and forbidden diagnostic inbox presence.

Eight added offline methods cover one send, non-destructive peek, exact cleanup,
wrong/extra messages, unexpected receive abandonment, bounded empty peek, ambiguous
completion and identity/action preflight. All 229 offline methods pass. Existing
application image and runtime code remain unchanged.


The diagnostic schema rejection, unchanged business-table fingerprints, targeted
cleanup, retained audit and subsequent valid processing are now verified. No
claim is made about all poison-message types, retry exhaustion, crash boundaries,
sustained throughput or token renewal across an actual expiry boundary. Next
priorities are operational backlog/heartbeat/dead-letter visibility and testing
recovery under a failure during processing.
