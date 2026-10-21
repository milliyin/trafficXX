#!/usr/bin/env bash
# User-run Cloud Shell deployment using the digest from the reviewed build.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
[[ -r browse-image.txt ]] || { echo 'Run the image build first.' >&2; exit 1; }
reference=$(cat browse-image.txt)
[[ "$reference" =~ ^trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}$ ]] || {
  echo 'Invalid lab image reference; refusing deployment.' >&2; exit 1;
}
kubectl set image --local -f kubernetes/apps/browse-deployment.yaml \
  "browse=$reference" -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/apps/browse-service.yaml
kubectl rollout status deployment/browse -n trafficxx --timeout=600s
kubectl get pods -n trafficxx -l app.kubernetes.io/name=trafficxx-browse -o wide
echo 'BROWSE_DEPLOYED; next run scripts/check-browse-http.sh to verify HTTP.'
