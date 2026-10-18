#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

suite="${1:-database}"
case "$suite" in
  database) profile='test'; service=tests ;;
  broker)
    if [[ "${SERVICEBUS_ACCEPT_EULA:-N}" != Y ]]; then
      echo 'Broker CI requires explicit SERVICEBUS_ACCEPT_EULA=Y for both Microsoft licenses.' >&2
      exit 2
    fi
    profile=broker-test; service=broker-tests ;;
  *) echo 'Usage: bash ci/test.sh database|broker' >&2; exit 2 ;;
esac

project="${CI_COMPOSE_PROJECT:-trafficxx-ci-${suite}-local}"
if [[ ! "$project" =~ ^trafficxx-ci-[a-z0-9_-]+$ ]]; then
  echo 'Refusing to manage a project outside the reserved trafficxx-ci- prefix.' >&2
  exit 2
fi
export CI_REPORT_DIR="$PWD/test-results/ci/$project"
CI_TEST_UID="$(id -u)"
CI_TEST_GID="$(id -g)"
export CI_TEST_UID CI_TEST_GID
mkdir -p "$CI_REPORT_DIR"
compose=(docker compose --env-file .env.example --project-name "$project" -f compose.yml -f ci/compose.yml --profile "$profile")

cleanup() {
  status=$?
  trap - EXIT
  "${compose[@]}" logs --no-color --tail 300 > "$CI_REPORT_DIR/compose.log" 2>&1 || true
  "${compose[@]}" down --volumes --remove-orphans --timeout 10 > "$CI_REPORT_DIR/cleanup.log" 2>&1 || true
  exit "$status"
}
trap cleanup EXIT
"${compose[@]}" config --quiet
"${compose[@]}" run --build --rm "$service"
