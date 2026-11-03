#!/bin/sh
# Stop the daemon; it takes its cfrs child with it.
here=$(cd "$(dirname "$0")" && pwd)
if [ -f "$here/run/relay.pid" ]; then
    pid=$(cat "$here/run/relay.pid")
    kill "$pid" 2>/dev/null && echo "stopped relay $pid"
    sleep 1
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
else
    echo "no pidfile"
fi
rm -f "$here/run/relay.pid"
