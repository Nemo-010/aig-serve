#!/usr/bin/env python3
"""Repeat measurements: latency distributions, throughput, long streams, limits.

  python3 tests/bench.py --target https://x.trycloudflare.com --key K
"""

import argparse
import json
import statistics
import sys
import time
import threading

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import probe  # noqa: E402


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1)


def summarize(name, xs, extra=None):
    row = {"name": name, "n": len(xs), "p50": pct(xs, 0.5), "p95": pct(xs, 0.95),
           "min": round(min(xs), 1) if xs else 0, "max": round(max(xs), 1) if xs else 0,
           "mean": round(statistics.fmean(xs), 1) if xs else 0}
    if extra:
        row.update(extra)
    print(json.dumps(row), flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--json-out")
    a = ap.parse_args()
    probe.TARGET = a.target
    probe.KEY = a.key
    if not a.target.startswith("unix:"):
        probe.HOSTHDR = a.target.split("//", 1)[1].partition("/")[0]
    out = {"target": a.target, "rows": []}

    # 1. edge only: unauthenticated GET / (a small static page from the proxy)
    xs = []
    for _ in range(a.n):
        r = probe.request("/")
        xs.append(r["total_ms"])
    out["rows"].append(summarize("GET / (no auth, small page)", xs,
                                 {"bytes": len(r["body"]), "status": r["status"]}))

    # 2. authenticated small JSON
    xs = []
    for _ in range(a.n):
        r = probe.request("/v1/models", key=probe.KEY)
        xs.append(r["total_ms"])
    out["rows"].append(summarize("GET /v1/models", xs, {"status": r["status"]}))

    # 3. one chat completion, non-stream
    msgs = [{"role": "user", "content": "hello"}]
    xs = []
    for _ in range(a.n):
        r = probe.call("standin-echo", msgs)
        xs.append(r["total_ms"])
    out["rows"].append(summarize("POST chat (standin-echo, 1 msg)", xs, {"status": r["status"]}))

    # 4. streamed short answer, time to first frame
    ttfb = []
    tot = []
    for _ in range(a.n):
        r = probe.call("standin-echo", msgs, stream=True)
        ttfb.append(r["ttfb_ms"])
        tot.append(r["total_ms"])
    out["rows"].append(summarize("POST chat stream, ttfb", ttfb, {"status": r["status"]}))
    out["rows"].append(summarize("POST chat stream, total", tot))

    # 5. throughput, non-stream bodies
    for size in (1048576, 4194304):
        rr = []
        for _ in range(5):
            r = probe.call("standin-bulk", msgs, {"x_bytes": size})
            rr.append(r)
        body = max(len(x["body"]) for x in rr)
        secs = statistics.fmean([x["total_ms"] for x in rr]) / 1000
        out["rows"].append({"name": "bulk %d bytes" % size, "n": len(rr),
                            "ms_mean": round(secs * 1000, 1),
                            "kib_per_s": round(body / 1024 / secs, 1),
                            "status": rr[-1]["status"]})

    # 6. long stream: 32 chunks x 200 ms = 6.4 s, must not be cut
    r = probe.call("standin-trickle", msgs, {"x_delay_ms": 200}, stream=True)
    txt, frames, done = probe.sse_text(r["body"])
    out["rows"].append({"name": "trickle 6.4 s stream", "status": r["status"], "frames": frames,
                        "done": done, "content_bytes": len(txt), "total_ms": r["total_ms"],
                        "gap_p50": probe.gaps(r["chunks"]).get("gap_p50")})

    # 7. big request body: 2 MiB prompt
    big = "x" * (2 * 1024 * 1024)
    r = probe.call("standin-echo", [{"role": "user", "content": big}])
    out["rows"].append({"name": "2 MiB prompt", "status": r["status"], "ms": r["total_ms"],
                        "resp_bytes": len(r["body"])})

    # 8. concurrency: 16 parallel small completions
    lat = []
    lock = threading.Lock()

    def one():
        t = time.time()
        rr = probe.call("standin-echo", msgs)
        with lock:
            lat.append(((time.time() - t) * 1000, rr["status"]))

    t0 = time.time()
    ts = [threading.Thread(target=one) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = (time.time() - t0) * 1000
    out["rows"].append({"name": "16 parallel chat calls", "wall_ms": round(wall),
                        "p50": pct([x[0] for x in lat], 0.5),
                        "max": round(max(x[0] for x in lat), 1),
                        "statuses": sorted(set(s for _, s in lat))})

    # 9. CORS preflight and compression
    r = probe.request("/v1/chat/completions", method="OPTIONS")
    out["rows"].append({"name": "OPTIONS preflight", "status": r["status"],
                        "allow_origin": r["headers"].get("access-control-allow-origin"),
                        "allow_methods": r["headers"].get("access-control-allow-methods")})
    r = probe.request("/v1/chat/completions",
                      json.dumps({"model": "standin-bulk", "messages": msgs}),
                      key=probe.KEY, extra_headers={"Accept-Encoding": "gzip, br"})
    out["rows"].append({"name": "Accept-Encoding gzip,br", "status": r["status"],
                        "content_encoding": r["headers"].get("content-encoding"),
                        "transfer_encoding": r["headers"].get("transfer-encoding"),
                        "wire_bytes": len(r["body"])})

    # 10. is the answer chunked on the wire?
    r = probe.call("standin-trickle", msgs, stream=True)
    out["rows"].append({"name": "stream framing", "chunked": r.get("chunked"),
                        "content_length": r["headers"].get("content-length"),
                        "transfer_encoding": r["headers"].get("transfer-encoding")})

    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
