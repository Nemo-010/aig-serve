#!/usr/bin/env python3
"""Generate AGENT-TEST.md: a standalone brief for an agent that has nothing else.

Reads run/url.txt and keys.json for the live values and embeds tests/harness.py
so the reader needs no file but this one. Run it again after the tunnel URL
changes.

  python3 make_agent_test.py            # writes AGENT-TEST.md
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def read(path, default=""):
    try:
        with open(os.path.join(HERE, path)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


TEMPLATE = "--template" in __import__("sys").argv
URL = "https://<your-tunnel>.trycloudflare.com" if TEMPLATE else read(
    "run/url.txt", "https://REPLACE-ME.trycloudflare.com")
KEYS = {"keys": []} if TEMPLATE else json.loads(read("keys.json", '{"keys":[]}'))
KEY = "<your-token>" if TEMPLATE else (
    KEYS["keys"][0]["key"] if KEYS.get("keys") else "REPLACE-ME")
HARNESS = read("tests/harness.py")
STAMP = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

PROSE = """# Test brief: an OpenAI-shaped API behind a cfrs quick tunnel

**This file is standalone. Everything you need is in it: the target, the key,
the models, the rules, the test harness (Appendix A), and the numbers to
compare against. You do not need any other file, and you do not need the
repository.**

Written __STAMP__ for the operator, Ajam.

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
TARGET = __URL__
KEY    = __KEY__
```

That is a Cloudflare quick tunnel in front of a small router, which fronts four
mock models. **No inference runs anywhere** - the models are deterministic
stand-ins. That is deliberate: every millisecond you measure is edge + tunnel +
proxy, with no model in the way.

```sh
export TARGET='__URL__'
export KEY='__KEY__'
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
digest = hashlib.sha256("\\n".join(str(m.get("content") or "")
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
curl -sS -o /dev/null -w 'healthz %{http_code} %{time_total}s\\n' "$TARGET/healthz"

curl -sS "$TARGET/v1/models" -H "Authorization: Bearer $KEY"

curl -sS -o /dev/null -w 'no key -> %{http_code}\\n' "$TARGET/v1/models"

curl -sS "$TARGET/v1/chat/completions" \\
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \\
  -d '{"model":"standin-echo","messages":[{"role":"user","content":"hello"}]}'
```

Expect: `200`; four model ids; `401`; a completion whose text ends in
`DIGEST <hex16>` equal to `sha256("hello")[:16]` - which is
`2cf24dba5fb0a30e` for the string `hello`.

A streamed call, which should print frames as they arrive:

```sh
curl -sS -N "$TARGET/v1/chat/completions" \\
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \\
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
  date -u +%H:%M:%S | tr -d '\\n'; printf ' '
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
  pattern like `pkill -f "[p]roxy\\.py"`.

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
__HARNESS__
HARNESS_EOF
chmod +x /tmp/harness.py
python3 /tmp/harness.py suite --target "$TARGET" --key "$KEY"
```
"""


def main():
    body = (PROSE.replace("__STAMP__", STAMP)
                 .replace("__URL__", URL)
                 .replace("__KEY__", KEY)
                 .replace("__HARNESS__", HARNESS))
    out = os.path.join(HERE, "AGENT-TEST.md" if not TEMPLATE else "agent-test-template.md")
    with open(out, "w") as f:
        f.write(body)
    print(out)


if __name__ == "__main__":
    main()
