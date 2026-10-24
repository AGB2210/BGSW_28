#!/usr/bin/env bash
# Run the Action API and the On-Call Console until this script ends, then stop both.
# Used by start.bat; also works on its own: scripts/serve.sh
#
# Ends on Ctrl+C, when the terminal window is closed, or when either server exits.
# The demo containers are separate: demo.bat (scripts/demo.sh run) starts and stops them.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$HOME/.venvs/oncall}"
URL="http://localhost:8800"
cd "$ROOT"

servers() { pgrep -f "^$VENV/bin/python3? $VENV/bin/(action-api|oncall-console)"; }

stop() {
  trap - INT TERM HUP EXIT
  echo
  echo "Stopping the On-Call Console and Action API..."
  kill $(servers) 2>/dev/null
  for _ in 1 2 3 4 5; do servers >/dev/null || break; sleep 1; done
  kill -9 $(servers) 2>/dev/null
  echo "Stopped."
  exit 0
}

if servers >/dev/null; then
  echo "Stopping servers left over from an earlier run..."
  kill $(servers) 2>/dev/null; sleep 2
fi

running=$(docker ps --filter status=running -q 2>/dev/null | wc -l)
if [ "$running" -lt 22 ]; then
  echo "Note: only $running of 22 demo containers are running. Start the demo with demo.bat"
  echo "(or scripts/demo.sh run) so the agent has something to watch."
fi

trap stop INT TERM HUP EXIT

# The servers ignore Ctrl+C themselves (no tracebacks); this script stops them instead.
# They still get SIGHUP if the window is closed before this script can react.
( trap '' INT; exec "$VENV/bin/action-api" ) &
( trap '' INT; exec "$VENV/bin/oncall-console" ) &

for _ in $(seq 30); do
  curl -sf -o /dev/null "$URL/api/state" && break
  sleep 1
done
explorer.exe "$URL" 2>/dev/null
echo
echo "On-Call Console: $URL   (dry run is on by default)"
echo "Press Ctrl+C or close this window to stop."
echo

wait -n
echo "A server exited."
stop
