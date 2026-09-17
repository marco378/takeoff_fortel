#!/usr/bin/env bash
# Deploy to Railway, but ONLY when nothing is in flight.
#
# The pre-flight used to be `curl /status; git push` in one command: it PRINTED the
# in-flight count and pushed regardless. On 10 Sep that pushed with processing_count=2.
# A restart re-queues work that was merely queued, but a job actually MEASURING is
# marked UNMEASURED and routed to the assessor -- so the client loses that upload.
# Reading a number you do not act on is not a check. This exits non-zero instead.
set -euo pipefail
PROD="${PROD:-https://takeofffortel-production.up.railway.app}"
# The deploy remote is `origin`. It was `marco378` until the 16 Sep 2026 move to
# sarabloh-1, where that remote does not exist at all -- `git ls-remote marco378` fails
# outright, so this script died at the push with the pre-flight already passed. Nothing
# reached Railway (set -e), but the deploy path was broken and would have been found at
# the worst moment: the next time a fix was cleared to ship.
REMOTE="${REMOTE:-origin}"
status=$(curl -fsS "$PROD/status")
echo "pre-flight: $status"
proc=$(printf '%s' "$status" | sed -n 's/.*"processing_count":\([0-9]*\).*/\1/p')
queued=$(printf '%s' "$status" | sed -n 's/.*"queued_count":\([0-9]*\).*/\1/p')
if [ "${proc:-0}" != "0" ] || [ "${queued:-0}" != "0" ]; then
  echo "REFUSING TO DEPLOY: $proc processing, $queued queued." >&2
  echo "A restart would strand measuring jobs as UNMEASURED. Wait, then re-run." >&2
  exit 1
fi
sha=$(git rev-parse --short HEAD)
git push "$REMOTE" main
echo "pushed $sha — waiting for it to go live..."
for _ in $(seq 1 60); do
  live=$(curl -fsS "$PROD/status" 2>/dev/null || true)
  case "$live" in *"$sha"*) echo "LIVE: $live"; exit 0;; esac
  sleep 10
done
echo "TIMED OUT waiting for $sha; check Railway." >&2; exit 1
