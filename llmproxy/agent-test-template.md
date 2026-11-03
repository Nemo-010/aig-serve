# Test brief: an OpenAI-shaped API behind a cfrs quick tunnel

**This file is standalone. Everything you need is in it: the target, the key,
the models, the rules, the test harness (Appendix A), and the numbers to
compare against. You do not need any other file, and you do not need the
repository.**

Written 2026-11-03 14:29 UTC for the operator, Ajam.

---

## 0. Mission

Prove two things with numbers, not opinion:

1. **Fidelity.** Does the tunnel carry an OpenAI-compatible HTTP API intact?
   Small JSON, multi-megabyte bodies, streaming, error statuses, slow origins,
   truncated streams, concurrency, auth.
2. **Convenience.** What does a client have to do, what does it cost in
   latency and throughput, and where does it break?

Every claim in your report needs a number you produced. Mark each fact
**measured** (you ran it) or **inferred** (you read it in code or docs). Never
present an inference as a measurement. Never report a fault you have not
reproduced twice.

---

## 1. The target

```
TARGET = https://<your-tunnel>.trycloudflare.com
KEY    = <your-token>
```

That is a Cloudflare quick tunnel in front of a small router, which fronts four
mock models. **No inference runs anywhere** - the models are deterministic
stand-ins. That is deliberate: every millisecond you measure is edge + tunnel +
proxy, with no model in the way.

```sh
export TARGET='https://<your-tunnel>.trycloudflare.com'
export KEY='<your-token>'
```

| Path | Auth | Answer |
| --- | --- | --- |
| `POST /v1/chat/completions` | yes | OpenAI chat completion; `stream: true` gives SSE |
| `GET /v1/models` | yes | the four model ids |
| `GET /healthz` | no | which upstreams are alive |
| `GET /metrics` | no | counters, latency p50/p95, bytes, per model |
| `GET /play` | no | browser playground; the key is typed into the page |
| `OPTIONS` any | no | CORS preflight, `204` |

Auth is `Authorization: Bearer <KEY>` or `x-api-key: <KEY>`. Wrong or missing
key answers `401` with an OpenAI-shaped error object.

Errors look like `{"error": {"message", "type", "code", "param"}}` with a
matching status.

### The four models

| Model id | Behaviour | Knobs |
| --- | --- | --- |
| `standin-echo` | Instant, deterministic, one short paragraph. `stream:true` sends it as a single chunk. | - |
| `standin-trickle` | 32 one-word chunks. The streaming test. | `x_delay_ms` wait between chunks, default 60, clamped 0..1000 |
| `standin-bulk` | A large deterministic body. The throughput test. | `x_bytes`, default 262144, clamped 4096..4194304 |
| `standin-flaky` | One failure per request. The error test. | `x_case`: `ok`, `429`, `500`, `400`, `slow-headers`, `stream-abort` (or a 6-case cycle if omitted) |

The proxy can also forward to a **real** provider. Any model id prefixed
`real/` (e.g. `real/deepseek-v4-1-flash`) is sent upstream verbatim; `GET
/v1/models` lists what the upstream offers. Those calls cost the operator real
money and take seconds, so do not use them in the suite. For scale, one of them
is about 120 ms slower through the tunnel than direct, and a 9 s generation is
unchanged end to end.

Extra fields go in the JSON body next to `model` and `messages`, e.g.
`{"model":"standin-bulk","x_bytes":1048576,"messages":[...]}`.

### The digest: how you prove integrity

Every chat answer ends with `DIGEST <hex16>`, computed as

```python
digest = hashlib.sha256("\n".join(str(m.get("content") or "")
                                   for m in messages).hexdigest()[:16]
```

Recompute it from what **you** sent. If it matches the `DIGEST` in the answer,
the tunnel neither mangled nor truncated the request. If it does not, that is
the bug, and you should say exactly which case broke it.

---

## 2. Two traps that will make you file a false bug

Both were hit and diagnosed on 2026-11-03.

**1. The edge re-chunks every answer.** Responses carry
`Transfer-Encoding: chunked` and **no** `Content-Length`. If your client reads
the body as one blob, chunk-size lines appear inside the SSE text and it looks
like corruption. It is not: de-chunk. The harness in Appendix A does it, and
so does `curl` automatically. Symptom of this mistake: SSE frames parse as
garbage, frame counts come out short, and a `curl` run of the same call looks
perfect.

**2. `Host` must be the tunnel name.** A client that sends `Host: something-
else` gets a **403** from the Cloudflare edge (a 151-byte page), not a routing
error. If every request 403s while `curl "$TARGET/healthz"` is fine, your
client is overriding `Host`.

