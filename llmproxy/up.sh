#!/bin/sh
# Start the four standins and the proxy on unix sockets.
set -e
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$here/run" "$here/logs"
for name in echo trickle bulk flaky; do
    if [ -S "$here/run/$name.sock" ] && pgrep -f "standins/$name.py" >/dev/null 2>&1; then
        echo "$name already up"
        continue
    fi
    setsid python3 "$here/standins/$name.py" --socket "$here/run/$name.sock" \
        >> "$here/logs/$name.log" 2>&1 < /dev/null &
    echo $! > "$here/run/$name.pid"
    echo "$name started (pid $!)"
done
if pgrep -f "python3 $here/proxy.py" >/dev/null 2>&1; then
    echo "proxy already up"
else
    setsid python3 "$here/proxy.py" >> "$here/logs/proxy.log" 2>&1 < /dev/null &
    echo $! > "$here/run/proxy.pid"
    echo "proxy started (pid $!)"
fi
sleep 1
curl -sS --unix-socket "$here/run/proxy.sock" http://localhost/healthz || true
echo
