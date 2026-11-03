#!/bin/sh
# Bring up the quick tunnel, waiting out a rate-limit answer (error code 1015).
# Writes the public URL to run/url.txt. POSIX sh.
set -u
here=$(cd "$(dirname "$0")" && pwd)
cfrs=${CFRS:-/workspace/.scratch/cfrs-bin/cfrs}
sock="$here/run/proxy.sock"
attempt=1

while [ "$attempt" -le 8 ]; do
    echo "--- attempt $attempt at $(date -u +%H:%M:%S)"
    out=$("$cfrs" tunnel --unix "$sock" --forward "unix:$sock" \
        --detach --json --name llmproxy --ready-timeout 45 2>&1)
    echo "$out"
    url=$(printf '%s' "$out" | sed -n 's/.*"url":"\([^"]*\)".*/\1/p' | head -1)
    if [ -n "$url" ]; then
        printf '%s\n' "$url" > "$here/run/url.txt"
        echo "READY $url"
        exit 0
    fi
    wait_s=$(printf '%s' "$out" | sed -n 's/.*Retry-After: \([0-9]*\) s.*/\1/p' | head -1)
    [ -n "$wait_s" ] || wait_s=60
    wait_s=$((wait_s + 8))
    echo "sleeping ${wait_s}s"
    sleep "$wait_s"
    attempt=$((attempt + 1))
done
echo "GAVEUP"
exit 1