Two more that look like bugs and are not:

* **`429` with an empty body** and the header
  `cf-int-tunnel-request-limit-hit: tunnel` is the edge's 200-in-flight cap,
  not the router's. The router's own cap answers with a JSON body and
  `"code": "too_many_inflight"`. Say which one you got.
* **A counter mismatch.** Requests the edge rejects never reach the router, so
  they appear in your client counts and not in `/metrics`.

---

## 3. Smoke test (about 30 seconds)

```sh
curl -sS -o /dev/null -w 'healthz %{http_code} %{time_total}s\n' "$TARGET/healthz"

curl -sS "$TARGET/v1/models" -H "Authorization: Bearer $KEY"

curl -sS -o /dev/null -w 'no key -> %{http_code}\n' "$TARGET/v1/models"

curl -sS "$TARGET/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"standin-echo","messages":[{"role":"user","content":"hello"}]}'
```

Expect: `200`; four model ids; `401`; a completion whose text ends in
`DIGEST <hex16>` equal to `sha256("hello")[:16]` - which is
`2cf24dba5fb0a30e` for the string `hello`.

A streamed call, which should print frames as they arrive:

```sh
curl -sS -N "$TARGET/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"standin-trickle","stream":true,"x_delay_ms":200,
       "messages":[{"role":"user","content":"count to thirty-two"}]}'
```

The frames must arrive about 200 ms apart over ~6.4 s, not all at once at the
end. If they arrive in one burst, that is a real finding: report it with your
timestamps.

---

## 4. The harness (Appendix A)

Save Appendix A to `/tmp/harness.py`. It is a single file, Python 3 stdlib
only, no dependencies, no imports from anywhere else. Three subcommands:

```sh
python3 /tmp/harness.py suite --target "$TARGET" --key "$KEY"   # 16 checks, exit 1 on any failure
python3 /tmp/harness.py bench --target "$TARGET" --key "$KEY" -n 25
python3 /tmp/harness.py poll  --target "$TARGET" --key "$KEY" --interval 2 --duration 120
```

`--target` also accepts `unix:/path/to.sock`, `http://host:port`, or anything
else with an HTTP origin, so the same harness measures a local socket and the
tunnel with no change.

---

## 5. What to verify, and how to judge it

| # | Test | How to judge | Expected |
| --- | --- | --- | --- |
| 1 | Integrity, small | Recompute `sha256(contents)[:16]`; must equal `DIGEST` | match |
| 2 | Integrity, large | Same, with a 2 MiB prompt | match, no truncation |
| 3 | Streaming is real | `x_delay_ms: 200`; inter-frame gaps must be ~200 ms | 33 frames, `[DONE]` last |
| 4 | No buffering | Stream TTFB should be within noise of a non-streamed `echo` call | within ~10 ms |
| 5 | Body shape | `echo` non-stream body parses, `model` echoed, `usage` integers present | pass |
| 6 | Status passthrough | `429` keeps `Retry-After`; `500` keeps its body; `400` keeps a body that is not JSON | pass |
| 7 | Slow origin | `slow-headers` gives first byte after ~5 s | no proxy timeout |
| 8 | Truncation | `stream-abort` gives exactly 5 frames, no `[DONE]`, then EOF | pass |
| 9 | Size | `x_bytes: 4194304` delivers exactly 4194304 content bytes | exact |
| 10 | Throughput | Record KiB/s for 1 MiB and 4 MiB | see section 7 |
| 11 | Concurrency, short | 16-400 parallel short calls | all `200` |
| 12 | Concurrency, long | 220 parallel `x_delay_ms: 200` streams | exactly 200 `200`, 20 edge `429` |
| 13 | Auth | No key `401`, wrong key `401`, `/`, `/play`, `/healthz`, `/metrics` open | pass |
| 14 | Compression | `Accept-Encoding: gzip, br` on a 256 KiB answer | `content-encoding: gzip`, far fewer wire bytes |
| 15 | Convenience | Time the whole flow: playground, curl, and a client that only swaps `base_url` | note every non-obvious step |
| 16 | Live behaviour | Poll `/metrics` under load; `inflight` must rise and fall with your traffic | see section 6 |

Test 12 is expected to hit the cap. It is a documented edge limit, not a defect
to file. If you want the burst shape instead of a spread, fire N slow streams
in threads and poll `/metrics` while they are open: a 40-stream burst reads
`inflight 0 -> 40 -> 0`, which proves the ingress is parallel rather than
queueing.

---

## 6. Polling for live measurements

`/metrics` is public and cheap, so watch the rig while you load it. Never trust
a single reading of a rolling percentile.

