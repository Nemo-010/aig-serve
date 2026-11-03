#!/bin/sh
# Start the relay daemon, or say what is wrong with the environment.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cfrs=${CFRS:-/workspace/.scratch/cfrs-bin/cfrs}

[ -n "${KENARI_API_KEY:-}" ] || { echo "KENARI_API_KEY is not set" >&2; exit 78; }
[ -x "$cfrs" ] || { echo "no cfrs at $cfrs (set CFRS=)" >&2; exit 78; }

exec "$here/target/release/relay" --daemon \
    --socket   "$here/run/relay.sock" \
    --keys     "$here/keys.txt" \
    --cfrs     "$cfrs" \
    --url-file "$here/run/url.txt" \
    --pidfile  "$here/run/relay.pid" \
    --logfile  "$here/logs/relay.log"
