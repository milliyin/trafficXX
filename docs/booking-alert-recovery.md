# Controlled Booking target-warning drill

Status: local validation and live warning/restoration evidence passed; the
user's final recovery JSON was truncated, as recorded below. This follows
the verified ten-panel Booking dashboard and healthy live Booking/Browse rules.

## Live checkpoint

Run 0d3588713206 targeted Deployment UID
e587fa09-243f-4a4f-812f-ee11fad7f6f2. The user supplied
BOOKING_TARGET_WARNING_OBSERVED: TrafficxxBookingTargetsUnavailable firing,
one healthy scrape target, 120-second configured hold and a verified native
firing series, followed by a 30-second observation window. A browser screenshot
also showed the preceding pending stage (pending 1, firing 0) and its history.

BOOKING_REPLICAS_RESTORED confirmed desired and ready replicas restored. The
visible BOOKING_ALERT_RECOVERY_OK JSON showed two targets/five healthy inactive
rules before and after, with two ready replicas after recovery. That final line
was cut off inside the surviving pod name. The complete pod name and later
fields, including the explicit stored-firing-history verdict, have not yet been
supplied; do not invent their values or repeat the drill to obtain them.
This checkpoint establishes actual warning appearance and replica/rule recovery,
not Booking HTTP continuity or external notification delivery.

## What it tests

The Cloud Shell helper reduces the existing Booking Deployment from two replicas
to one, waits for one stable original pod, and verifies
TrafficxxBookingTargetsUnavailable fires after its configured two-minute hold.
It requires current healthy rule evaluation and a native firing ALERTS series,
with exactly one healthy Booking scrape target. Other Booking rules must remain
inactive. The surviving pod's UID and container restart count must stay fixed.

It leaves the warning visible for 30 seconds by default, restores two replicas,
waits for both to be ready and for five healthy inactive Booking rules/fresh
stored samples to return. Existing Browse rules must also remain healthy.
A range query must find a stored firing sample within this run's time window;
older warning history alone cannot satisfy the check.

This is a target-availability warning experiment. It generates no Booking API
requests and makes no inventory writes. It does not prove uninterrupted HTTP
service, reservation correctness under load, recovery latency SLOs, other rule
conditions or external email/phone/chat delivery. It changes no image, SQL
permission, Grafana/Prometheus deployment, worker or token-renewal Job.

## Run in browser Cloud Shell

Open trafficXX | Booking database pool in Grafana, choose Last 15 minutes and
enable 15-second refresh. Keep the dashboard open while running:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/check-booking-alert-recovery.py
```

Allow roughly four to six minutes in a healthy lab. The target count should
drop to one, the target warning become pending and then firing, and both counts
return to zero after recovery. History should retain the warning interval.
The script prints BOOKING_TARGET_WARNING_OBSERVED, then
BOOKING_REPLICAS_RESTORED and finally BOOKING_ALERT_RECOVERY_OK only when all
evidence gates pass. Do not run concurrent deployment/scaling/load experiments.
An optional --observe-seconds value from 0 to 60 changes only the viewing window.

The helper guards the subscription/context, stable Deployment template and
ownership, no existing drill marker, and no HPA targeting Booking. Atomic JSON
patches test Deployment UID/resourceVersion and the initial replica count.
A unique trafficxx.io/booking-alert-drill annotation marks this run's change.
Restoration acts only on that same Deployment and marker, with expected desired
count one or two; it will not overwrite a different drill's marker or unexpected
replica count. Other existing annotations are preserved. The marker is removed
after two ready replicas return. A localhost-only Prometheus forward is stopped
on exit.

## If interrupted

Keep Cloud Shell open. Errors or Ctrl+C enter a finally block that attempts to
restore two replicas and readiness. A hard-killed process or disconnected shell
cannot guarantee execution of cleanup. Do not repeat a failed drill to diagnose.
First inspect:

```bash
kubectl get deployment booking -n trafficxx
kubectl get pods -n trafficxx -l app.kubernetes.io/name=trafficxx-booking
kubectl get deployment booking -n trafficxx \
  -o jsonpath='{.metadata.uid}{"\n"}{.metadata.annotations.trafficxx\.io/booking-alert-drill}{"\n"}'
```

If the same Deployment/run is left at one replica, restore the intended two:

```bash
kubectl scale deployment/booking -n trafficxx --replicas=2 &&
kubectl rollout status deployment/booking -n trafficxx --timeout=180s
```

After verifying two ready replicas and that the annotation belongs to this
interrupted run, remove only that drill marker:

```bash
kubectl annotate deployment/booking -n trafficxx trafficxx.io/booking-alert-drill-
bash scripts/check-prometheus-http.sh --booking-alerts
```

Never recreate the unrelated browse-token-renewal-check Job. A failure report
does not establish that restoration succeeded; inspect desired and ready counts.

## Local validation

All 422 offline methods passed, including twelve new methods covering ordered
recovery evidence, Ctrl+C/failure cleanup, lost patch responses, failed restoration,
existing markers/HPA, UID/resourceVersion and annotation preservation, foreign
ownership/unexpected counts, bounded retries, current-run history, native firing
evidence and stable pod ownership/template checks. Native kubectl applied the
actual scale-down/restoration/marker-removal patches locally using an empty
isolated kubeconfig. No Azure cluster call was made. Docker stayed stopped;
hosted CI remains unverified. The live evidence supplied separately by the user
is recorded above, with its truncation limit.
