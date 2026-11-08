#!/usr/bin/env bash
# User-run Azure Cloud Shell build; no local Docker daemon or Azure keys needed.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
check_interval=0
if [[ $# -eq 1 && "$1" == '--recent-checks' ]]; then
  check_interval=1
elif [[ $# -ne 0 ]]; then
  echo 'Usage: build-browse-image.sh [--recent-checks]' >&2
  exit 1
fi

expected_subscription=35aaf788-f4cf-40c3-9ea5-bc8fe8708a85
registry=trafficxxacre8708a85
server=trafficxxacre8708a85.azurecr.io
if [[ "$(az account show --query id -o tsv)" != "$expected_subscription" ]]; then
  echo 'Select the trafficXX sponsorship subscription before building.' >&2
  exit 1
fi
if [[ "$(az acr show --name "$registry" --query loginServer -o tsv)" != "$server" ]]; then
  echo 'Registry does not match the expected trafficXX lab registry.' >&2
  exit 1
fi
tag="browse-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
az acr build --registry "$registry" --image "trafficxx:$tag" \
  --build-arg "BROWSE_CHECK_INTERVAL_SECONDS=$check_interval" \
  --target runtime --file Dockerfile .
digest=$(az acr repository show --name "$registry" --image "trafficxx:$tag" --query digest -o tsv)
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo 'Build returned no valid image digest; no deployment should proceed.' >&2
  exit 1
fi
reference="$server/trafficxx@$digest"
printf '%s\n' "$reference" > browse-image.txt
printf 'IMAGE_BUILD_OK\nTag: %s\nImage: %s\nSaved image reference to browse-image.txt\n' "$tag" "$reference"
