#!/usr/bin/env bash
# Run the OpenTelemetry Demo 3.1.0 release in a lean setup for this project.
#
#   scripts/demo.sh run      start the demo, wait until healthy, stop it on Ctrl+C (demo.bat)
#   scripts/demo.sh pull     download the images (once)
#   scripts/demo.sh up       start the demo in the background
#   scripts/demo.sh stop     stop the containers (kept, so the next start is quick)
#   scripts/demo.sh down     stop and remove the demo containers
#   scripts/demo.sh status   container health
#
# Lean = the demo's minimal mode (no Kafka, accounting, fraud-detection) minus
# grafana, flagd-ui and telemetry-docs, which the agent does not use.
# compose.local.yaml (in this repo) raises six memory caps; see that file for why.
# Expects the demo cloned at $DEMO_DIR (default ~/opentelemetry-demo) at tag 3.1.0;
# `run` and `pull` clone it there if it is missing.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DEMO_DIR="${DEMO_DIR:-$HOME/opentelemetry-demo}"
LOCAL_OVERRIDE="$(cd "$(dirname "$0")/.." && pwd)/compose.local.yaml"
export DEMO_VERSION="${DEMO_VERSION:-3.1.0}"   # release images, not nightly "latest"

SERVICES=(
  # shop
  frontend-proxy frontend ad cart checkout currency email image-provider payment
  product-catalog quote recommendation shipping load-generator
  # data stores and flags
  astronomy-db valkey-cart flagd
  # telemetry
  otel-collector opamp-server jaeger prometheus opensearch
)

ensure_clone() {
  [ -d "$DEMO_DIR" ] && return
  echo "Cloning the OpenTelemetry Demo $DEMO_VERSION into $DEMO_DIR..."
  git clone --quiet --branch "$DEMO_VERSION" --depth 1 \
    https://github.com/open-telemetry/opentelemetry-demo.git "$DEMO_DIR"
}

wait_for_docker() {
  for _ in $(seq 60); do docker info >/dev/null 2>&1 && return; sleep 1; done
  echo "Docker is not running in WSL (try: sudo systemctl start docker)" >&2
  exit 1
}

compose() {
  (cd "$DEMO_DIR" && docker compose --env-file .env --env-file .env.override \
    -f compose.yaml -f compose.observability.yaml -f compose.extras.yaml -f "$LOCAL_OVERRIDE" "$@")
}

NAME_FILTER="name=^($(IFS='|'; echo "${SERVICES[*]}"))$"

# "<ready> <total>": ready = running and, where a health check exists, healthy
readiness() {
  docker ps -a --filter "$NAME_FILTER" --format '{{.State}} {{.Status}}' |
    awk '{ total++ } /^running/ && !/starting|unhealthy/ { ready++ } END { print ready+0, total+0 }'
}

# All at once (compose stops one dependency level at a time, which takes over a minute).
# Stopped containers stay stopped when WSL restarts; `up` or `run` starts them again.
stop_all() {
  local running
  running=$(docker ps -q --filter "$NAME_FILTER")
  [ -z "$running" ] || docker stop -t 10 $running >/dev/null
}

run() {
  ensure_clone
  wait_for_docker
  echo "Starting the demo containers (images are downloaded the first time, ~10 GB)..."
  compose up -d --no-build --pull missing --no-deps "${SERVICES[@]}"

  # Ctrl+C: stop here and show progress. Window closed: the terminal is gone, so stop
  # in a detached process that does not need it.
  trap 'trap - INT TERM HUP; echo; echo "Stopping the demo containers..."; stop_all; echo "Stopped."; exit 0' INT TERM
  trap '(trap "" HUP; exec setsid bash "$SELF" stop </dev/null >/tmp/demo-stop.log 2>&1) & exit 0' HUP

  local last=""
  for _ in $(seq 180); do
    read -r ready total < <(readiness)
    [ "$ready/$total" != "$last" ] && printf '\r  ready: %s/%s containers ' "$ready" "$total" && last="$ready/$total"
    [ "$total" -gt 0 ] && [ "$ready" -eq "$total" ] && break
    sleep 2
  done
  echo
  [ "${ready:-0}" -lt "${total:-1}" ] && echo "Some containers are not healthy yet; see: bash scripts/demo.sh status"
  cat <<EOF

OpenTelemetry Demo is running.
  Shop:        http://localhost:8080
  Jaeger:      http://localhost:8080/jaeger/ui
  Prometheus:  http://localhost:9090
Allow about 5 minutes for traffic to settle before investigating.
Press Ctrl+C or close this window to stop the demo.
EOF
  while true; do sleep 3600 & wait $!; done
}

case "${1:-}" in
  run)    run ;;
  pull)   ensure_clone; compose pull "${SERVICES[@]}" ;;
  up)     compose up -d --no-build --pull never --no-deps "${SERVICES[@]}" ;;
  stop)   stop_all ;;
  down)   compose down --remove-orphans ;;
  status) docker ps --format 'table {{.Names}}\t{{.Status}}' ;;
  *)      echo "usage: $0 {run|pull|up|stop|down|status}" >&2; exit 2 ;;
esac