```sh
while :; do
  date -u +%H:%M:%S | tr -d '\n'; printf ' '
  curl -sS "$TARGET/metrics"; echo
  sleep 5
done
```

Fields that move: `requests`, `inflight`, `max_inflight`, `latency_ms.p50`,
`latency_ms.p95`, `bytes_out`, `per_model.<model>.n`.

Two things to know before you interpret a series:

* `latency_ms.p50` and `.p95` are computed over a **rolling window of the last
  500 samples**, so they drift as traffic changes. p50 "improving" from 1900 ms
  to 0.7 ms during a run usually means slow streams aged out of the window, not
  that anything got faster. Report the series, not one number.
* `max_inflight` is a high-water mark since the router started. It never falls,
  so it cannot show you the present. Use `inflight` for that.

Other live sources, if you are on the same host as the rig (skip if not):

| Source | Shows |
| --- | --- |
| `/workspace/llmproxy/logs/requests.jsonl` | one line per request: id, key, model, stream, status, ms, bytes |
| `/workspace/.scratch/cfrs-bin/cfrs ps` | tunnel state, PID, age, current URL |
| `/workspace/.scratch/cfrs-bin/cfrs logs llmproxy` | the tunnel's own log |

---

## 7. Baseline from 2026-11-03 (all measured)

Same rig, from a host near the edge colo `baq01`. Your distance to the edge
changes every latency number, so compare shapes and ratios, not absolutes.

| Measurement | Local unix socket | HTTPS origin | Raw WebSocket |
| --- | --- | --- | --- |
| 1-message chat, p50 | 0.3 ms | 99 ms | 173 ms |
| Small GET p50 | 0.1 ms | 103 ms | 167 ms |
| Stream TTFB p50 | 0.3 ms | 99 ms | 171 ms |
| 1 MiB body | 97 MB/s | 2934 KiB/s (356 ms) | 2721 KiB/s (384 ms) |
| 4 MiB body | 71 MB/s | 4404 KiB/s (948 ms) | 3783 KiB/s (1103 ms) |
| 2 MiB prompt | 16 ms | 583 ms | 664 ms |
| Paced stream gaps (60 ms set) | 60.5 ms | 60.3 ms | 60.5 ms |
| 6.4 s stream (200 ms set) | 200.5 ms | 200.3 ms | 200.6 ms |
| 16 parallel short calls | 5 ms wall | 157-182 ms wall | 393 ms wall |
| Response framing | close-delimited | chunked | as sent |
| 16-check suite | 16/16 | 16/16 | 16/16 |

Known limits, measured: 200 requests in flight per quick tunnel (220 long
streams gave exactly 200 `200` and 20 edge `429`); the edge gzips when asked
(267 KB answer, 12.8 KB on the wire); provisioning a *new* quick tunnel is rate
limited per source IP and can cost a 20-minute wait.

---

## 8. Rules

* **The target is ephemeral.** It is a quick tunnel, not a hosted service. If
  `$TARGET/healthz` does not answer, say so and stop. Do not try to provision a
  new tunnel: `cfrs tunnel` on this host is rate limited and you would burn the
  window. Ask the operator for a fresh URL.
* Test only. Do not modify the rig, the keys, or the tunnel, and do not file
  tickets, pull requests, issues or posts anywhere.
* Keep scratch out of `/workspace`; use `/tmp`.
* If you are running on the same sandbox as the rig, three host facts apply:
  `bind()` on any `AF_INET` address fails with `EACCES` (unix sockets only, so
  never start a TCP listener); long-lived processes must be started with
  `setsid`; and `pkill -f "script.py"` kills your own shell, so use a bracket
  pattern like `pkill -f "[p]roxy\.py"`.

---

## 9. Report format

Short. A human reads this, not another agent.

1. **Verdict table**: one row per question in section 0, with the number behind
   it and the mark `measured` or `inferred`.
2. **Live series**: your polling samples - timestamps, request count, in-flight
   peak, latency p50/p95 - and what traffic you were sending.
3. **Where it is inconvenient**: concretely, which step cost the most time,
   which limit surprised you, which error a normal client would misread.
4. **What you did not test**, in one line.
5. **Faults**: each with the command, the expected result and the actual one.

Do not restate this brief. Do not pad. Numbers or nothing.

---

## Appendix A - the harness, complete

Save this to `/tmp/harness.py`. Single file, Python 3 stdlib only.

```sh
cat > /tmp/harness.py <<'HARNESS_EOF'
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
HARNESS_EOF
chmod +x /tmp/harness.py
python3 /tmp/harness.py suite --target "$TARGET" --key "$KEY"
```
