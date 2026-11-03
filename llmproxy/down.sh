#!/bin/sh
# Stop the router by pidfile, then the four standins by pidfile if present.
here=$(cd "$(dirname "$0")" && pwd)
if [ -f "$here/run/proxy.pid" ]; then
    pid=$(cat "$here/run/proxy.pid")
    kill "$pid" 2>/dev/null && echo "stopped proxy $pid"
    rm -f "$here/run/proxy.pid"
else
    echo "no proxy pidfile"
fi
for name in echo trickle bulk flaky; do
    pid=$(cat "$here/run/$name.pid" 2>/dev/null) || continue
    kill "$pid" 2>/dev/null && echo "stopped $name $pid"
    rm -f "$here/run/$name.pid"
done
