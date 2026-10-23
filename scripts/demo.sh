#!/usr/bin/env bash
# Run the OpenTelemetry Demo 3.1.0 release in a lean setup for this project.
#
#   scripts/demo.sh pull     download the images (once)
#   scripts/demo.sh up       start the demo
#   scripts/demo.sh down     stop and remove the demo containers
#   scripts/demo.sh status   container health
#
# Lean = the demo's minimal mode (no Kafka, accounting, fraud-detection) minus
# grafana, flagd-ui and telemetry-docs, which the agent does not use.
# Expects the demo cloned at $DEMO_DIR (default ~/opentelemetry-demo) and checked out at tag 3.1.0.
set -euo pipefail

DEMO_DIR="${DEMO_DIR:-$HOME/opentelemetry-demo}"
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

cd "$DEMO_DIR"
compose() {
  docker compose --env-file .env --env-file .env.override \
    -f compose.yaml -f compose.observability.yaml -f compose.extras.yaml "$@"
}

case "${1:-}" in
  pull)   compose pull "${SERVICES[@]}" ;;
  up)     compose up -d --no-build --pull never --no-deps "${SERVICES[@]}" ;;
  down)   compose down --remove-orphans ;;
  status) docker ps --format 'table {{.Names}}\t{{.Status}}' ;;
  *)      echo "usage: $0 {pull|up|down|status}" >&2; exit 2 ;;
esac
