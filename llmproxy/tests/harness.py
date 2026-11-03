#!/usr/bin/env python3
"""One-file harness for a cfrs-tunnelled OpenAI-shaped proxy. Stdlib only.

  python3 harness.py suite --target https://X --key K      # 16 checks
  python3 harness.py bench --target https://X --key K -n 25 # latency, throughput, limits
  python3 harness.py poll  --target https://X --key K --duration 120  # live counters

Nothing here imports a local module, so this file can be copied anywhere.
"""

import argparse
import hashlib
import json
import socket
import ssl
import statistics
import sys
import threading
import time
import uuid

TARGET = None
KEY = None
HOSTHDR = "localhost"
RESULTS = []


# ---------------------------------------------------------------- transport

def connect():
    global HOSTHDR
    if TARGET.startswith("unix:"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(120)
        s.connect(TARGET[5:])
        return s
    plain = TARGET.startswith("http://")
    hostport = TARGET.split("//", 1)[1].partition("/")[0]
    host = hostport.split(":")[0]
    port = int(hostport.split(":")[1]) if ":" in hostport else (80 if plain else 443)
    # Prefer IPv4: a host with no route to IPv6 fails on the AAAA record.
    infos = [i for i in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
             if i[0] == socket.AF_INET]
    if not infos:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    raw = None
    err = None
    for fam, typ, proto, _, addr in infos:
        try:
            raw = socket.socket(fam, typ, proto)
            raw.settimeout(30)
            raw.connect(addr)
            break
        except OSError as exc:
            err, raw = exc, None
    if raw is None:
        raise err
    if plain:
        raw.settimeout(120)
        return raw
    ctx = ssl.create_default_context()
    s = ctx.wrap_socket(raw, server_hostname=host)
    s.settimeout(120)
    return s


def request(path, body=None, key=None, method=None, extra_headers=None):
    """One request. Returns status, headers, body, ttfb_ms, total_ms, chunks.

    chunks is [(ms, payload_bytes)] and is what you time a stream with. The
    edge answers with Transfer-Encoding: chunked and no Content-Length, so the
    body is de-chunked here -- a client that reads it as one blob sees the
    chunk framing inside the SSE text and calls it corruption.
    """
    s = connect()
    method = method or ("POST" if body is not None else "GET")
    payload = b"" if body is None else (body.encode() if isinstance(body, str) else body)
    hs = ["%s %s HTTP/1.1" % (method, path), "Host: " + HOSTHDR, "Connection: close",
          "User-Agent: harness/1"]
    if key:
        hs.append("Authorization: Bearer " + key)
    for k, v in (extra_headers or {}).items():
        hs.append("%s: %s" % (k, v))
    if body is not None:
        hs.append("Content-Type: application/json")
        hs.append("Content-Length: %d" % len(payload))
    t0 = time.time()
    s.sendall(("\r\n".join(hs) + "\r\n\r\n").encode() + payload)

    buf = b""
    ttfb = None
    while b"\r\n\r\n" not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
        if ttfb is None:
            ttfb = (time.time() - t0) * 1000
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines and lines[0].startswith("HTTP/") else 0
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, _, v = ln.partition(":")
            hdrs[k.strip().lower()] = v.strip()

    cl = hdrs.get("content-length")
    chunked = "chunked" in hdrs.get("transfer-encoding", "").lower()
    data = b""
    chunks = []
    state = ["size", 0]

    def feed(b, at):
        nonlocal data
        while b:
            if state[0] == "size":
                i = b.find(b"\r\n")
                if i < 0:
                    return b
                size = int(b[:i].split(b";")[0] or b"0", 16)
                b = b[i + 2:]
                if size == 0:
                    state[0] = "done"
                    return b
                state[0], state[1] = "data", size
            elif state[0] == "data":
                take = b[:state[1]]
                if take:
                    data += take
                    chunks.append((at, len(take)))
                if len(take) < state[1]:
                    state[1] -= len(take)
                    return b""
                b = b[state[1]:]
                state[0] = "crlf"
            elif state[0] == "crlf":
                if len(b) < 2:
                    return b
                b = b[2:]
                state[0] = "size"
            else:
                return b""
        return b

    if chunked:
        pending = feed(rest, round((time.time() - t0) * 1000, 1))
        while state[0] != "done":
            try:
                c = s.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            pending = feed(pending + c, round((time.time() - t0) * 1000, 1))
    else:
        data = rest
        if rest:
            chunks.append((round((time.time() - t0) * 1000, 1), len(rest)))
        while True:
            if cl is not None and len(data) >= int(cl):
                break
            try:
                c = s.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            data += c
            chunks.append((round((time.time() - t0) * 1000, 1), len(c)))
    total = (time.time() - t0) * 1000
    try:
        s.close()
    except OSError:
        pass
    return {"status": status, "headers": hdrs, "body": data, "chunked": chunked,
            "ttfb_ms": round(ttfb or 0, 1), "total_ms": round(total, 1), "chunks": chunks}


def digest_of(prompt):
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def sse_text(body):
    """Concatenated delta content, frame count, whether [DONE] arrived."""
    text, frames, done = [], 0, False
    for line in body.decode("utf-8", "replace").split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            doc = json.loads(payload)
        except ValueError:
            continue
        frames += 1
        for ch in doc.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                text.append(piece)
    return "".join(text), frames, done


def call(model, messages, extra=None, stream=False):
    body = {"model": model, "messages": messages}
    if stream:
        body["stream"] = True
    if extra:
        body.update(extra)
    return request("/v1/chat/completions", json.dumps(body), key=KEY)


def gaps(chunks):
    if len(chunks) < 2:
        return {"chunks": len(chunks), "first_ms": chunks[0][0] if chunks else None}
    d = sorted(round(chunks[i][0] - chunks[i - 1][0], 1) for i in range(1, len(chunks)))
    return {"chunks": len(chunks), "gap_p50": d[len(d) // 2], "gap_max": d[-1],
            "first_ms": chunks[0][0], "last_ms": chunks[-1][0]}


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1)


def record(name, ok, **kw):
    row = {"test": name, "ok": bool(ok)}
    row.update(kw)
    RESULTS.append(row)
    print(("PASS " if ok else "FAIL ") + name + "  " +
          "  ".join("%s=%s" % (k, v) for k, v in kw.items() if v is not None), flush=True)


# -------------------------------------------------------------------- suite

def cmd_suite(a):
    msgs = [{"role": "system", "content": "you are a standin"},
            {"role": "user", "content": "prove the tunnel is intact " + uuid.uuid4().hex[:6]}]
    want = digest_of("\n".join(m["content"] for m in msgs))

    r = request("/v1/models")
    record("no_key_401", r["status"] == 401, status=r["status"], ms=r["total_ms"])

    r = request("/v1/models", key=KEY)
    ids = []
    try:
        ids = [m["id"] for m in json.loads(r["body"])["data"]]
    except Exception:
        pass
    standins = [i for i in ids if not i.startswith("real/")]
    record("models", r["status"] == 200 and len(standins) == 4, status=r["status"],
           ms=r["total_ms"], standins=",".join(standins),
           real=len([i for i in ids if i.startswith("real/")]))

    r = call("standin-echo", msgs)
    try:
        doc = json.loads(r["body"])
        txt = doc["choices"][0]["message"]["content"]
        usage = doc.get("usage") or {}
    except Exception:
        txt, usage = "", {}
    record("echo_nonstream", r["status"] == 200 and ("DIGEST " + want) in txt,
           status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], bytes=len(r["body"]),
           digest_ok=("DIGEST " + want) in txt,
           usage="%s/%s" % (usage.get("prompt_tokens"), usage.get("completion_tokens")))

    r = call("standin-echo", msgs, stream=True)
    txt, frames, done = sse_text(r["body"])
    record("echo_stream", r["status"] == 200 and done and ("DIGEST " + want) in txt,
           status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], frames=frames,
           done=done, digest_ok=("DIGEST " + want) in txt, **gaps(r["chunks"]))

    r = call("standin-trickle", msgs, stream=True)
    txt, frames, done = sse_text(r["body"])
    record("trickle_stream", r["status"] == 200 and done and frames >= 33,
           status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], frames=frames,
           done=done, digest_ok=("DIGEST " + want) in txt, **gaps(r["chunks"]))

    r = call("standin-trickle", msgs)
    record("trickle_nonstream", r["status"] == 200 and r["total_ms"] > 1500,
           status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], bytes=len(r["body"]))

    r = call("standin-bulk", msgs)
    try:
        blen = len(json.loads(r["body"])["choices"][0]["message"]["content"])
    except Exception:
        blen = 0
    record("bulk_nonstream_256k", r["status"] == 200 and blen == 262144,
           status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], bytes=len(r["body"]),
           content_bytes=blen)

    r = call("standin-bulk", msgs, stream=True)
    txt, frames, done = sse_text(r["body"])
    record("bulk_stream_256k", r["status"] == 200 and done and len(txt) == 262144,
           status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], frames=frames,
           content_bytes=len(txt), **gaps(r["chunks"]))

    r = call("standin-bulk", msgs, {"x_bytes": 1048576})
    record("bulk_nonstream_1m", r["status"] == 200 and len(r["body"]) > 1048576,
           status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], bytes=len(r["body"]))

    r = request("/v1/chat/completions",
                json.dumps({"model": "no-such-model", "messages": msgs}), key=KEY)
    record("unknown_model_400", r["status"] == 400, status=r["status"], ms=r["total_ms"])

    cases = (("429", 429, "flaky_429"), ("500", 500, "flaky_500"), ("400", 400, "flaky_400"),
             ("slow-headers", 200, "flaky_slow_headers"),
             ("stream-abort", 200, "flaky_stream_abort"))
    for case, want_status, label in cases:
        r = call("standin-flaky", msgs, {"x_case": case}, stream=(case == "stream-abort"))
        if case == "stream-abort":
            _, frames, done = sse_text(r["body"])
            record(label, r["status"] == 200 and not done and frames == 5,
                   status=r["status"], frames=frames, done=done, ms=r["total_ms"])
        elif case == "slow-headers":
            record(label, r["status"] == 200 and r["ttfb_ms"] > 4000, status=r["status"],
                   ttfb=r["ttfb_ms"], ms=r["total_ms"])
        else:
            record(label, r["status"] == want_status, status=r["status"], ms=r["total_ms"],
                   retry_after=r["headers"].get("retry-after"), body_len=len(r["body"]))

    out = []

    def one():
        t = time.time()
        rr = call("standin-trickle", msgs, stream=True)
        _, fr, dn = sse_text(rr["body"])
        out.append((rr["status"], fr, dn, round((time.time() - t) * 1000)))

    t0 = time.time()
    ts = [threading.Thread(target=one) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = round((time.time() - t0) * 1000)
    record("concurrency_8_streams",
           len(out) == 8 and all(s == 200 and d and f >= 33 for s, f, d, _ in out),
           wall_ms=wall, slowest=max(o[3] for o in out) if out else 0)

    doc = {"target": TARGET, "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "results": RESULTS, "passed": sum(1 for r in RESULTS if r["ok"]),
           "total": len(RESULTS)}
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(doc, f, indent=1)
    print("\n%d/%d passed on %s" % (doc["passed"], doc["total"], TARGET))
    return 0 if doc["passed"] == doc["total"] else 1


# -------------------------------------------------------------------- bench

def cmd_bench(a):
    msgs = [{"role": "user", "content": "hello"}]
    out = {"target": TARGET, "rows": []}

    def row(name, xs, extra=None):
        d = {"name": name, "n": len(xs), "p50": pct(xs, 0.5), "p95": pct(xs, 0.95),
             "min": round(min(xs), 1) if xs else 0, "max": round(max(xs), 1) if xs else 0,
             "mean": round(statistics.fmean(xs), 1) if xs else 0}
        if extra:
            d.update(extra)
        out["rows"].append(d)
        print(json.dumps(d), flush=True)

    xs = []
    for _ in range(a.n):
        r = request("/")
        xs.append(r["total_ms"])
    row("GET / (no auth, small page)", xs, {"bytes": len(r["body"]), "status": r["status"]})

    xs = []
    for _ in range(a.n):
        r = request("/v1/models", key=KEY)
        xs.append(r["total_ms"])
    row("GET /v1/models", xs, {"status": r["status"]})

    xs = []
    for _ in range(a.n):
        r = call("standin-echo", msgs)
        xs.append(r["total_ms"])
    row("POST chat (standin-echo, 1 msg)", xs, {"status": r["status"]})

    ttfb, tot = [], []
    for _ in range(a.n):
        r = call("standin-echo", msgs, stream=True)
        ttfb.append(r["ttfb_ms"])
        tot.append(r["total_ms"])
    row("POST chat stream, ttfb", ttfb, {"status": r["status"]})
    row("POST chat stream, total", tot)

    for size in (1048576, 4194304):
        rr = [call("standin-bulk", msgs, {"x_bytes": size}) for _ in range(5)]
        body = max(len(x["body"]) for x in rr)
        secs = statistics.fmean([x["total_ms"] for x in rr]) / 1000
        out["rows"].append({"name": "bulk %d bytes" % size, "n": len(rr),
                            "ms_mean": round(secs * 1000, 1),
                            "kib_per_s": round(body / 1024 / secs, 1),
                            "status": rr[-1]["status"]})
        print(json.dumps(out["rows"][-1]), flush=True)

    r = call("standin-trickle", msgs, {"x_delay_ms": 200}, stream=True)
    txt, frames, done = sse_text(r["body"])
    out["rows"].append({"name": "trickle 6.4 s stream", "status": r["status"], "frames": frames,
                        "done": done, "content_bytes": len(txt), "total_ms": r["total_ms"],
                        "gap_p50": gaps(r["chunks"]).get("gap_p50")})
    print(json.dumps(out["rows"][-1]), flush=True)

    r = call("standin-echo", [{"role": "user", "content": "x" * (2 * 1024 * 1024)}])
    out["rows"].append({"name": "2 MiB prompt", "status": r["status"], "ms": r["total_ms"],
                        "resp_bytes": len(r["body"])})
    print(json.dumps(out["rows"][-1]), flush=True)

    lat = []
    lock = threading.Lock()

    def one():
        t = time.time()
        rr = call("standin-echo", msgs)
        with lock:
            lat.append(((time.time() - t) * 1000, rr["status"]))

    t0 = time.time()
    ts = [threading.Thread(target=one) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    out["rows"].append({"name": "16 parallel chat calls",
                        "wall_ms": round((time.time() - t0) * 1000),
                        "p50": pct([x[0] for x in lat], 0.5),
                        "max": round(max(x[0] for x in lat), 1),
                        "statuses": sorted(set(s for _, s in lat))})
    print(json.dumps(out["rows"][-1]), flush=True)

    r = request("/v1/chat/completions", method="OPTIONS")
    out["rows"].append({"name": "OPTIONS preflight", "status": r["status"],
                        "allow_origin": r["headers"].get("access-control-allow-origin")})
    print(json.dumps(out["rows"][-1]), flush=True)

    r = request("/v1/chat/completions", json.dumps({"model": "standin-bulk", "messages": msgs}),
                key=KEY, extra_headers={"Accept-Encoding": "gzip, br"})
    out["rows"].append({"name": "Accept-Encoding gzip,br", "status": r["status"],
                        "content_encoding": r["headers"].get("content-encoding"),
                        "transfer_encoding": r["headers"].get("transfer-encoding"),
                        "wire_bytes": len(r["body"])})
    print(json.dumps(out["rows"][-1]), flush=True)

    r = call("standin-trickle", msgs, stream=True)
    out["rows"].append({"name": "stream framing", "chunked": r["chunked"],
                        "content_length": r["headers"].get("content-length"),
                        "transfer_encoding": r["headers"].get("transfer-encoding")})
    print(json.dumps(out["rows"][-1]), flush=True)

    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(out, f, indent=1)
    return 0


# --------------------------------------------------------------------- poll

def cmd_poll(a):
    stop = threading.Event()
    sent = {"echo": 0, "trickle": 0, "bulk": 0, "flaky": 0}
    lock = threading.Lock()
    client_lat = []

    def generator():
        msgs = [{"role": "user", "content": "load"}]
        i = 0
        while not stop.is_set():
            i += 1
            try:
                if i % 12 == 0:
                    r, tag = call("standin-bulk", msgs, {"x_bytes": 65536}), "bulk"
                elif i % 6 == 0:
                    r, tag = call("standin-trickle", msgs, {"x_delay_ms": 120},
                                  stream=True), "trickle"
                elif i % 20 == 0:
                    r, tag = call("standin-flaky", msgs, {"x_case": "429"}), "flaky"
                else:
                    r, tag = call("standin-echo", msgs), "echo"
                with lock:
                    sent[tag] += 1
                    client_lat.append((round(r["total_ms"], 1), r["status"]))
            except Exception as exc:  # noqa: BLE001
                with lock:
                    client_lat.append((-1, "EXC " + type(exc).__name__))
            stop.wait(1.0)

    g = threading.Thread(target=generator, daemon=True)
    g.start()
    samples, poll_lat = [], []
    t0 = time.time()
    print("%-8s %6s %5s %5s %6s %6s %8s %8s %s" %
          ("t+s", "reqs", "infl", "max", "p50", "p95", "poll_ms", "bytes", "sent e/t/b/f"))
    while time.time() - t0 < a.duration:
        t = time.time() - t0
        s0 = time.time()
        try:
            r = request("/metrics")
            doc = json.loads(r["body"])
            poll_ms = round((time.time() - s0) * 1000, 1)
        except Exception as exc:  # noqa: BLE001
            print("%-8.0f poll failed: %r" % (t, exc), flush=True)
            time.sleep(a.interval)
            continue
        poll_lat.append(poll_ms)
        with lock:
            s = dict(sent)
        samples.append({"t": round(t, 1), "requests": doc["requests"],
                        "inflight": doc["inflight"], "max_inflight": doc["max_inflight"],
                        "p50": doc["latency_ms"]["p50"], "p95": doc["latency_ms"]["p95"],
                        "poll_ms": poll_ms, "bytes_out": doc["bytes_out"], "sent": s})
        print("%-8.0f %6d %5d %5d %6.1f %6.1f %8.1f %8d %d/%d/%d/%d" %
              (t, doc["requests"], doc["inflight"], doc["max_inflight"],
               doc["latency_ms"]["p50"], doc["latency_ms"]["p95"], poll_ms,
               doc["bytes_out"], s["echo"], s["trickle"], s["bulk"], s["flaky"]), flush=True)
        time.sleep(max(0.0, a.interval - (time.time() - s0)))
    stop.set()
    g.join(timeout=10)

    ok = sorted(x[0] for x in client_lat if x[1] == 200)
    bad = [(s, c) for c, s in client_lat if c != 200]
    summary = {
        "target": TARGET, "duration_s": a.duration, "samples": len(samples),
        "proxy_req_delta": (samples[-1]["requests"] - samples[0]["requests"]) if len(samples) > 1 else 0,
        "peak_inflight": max((s["max_inflight"] for s in samples), default=0),
        "client_calls": len(client_lat), "client_ok": len(ok), "client_bad": bad[:10],
        "client_p50": pct(ok, 0.5), "client_p95": pct(ok, 0.95),
        "poll_rtt_p50": pct(poll_lat, 0.5), "poll_rtt_max": max(poll_lat or [0]),
        "bytes_out": samples[-1]["bytes_out"] if samples else 0, "sent": sent,
    }
    print("\n--- summary ---")
    print(json.dumps(summary, indent=1))
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump({"summary": summary, "samples": samples}, f, indent=1)
    return 0


# --------------------------------------------------------------------- main

def main():
    global TARGET, KEY, HOSTHDR
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("suite", cmd_suite, "16 checks, exit 1 on any failure"),
                               ("bench", cmd_bench, "latency, throughput, limits"),
                               ("poll", cmd_poll, "live counters under light load")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--target", required=True,
                       help="unix:/path.sock, http://host, https://host")
        p.add_argument("--key", default="")
        p.add_argument("--json-out", default="")
        p.add_argument("-n", type=int, default=25)
        p.add_argument("--interval", type=float, default=2.0)
        p.add_argument("--duration", type=float, default=120.0)
        p.set_defaults(func=fn)
    a = ap.parse_args()
    TARGET, KEY = a.target, a.key
    if not TARGET.startswith("unix:") and "://" not in TARGET:
        TARGET = "https://" + TARGET          # a bare host is the common typo
    if not TARGET.startswith("unix:"):
        HOSTHDR = TARGET.split("//", 1)[1].partition("/")[0]
    if not KEY and a.cmd != "poll":
        print("warning: no --key given", file=sys.stderr)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
