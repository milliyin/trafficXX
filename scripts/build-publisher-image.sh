#!/usr/bin/env bash
# User-run Cloud Shell ACR build; does not deploy or change worker replicas.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
registry=trafficxxacre8708a85
server=trafficxxacre8708a85.azurecr.io
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(az acr show --name "$registry" --query loginServer -o tsv)" == "$server" ]] || {
  echo 'Registry does not match the expected trafficXX lab registry.' >&2; exit 1;
}
tag="publisher-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
az acr build --registry "$registry" --image "trafficxx:$tag" \
  --target runtime --file Dockerfile .
digest=$(az acr repository show --name "$registry" --image "trafficxx:$tag" --query digest -o tsv)
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo 'Build returned no valid image digest; do not deploy.' >&2; exit 1;
}
reference="$server/trafficxx@$digest"
printf '%s\n' "$reference" > publisher-image.txt
printf 'IMAGE_BUILD_OK\nTag: %s\nImage: %s\nSaved image reference to publisher-image.txt\n' "$tag" "$reference"
