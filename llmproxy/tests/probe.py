#!/usr/bin/env python3
"""Measure one llmproxy endpoint, over a unix socket or over HTTPS.

Same suite either way, so localhost and tunnel numbers are comparable.
  python3 tests/probe.py --target unix:/workspace/llmproxy/run/proxy.sock --key K
  python3 tests/probe.py --target https://x.trycloudflare.com --key K --json-out out.json
"""

import argparse
import hashlib
import json
import socket
import ssl
import sys
import threading
import time
import uuid

TARGET = None
KEY = None
HOSTHDR = "localhost"


def connect():
    if TARGET.startswith("unix:"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(120)
        s.connect(TARGET[5:])
        return s, False
    plain = TARGET.startswith("http://")
    hostport = TARGET.split("//", 1)[1].partition("/")[0]
    host = hostport.split(":")[0]
    port = int(hostport.split(":")[1]) if ":" in hostport else (80 if plain else 443)
    infos = [i for i in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
             if i[0] == socket.AF_INET] or socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    raw = None
    err = None
    for fam, typ, proto, _, addr in infos:
        try:
            raw = socket.socket(fam, typ, proto)
            raw.settimeout(30)
            raw.connect(addr)
            break
        except OSError as exc:
            err = exc
            raw = None
    if raw is None:
        raise err
    if plain:
        raw.settimeout(120)
        return raw, False
    ctx = ssl.create_default_context()
    s = ctx.wrap_socket(raw, server_hostname=host)
    s.settimeout(120)
    return s, True


def request(path, body=None, key=None, method=None, extra_headers=None):
    """Return status, headers, body, ttfb_ms, total_ms, chunks [(ms, bytes)]."""
    s, _ = connect()
    method = method or ("POST" if body is not None else "GET")
    payload = b"" if body is None else (body.encode() if isinstance(body, str) else body)
    hs = ["%s %s HTTP/1.1" % (method, path), "Host: " + HOSTHDR, "Connection: close",
          "User-Agent: cfrs-probe/1"]
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
    chunks = []
    while b"\r\n\r\n" not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
        if ttfb is None:
            ttfb = (time.time() - t0) * 1000
    head, _, body_buf = buf.partition(b"\r\n\r\n")
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
    state = ["size"]          # chunked framing: size -> data -> crlf
    left = 0
    raw = body_buf

    def feed(buf, at):
        """Consume de-chunked payload from buf; return the bytes not yet consumed."""
        nonlocal data
        while buf:
            if state[0] == "size":
                i = buf.find(b"\r\n")
                if i < 0:
                    return buf
                size = int(buf[:i].split(b";")[0] or b"0", 16)
                buf = buf[i + 2:]
                if size == 0:
                    state[0] = "done"
                    return buf
                left = size
                state[0] = "data"
                state.append(size)
            elif state[0] == "data":
                n = state[-1]
                take = buf[:n]
                if take:
                    data += take
                    chunks.append((at, len(take)))
                if len(take) < n:
                    state[-1] = n - len(take)
                    return b""
                buf = buf[n:]
                state[0] = "crlf"
            elif state[0] == "crlf":
                if len(buf) < 2:
                    return buf
                buf = buf[2:]
                state[0] = "size"
                state.pop()
            else:
                return b""
        return buf

    if chunked:
        pending = feed(raw, round((time.time() - t0) * 1000, 1))
        while state[0] != "done":
            try:
                c = s.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            pending = feed(pending + c, round((time.time() - t0) * 1000, 1))
    else:
        data = body_buf
        if body_buf:
            chunks.append((round((time.time() - t0) * 1000, 1), len(body_buf)))
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
            "ttfb_ms": round(ttfb or 0, 1),
            "total_ms": round(total, 1), "chunks": chunks, "sse": "event-stream" in hdrs.get("content-type", "")}


def digest_of(prompt):
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def sse_text(body):
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


RESULTS = []


def record(name, ok, **kw):
    row = {"test": name, "ok": bool(ok)}
    row.update(kw)
    RESULTS.append(row)
    print(("PASS " if ok else "FAIL ") + name + "  " +
          "  ".join("%s=%s" % (k, v) for k, v in kw.items() if v is not None), flush=True)


