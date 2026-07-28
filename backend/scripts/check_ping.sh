#!/usr/bin/env bash
# Verify /ping stays responsive while a batch is running (prd.md R7, implement.md phase 6).
#
# This is the check that catches a blocking call in the entrypoint. A synchronous subprocess or
# file read inside the handler stalls the shared event loop, /ping times out, and the platform
# terminates a perfectly healthy instance in the middle of a batch -- losing every material in
# flight. The symptom looks like a random platform failure, so it is worth testing directly.
#
#   bash backend/scripts/check_ping.sh [host] [samples]
set -uo pipefail

HOST="${1:-localhost:8080}"
SAMPLES="${2:-20}"
status=0

for i in $(seq 1 "$SAMPLES"); do
    elapsed=$(curl -s -m 1 -o /dev/null -w '%{time_total}' "http://${HOST}/ping" 2>/dev/null)
    if [ -z "$elapsed" ]; then
        echo "sample $i: TIMEOUT or connection failure -- the entrypoint is blocking"
        status=1
    else
        echo "sample $i: ${elapsed}s"
        awk -v t="$elapsed" 'BEGIN {exit (t > 1.0) ? 0 : 1}' \
            && { echo "  over the 1s budget"; status=1; } || true
    fi
    sleep 3
done

[ "$status" -eq 0 ] && echo "PASS: /ping stayed healthy" || echo "FAIL: see samples above"
exit "$status"
