#!/usr/bin/env python3
"""Poll the rig while generating traffic, and print the live series.

The point is to watch counters move rather than trust one reading: the
/metrics endpoint is public, so polling it costs nothing but a round trip.

  python3 tests/poll.py --target https://x.trycloudflare.com --key K \
          --interval 2 --duration 120 --json-out /tmp/poll.json
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import probe  # noqa: E402

STOP = threading.Event()
SENT = {"echo": 0, "trickle": 0, "bulk": 0, "flaky": 0}
SENT_LOCK = threading.Lock()
CLIENT_LAT = []


def bump(model):
    with SENT_LOCK:
        SENT[model] += 1


def generator(key):
    """Deterministic light load: something fast, something slow, something big."""
    msgs = [{"role": "user", "content": "load"}]
    i = 0
    while not STOP.is_set():
        i += 1
        try:
            if i % 12 == 0:
                r = probe.call("standin-bulk", msgs, {"x_bytes": 65536}, )
                bump("bulk")
            elif i % 6 == 0:
                r = probe.call("standin-trickle", msgs, {"x_delay_ms": 120}, stream=True)
                bump("trickle")
            elif i % 20 == 0:
                r = probe.call("standin-flaky", msgs, {"x_case": "429"})
                bump("flaky")
            else:
                r = probe.call("standin-echo", msgs)
                bump("echo")
            with SENT_LOCK:
                CLIENT_LAT.append((round(r["total_ms"], 1), r["status"]))
        except Exception as exc:  # noqa: BLE001 - a dead sample is a datum
            with SENT_LOCK:
                CLIENT_LAT.append((-1, "EXC " + type(exc).__name__))
        STOP.wait(1.0)


def tunnel_state():
    try:
        out = subprocess.run(["/workspace/.scratch/cfrs-bin/cfrs", "ps"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if line.startswith("llmproxy"):
                f = line.split()
                return {"state": f[1], "age": f[4] if len(f) > 4 else None}
    except Exception:
        pass
    return {"state": "unknown"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--json-out")
    a = ap.parse_args()

    probe.TARGET = a.target
    probe.KEY = a.key
    if not a.target.startswith("unix:"):
        probe.HOSTHDR = a.target.split("//", 1)[1].partition("/")[0]

    gen = threading.Thread(target=generator, args=(a.key,), daemon=True)
    gen.start()

    samples = []
    t0 = time.time()
    poll_lat = []
    print("%-8s %6s %5s %5s %6s %6s %6s %8s %s" %
          ("t+s", "reqs", "infl", "max", "p50", "p95", "poll_ms", "bytes", "sent e/t/b/f"))
    while time.time() - t0 < a.duration:
        t = time.time() - t0
        s0 = time.time()
        try:
            r = probe.request("/metrics")
            doc = json.loads(r["body"])
            poll_ms = round((time.time() - s0) * 1000, 1)
        except Exception as exc:  # noqa: BLE001
            print("%-8.0f poll failed: %r" % (t, exc))
            time.sleep(a.interval)
            continue
        poll_lat.append(poll_ms)
        with SENT_LOCK:
            sent = dict(SENT)
        row = {"t": round(t, 1), "requests": doc["requests"], "inflight": doc["inflight"],
               "max_inflight": doc["max_inflight"], "p50": doc["latency_ms"]["p50"],
               "p95": doc["latency_ms"]["p95"], "poll_ms": poll_ms,
               "bytes_out": doc["bytes_out"],
               "unauthorized": doc["unauthorized"], "upstream_error": doc["upstream_error"],
               "per_model": {k: v["n"] for k, v in doc["per_model"].items()},
               "sent": sent, "tunnel": tunnel_state() if int(t) % 20 == 0 else None}
        samples.append(row)
        print("%-8.0f %6d %5d %5d %6.1f %6.1f %6.1f %8d %d/%d/%d/%d" %
              (t, doc["requests"], doc["inflight"], doc["max_inflight"],
               doc["latency_ms"]["p50"], doc["latency_ms"]["p95"], poll_ms,
               doc["bytes_out"], sent["echo"], sent["trickle"], sent["bulk"], sent["flaky"]),
              flush=True)
        time.sleep(max(0.0, a.interval - (time.time() - s0)))

    STOP.set()
    gen.join(timeout=10)

    ok = [x[0] for x in CLIENT_LAT if x[1] == 200]
    bad = [x for x in CLIENT_LAT if x[1] != 200]
    ok.sort()

    def pct(xs, p):
        return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else 0

    summary = {
        "target": a.target, "duration_s": a.duration, "samples": len(samples),
        "requests_in_proxy": samples[-1]["requests"] if samples else 0,
        "proxy_req_delta": (samples[-1]["requests"] - samples[0]["requests"]) if len(samples) > 1 else 0,
        "peak_inflight": max((s["max_inflight"] for s in samples), default=0),
        "client_calls": len(CLIENT_LAT), "client_ok": len(ok),
        "client_bad": [(s, c) for c, s in bad][:10],
        "client_p50": pct(ok, 0.5), "client_p95": pct(ok, 0.95),
        "client_max": ok[-1] if ok else 0,
        "poll_rtt_p50": pct(poll_lat, 0.5), "poll_rtt_max": max(poll_lat or [0]),
        "bytes_out": samples[-1]["bytes_out"] if samples else 0,
        "sent": SENT, "tunnel_states": sorted({(s["tunnel"] or {}).get("state")
                                               for s in samples if s["tunnel"]}),
    }
    print("\n--- summary ---")
    print(json.dumps(summary, indent=1))
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump({"summary": summary, "samples": samples}, f, indent=1)


if __name__ == "__main__":
    main()
