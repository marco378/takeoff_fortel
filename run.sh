#!/usr/bin/env bash
# Fortel AI Takeoff — portal run script.
#
#   ./run.sh start     start the portal in the background, logging to logs/portal.log
#   ./run.sh stop      stop the background portal (via PID file)
#   ./run.sh restart   stop then start
#   ./run.sh status    is it running, and the one-line health check
#   ./run.sh fg        run in the foreground (Ctrl-C to stop) — for debugging
#
# See DEPLOY.md for the full operations guide (backups, updating, launchd).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_PYTHON=".venv/bin/python"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/portal.log"
PID_FILE="run/portal.pid"

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

check_venv() {
  if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: $VENV_PYTHON not found." >&2
    echo "Set up the virtualenv first:" >&2
    echo "  python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
}

check_deps() {
  if ! "$VENV_PYTHON" -c "import flask, fitz, numpy, shapely, cv2, PIL" >/dev/null 2>&1; then
    echo "ERROR: dependencies missing or out of date in .venv." >&2
    echo "Run: .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
}

load_env() {
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
  fi
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

do_start() {
  check_venv
  check_deps
  load_env
  mkdir -p "$LOG_DIR" "$(dirname "$PID_FILE")"

  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Portal already running (PID $(cat "$PID_FILE"))."
    exit 0
  fi

  local host="${APPROVAL_HOST:-127.0.0.1}"
  local port="${APPROVAL_PORT:-5001}"
  echo "Starting portal on ${host}:${port} — logging to $LOG_FILE"

  nohup "$VENV_PYTHON" approval_server.py >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  sleep 1

  if kill -0 "$pid" 2>/dev/null; then
    echo "Started (PID $pid). Health check: ./run.sh status"
  else
    echo "ERROR: portal exited immediately — check $LOG_FILE" >&2
    rm -f "$PID_FILE"
    tail -n 20 "$LOG_FILE" >&2 || true
    exit 1
  fi
}

do_stop() {
  if [ ! -f "$PID_FILE" ]; then
    echo "No PID file ($PID_FILE) — portal does not appear to be running via run.sh."
    exit 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "Stopping portal (PID $pid)..."
    kill "$pid"
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "Still running after 10s, sending SIGKILL." >&2
      kill -9 "$pid" 2>/dev/null || true
    fi
  else
    echo "PID $pid not running (stale PID file)."
  fi
  rm -f "$PID_FILE"
}

do_status() {
  load_env
  local host="${APPROVAL_HOST:-127.0.0.1}"
  local port="${APPROVAL_PORT:-5001}"

  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Process: running (PID $(cat "$PID_FILE"))"
  else
    echo "Process: NOT running"
  fi

  echo "Health check: curl -s http://${host}:${port}/status"
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 3 "http://${host}:${port}/status" && echo || echo "  (no response)"
  fi
}

do_fg() {
  check_venv
  check_deps
  load_env
  exec "$VENV_PYTHON" approval_server.py
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  fg)      do_fg ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|fg}" >&2
    exit 1
    ;;
esac
