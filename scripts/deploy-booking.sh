#!/usr/bin/env bash
# User-run Cloud Shell deployment of the existing, digest-pinned runtime image.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
[[ -r booking-image.txt ]] || { echo 'Run scripts/build-booking-image.sh first.' >&2; exit 1; }
reference=$(cat booking-image.txt)
[[ "$reference" =~ ^trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}$ ]] || {
  echo 'Invalid lab image reference; refusing deployment.' >&2; exit 1;
}
# `set image --local` prints nothing for an unchanged image. JSON patch emits
# the complete object even when the reviewed digest is already in the file.
patch='[{"op":"test","path":"/spec/template/spec/containers/0/name","value":"booking"},{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"'"$reference"'"}]'
rendered=$(kubectl patch --local -f kubernetes/apps/booking-deployment.yaml \
  --type=json --patch "$patch" -o yaml)
[[ -n "$rendered" ]] || { echo 'Booking manifest rendering returned no object.' >&2; exit 1; }
printf '%s\n' "$rendered" | kubectl apply -f -
kubectl apply -f kubernetes/apps/booking-service.yaml
kubectl rollout status deployment/booking -n trafficxx --timeout=600s
kubectl get pods -n trafficxx -l app.kubernetes.io/name=trafficxx-booking -o wide
echo 'BOOKING_DEPLOYED; next run scripts/check-booking-http.sh.'
