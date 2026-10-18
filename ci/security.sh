#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=ci/tools.env
source ci/tools.env

revision="${GITHUB_SHA:-local}"
if [[ ! "$revision" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
  echo 'Invalid image revision' >&2
  exit 2
fi
reports="$PWD/test-results/ci/security"
mkdir -p "$reports"
cache_volume="trafficxx-ci-trivy-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
if [[ ! "$cache_volume" =~ ^trafficxx-ci-trivy-[a-zA-Z0-9_-]+$ ]]; then
  echo 'Invalid scanner cache volume' >&2
  exit 2
fi
trap 'docker volume rm "$cache_volume" >/dev/null 2>&1 || true' EXIT

docker build --pull --target runtime --tag "trafficxx-ci:$revision" \
  --label "org.opencontainers.image.revision=$revision" .
docker image inspect "trafficxx-ci:$revision" > "$reports/image-inspect.json"
docker save --output "$reports/runtime-image.tar" "trafficxx-ci:$revision"
scanner=(docker run --rm --mount "type=bind,source=$reports,target=/reports" \
  --mount "type=volume,source=$cache_volume,target=/root/.cache/trivy" "$TRIVY_IMAGE" \
  image --input /reports/runtime-image.tar --scanners vuln --ignorefile /dev/null \
  --disable-telemetry --skip-version-check --no-progress --timeout 10m)

# Keep every severity, including findings without a fix, in the evidence report.
"${scanner[@]}" --format json --output /reports/trivy.json --exit-code 0
"${scanner[@]}" --skip-db-update --format cyclonedx --output /reports/sbom.cdx.json
docker run --rm --mount "type=bind,source=$PWD/ci,target=/ci,readonly" \
  --mount "type=bind,source=$reports,target=/reports,readonly" \
  --entrypoint python "trafficxx-ci:$revision" /ci/security_summary.py > "$reports/security-summary.md"
# Block fixable HIGH/CRITICAL OS and installed Python dependency findings.
"${scanner[@]}" --skip-db-update --severity HIGH,CRITICAL --ignore-unfixed \
  --format table --output /reports/security-gate.txt --exit-code 1