def gaps(chunks):
    if len(chunks) < 2:
        return {"chunks": len(chunks or []), "first_ms": (chunks[0][0] if chunks else None)}
    d = sorted(round(chunks[i][0] - chunks[i - 1][0], 1) for i in range(1, len(chunks)))
    return {"chunks": len(chunks), "gap_p50": d[len(d) // 2], "gap_max": d[-1],
            "first_ms": chunks[0][0], "last_ms": chunks[-1][0]}


def call(model, messages, extra=None, stream=False):
    body = {"model": model, "messages": messages}
    if stream:
        body["stream"] = True
    if extra:
        body.update(extra)
    return request("/v1/chat/completions", json.dumps(body), key=KEY)


def main():
    global TARGET, KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--key", default="")
    ap.add_argument("--json-out")
    ap.add_argument("--label", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--show-body", action="store_true")
    a = ap.parse_args()
    global HOSTHDR
    TARGET, KEY = a.target, a.key
    if not TARGET.startswith("unix:"):
        HOSTHDR = TARGET.split("//", 1)[1].partition("/")[0]
    only = set(x.strip() for x in a.only.split(",") if x.strip())

    def wanted(n):
        return not only or n in only

    msgs = [{"role": "system", "content": "you are a standin"},
            {"role": "user", "content": "prove the tunnel is intact " + uuid.uuid4().hex[:6]}]
    want = digest_of("\n".join(m["content"] for m in msgs))

    if wanted("no_key_401"):
        r = request("/v1/models")
        record("no_key_401", r["status"] == 401, status=r["status"], ms=r["total_ms"])

    if wanted("models"):
        r = request("/v1/models", key=KEY)
        ids = []
        try:
            ids = [m["id"] for m in json.loads(r["body"])["data"]]
        except Exception:
            pass
        record("models", r["status"] == 200 and len(ids) == 4, status=r["status"], ms=r["total_ms"],
               ids=",".join(ids))

    if wanted("echo_nonstream"):
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

    if wanted("echo_stream"):
        r = call("standin-echo", msgs, stream=True)
        txt, frames, done = sse_text(r["body"])
        record("echo_stream", r["status"] == 200 and done and ("DIGEST " + want) in txt,
               status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], frames=frames,
               done=done, digest_ok=("DIGEST " + want) in txt, **gaps(r["chunks"]))

    if wanted("trickle_stream"):
        r = call("standin-trickle", msgs, stream=True)
        txt, frames, done = sse_text(r["body"])
        record("trickle_stream", r["status"] == 200 and done and frames >= 33,
               status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], frames=frames,
               done=done, digest_ok=("DIGEST " + want) in txt, **gaps(r["chunks"]))

    if wanted("trickle_nonstream"):
        r = call("standin-trickle", msgs)
        record("trickle_nonstream", r["status"] == 200 and r["total_ms"] > 1500,
               status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], bytes=len(r["body"]))

    if wanted("bulk_nonstream_256k"):
        r = call("standin-bulk", msgs)
        try:
            blen = len(json.loads(r["body"])["choices"][0]["message"]["content"])
        except Exception:
            blen = 0
        record("bulk_nonstream_256k", r["status"] == 200 and blen == 262144,
               status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], bytes=len(r["body"]),
               content_bytes=blen)

    if wanted("bulk_stream_256k"):
        r = call("standin-bulk", msgs, stream=True)
        txt, frames, done = sse_text(r["body"])
        record("bulk_stream_256k", r["status"] == 200 and done and len(txt) == 262144,
               status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], frames=frames,
               content_bytes=len(txt), **gaps(r["chunks"]))

    if wanted("bulk_nonstream_1m"):
        r = call("standin-bulk", msgs, {"x_bytes": 1048576})
        record("bulk_nonstream_1m", r["status"] == 200 and len(r["body"]) > 1048576,
               status=r["status"], ms=r["total_ms"], ttfb=r["ttfb_ms"], bytes=len(r["body"]))

    if wanted("unknown_model_400"):
        r = request("/v1/chat/completions",
                    json.dumps({"model": "no-such-model", "messages": msgs}), key=KEY)
        record("unknown_model_400", r["status"] == 400, status=r["status"], ms=r["total_ms"])

    for case, want_status, label in (("429", 429, "flaky_429"), ("500", 500, "flaky_500"),
                                     ("400", 400, "flaky_400"),
                                     ("slow-headers", 200, "flaky_slow_headers"),
                                     ("stream-abort", 200, "flaky_stream_abort")):
        if not wanted(label):
            continue
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

    if wanted("concurrency_8_streams"):
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
        ok = len(out) == 8 and all(s == 200 and d and f >= 33 for s, f, d, _ in out)
        record("concurrency_8_streams", ok, wall_ms=wall,
               slowest=max(o[3] for o in out) if out else 0)

    doc = {"target": TARGET, "label": a.label,
           "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "results": RESULTS, "passed": sum(1 for r in RESULTS if r["ok"]),
           "total": len(RESULTS)}
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(doc, f, indent=1)
    print("\n%d/%d passed on %s" % (doc["passed"], doc["total"], TARGET))


if __name__ == "__main__":
    main()
