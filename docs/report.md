# cfrs quick tunnel in front of an OpenAI-shaped proxy: measurements

When: 2026-11-03, 12:33-13:05 UTC. Host: the sandbox (Artix, uid 966, no root).
cfrs: `0.3.0-beta.8` (release binary, SHA-256 checked against `SHA256SUMS`)
plus a source build of `0.3.0-beta.9` (head `aa93371`, 2m04s, not used).
Edge leg: `quic`, colo `baq01`, one quick tunnel, `--ha-connections 2` (default).

Marks: **measured** = a command ran, **inferred** = read from code or docs,
**not tested** = listed at the end.

## 1. What was built

| Piece | What it does |
| --- | --- |
| `proxy.py` | Unix-socket HTTP server, 360 lines. Bearer auth per key, model → upstream lookup, verbatim forward, unbuffered relay back, JSONL log, counters at `/metrics`, a browser playground at `/play`. |
| `standins/*.py` | Four mock OpenAI models on their own unix sockets: `echo` (instant), `trickle` (32 chunks, `x_delay_ms`), `bulk` (`x_bytes` up to 4 MiB), `flaky` (429 / 500 / 400 / 5 s of silence / stream that dies). Every answer ends with `DIGEST <sha256-16 of the joined prompt>`, so the caller can prove the tunnel did not mangle anything. |
| `tests/probe.py` | 16 checks, one suite that runs over a unix socket, `http://` or `https://`. |
| `tests/bench.py` | Latency distributions, throughput, a 6.4 s stream, 2 MiB prompt, 16 parallel calls, CORS, compression, framing. |

Why mock models and not real inference: the question is what the *plumbing*
costs. With standins, latency is edge + tunnel + proxy and nothing else, and
the digest proves integrity. **inferred** (design)

## 2. Provisioning

| Fact | Result | Mark |
| --- | --- | --- |
| First `cfrs tunnel` at 12:33 | `HTTP 429 (Retry-After: 1166 s): error code: 1015` | measured |
| Same call at 12:45:23 | `Retry-After: 456 s`, and `the quick-tunnel service asks to wait 456 s, more than the 299 s left` → cfrs exits 69 instead of waiting | measured |
| Retry at 12:53:25 (window over) | ready in 6 attempts; URL verified `status=200` at 12:53:28; `{"leg":"quic","locations":["baq01"],"verified":true}` | measured |
| Rapid repeats extend the ban? | No: 1166 s → 456 s → 441 s, i.e. a fixed expiry, not a rolling penalty for asking | measured |
| `--check` before provisioning | Validates both origins and the legs, calls no API, writes nothing — the safe way to test flags | measured |
| New name resolvable at once? | No. cfrs warned `the system resolver of this host does not resolve the new name yet`; a `getaddrinfo` in the same seconds returned AAAA only, so a client got `OSError 101 Network is unreachable` for ~10-40 s | measured |

## 3. Two ways in, both work

`cfrs tunnel --unix <proxy.sock> --forward unix:<proxy.sock>` gives one URL
and two transports:

| | HTTPS origin (plain `https://…/v1/…`) | Raw stream (`wss://…/__cfrs/ws` + `cfrs connect`) |
| --- | --- | --- |
| Client needs | curl / any HTTP client, nothing installed | the `cfrs` binary on the client |
| 16/16 probe suite | pass | pass |
| Latency p50 (1 msg chat) | 99.1 ms | 173.4 ms |
| Latency overhead vs local | +99 ms | +173 ms |
| 1 MiB body | 2934 KiB/s | 2721 KiB/s |
| 4 MiB body | 4404 KiB/s | 3783 KiB/s |
| Streaming pacing | preserved (60.3 ms median gap for 60 ms server pacing) | preserved (60.5 ms) |
| Framing | edge re-chunks (`Transfer-Encoding: chunked`, no `Content-Length`) | as the origin sent it |
| Extra risk | the edge limits (§5) | one WebSocket per connection (**inferred** from `cfrs connect --help`) |

All measured. Note the raw path costs ~70 ms more per request here, because
each client connection opens its own WebSocket and pays the handshake again.
It buys nothing for plain HTTP, and everything if you need a protocol the
edge would not carry.

## 4. Does SSE work through a quick tunnel?

**It does, right now.** `docs/protocol.md` §7 says a quick tunnel has no
Server-Sent Events, and the README repeats it. Measured, the opposite:

| Test | Result | Mark |
| --- | --- | --- |
| `stream:true`, 32 chunks at 60 ms | 33 frames, gap p50 **60.3 ms**, gap max 64.3 ms, `[DONE]` present, digest correct | measured |
| 6.4 s stream (32 × 200 ms) | 33 frames, gap p50 200.3 ms, `[DONE]`, not cut | measured |
| 220 × 6.4 s SSE streams at once | 200 completed with `[DONE]` | measured |
| Time to first frame, stream vs not | 99.2 ms vs 99.1 ms — no buffering penalty | measured |

So the documented limit did not reproduce. What the client must handle is
`Transfer-Encoding: chunked`: the edge re-frames the answer and drops
`Content-Length`. A reader that treats the body as one blob (my first probe
did) sees chunk-size lines inside the SSE text and calls it corrupt. That is
a tooling trap, not a tunnel fault — measured, and fixed in `tests/probe.py`.

## 5. The one hard limit that bites

| Load | Result | Mark |
| --- | --- | --- |
| 250 short completions at once (barrier-started) | 250 × 200, wall 1257 ms | measured |
| 400 short completions at once | 400 × 200, wall 839 ms | measured |
| **220** long streams (6.4 s each) | **200 × 200, 20 × 429**, wall 7142 ms | measured |
| 230 long streams | 200 × 200, 30 × 429 | measured |

The 429 is the edge's, not the proxy's: empty body, `content-length: 0`, and
a header that names the reason — `cf-int-tunnel-request-limit-hit: tunnel`.
200 in flight is exactly the documented number, and it counts **every** path
on the tunnel, so long-lived SSE streams are what exhaust it. A real chat
front end with a user typing streams will hit this at 200 concurrent
conversations; short request/response traffic hides it.

## 6. What the tunnel preserves

Each of these is a `flaky` standin case, measured through the HTTPS origin:

| Upstream behaviour | At the client |
| --- | --- |
| `429` + `Retry-After: 3` | `429`, `retry_after: 3`, 125-byte JSON error, 94 ms |
| `500` with a JSON error body | `500`, 117 bytes, 112 ms |
| `400` whose body is not JSON | `400`, 112 bytes, still not JSON |
| 5 s of silence before the status line | 5093 ms to first byte, then a normal 200 (no proxy timeout) |
| Stream ends after 5 chunks, no `[DONE]` | exactly 5 frames, no `[DONE]`, close — the client can tell it was cut |
| `stream:true` | `content-type: text/event-stream` intact |

The request side is unchanged too: a 2 MiB prompt traversed in 583 ms and
the digest of the 2 MiB prompt matched what the standin computed, so no
truncation and no rewriting.

## 6b. Correction: the per-request cost depends on connection reuse

The suite and bench open a new TLS connection for every request (`Connection:
close`), so their ~99 ms figure is a **cold-connection** number. Measured
afterwards, on one kept-alive connection:

| Measurement | Cold connection | Warm connection |
| --- | --- | --- |
| 1-message chat p50 | 99 ms | **50.8 ms** (p95 71.7, min 41.7, n=20) |
| Stream TTFB p50 | 99 ms | **54.7 ms** (n=6) |
| curl breakdown | dns 12 ms, tcp 24 ms, TLS 43-54 ms, ttfb 114-123 ms | TLS 0 ms on reuse |

So roughly **half** of the 99 ms was the TLS handshake to the edge, paid per
connection and not per request. A client with a connection pool (the OpenAI
SDKs use httpx, which pools) sees ~50 ms, not ~99 ms. The edge keeps the
downstream connection alive even though the origin answers `Connection: close`,
because it decouples the two sides.

## 7. Convenience, honestly

What is good:

* One URL, ordinary HTTPS, ordinary `Authorization: Bearer`. Any OpenAI
  client works by setting `base_url`; no daemon and no client install.
* `/play` is a one-page browser client with a streamed token display and a
  ping button, so "does it work" is one click. Measured: 200, 3093 bytes,
  282 ms.
* The edge gzips what the client asks for: a 267 KB answer went over the
  wire as 12.8 KB. Measured.
* Errors, delay and truncation survive verbatim, so debugging through the
  tunnel is bearable.
* CORS preflight answers 204, so browser clients on other origins work.

What costs:

* ~50 ms per request added on a pooled connection, ~99 ms when the client
  opens a fresh connection each time. A chat client that makes three calls per
  turn pays 150-300 ms of pure overhead. Measured.
* The 200-in-flight cap is shared by the whole tunnel, streaming included.
* The URL is ephemeral. `cfrs tunnel` re-provisions and gets a new name after
  a crash or a restart, and the client's `base_url` breaks. `--detach` plus
  the supervisor keeps it alive, but a new name means telling the user again.
* Provisioning is rate limited by source IP (`error code: 1015`). On this
  host, a window of ~20 minutes opened only after a 19.5-minute wait, and
  cfrs gives up when `Retry-After` exceeds its 299 s budget, so a scripted
  retry loop is required.
* A client that sets `Host:` to anything other than the tunnel name gets
  **403** from the edge (151-byte page), not a routing error. Measured while
  my probe sent `Host: target`.
* `/metrics` and `/healthz` are public on the URL. Fine for a test rig; a
  real deployment would gate them.

## 8. Not tested

Named tunnels (`--token`, `--credentials-file`) — no Cloudflare account
here. `--transport relay` and `--transport warp` — `doctor` says both are
reachable from this host but the tunnel took `quic` and stayed on it.
`cfrs share` and `cfrs relay pair`. Bodies above 4 MiB. A tunnel surviving
an edge leg failure mid-stream. IPv6-only clients. Long-idle connections.

## 9. With a real model behind it (added after review)

The earlier sections measure the pipe with mocks. That answers "does the tunnel
carry SSE", not "what does a real model feel like through it". This section
fixes that. Upstream: `https://kenari.id/v1`, the provider this agent runs on.
The key allows exactly four models (`deepseek-v4-1-flash`, `glm-5-3-flash` and
their `:free` variants); everything else answers `400 model ... is not allowed
by this key`. **measured**

`real/<model-id>` on the proxy routes to that upstream. The key is read from
the environment and never logged.

### 9.1 Where the added time actually goes

Fixed payload, no model time: the provider is asked for a model that does not
exist, so it answers a small JSON error. That isolates TLS + request + response
and removes model jitter entirely, n=12 each. **measured**

| Path | p50 | delta |
| --- | --- | --- |
| Direct to the provider | 0.314 s | — |
| My proxy over a local unix socket | 0.322 s | **+8 ms** |
| Proxy + quick tunnel, cold connection | 0.446 s | **+123 ms** |
| Proxy + raw WebSocket forward | 0.535 s | **+221 ms** |

So the proxy itself costs ~8 ms, the tunnel costs ~123 ms per cold connection
(~50 ms of that is the TLS handshake, measured in §6b), and the raw WebSocket
path is ~90 ms *worse* than the HTTPS origin rather than better.

### 9.2 Against a real generation

Reasoning model, 900-token budget, 902 frames, ~9 s of streaming.**measured**

| | TTFT | Total | Frames | Tokens |
| --- | --- | --- | --- | --- |
| Direct | 2.89 s | 9.42 s | 902 | 900 |
| Tunnel | 2.57 s | 9.15 s | 902 | 900 |

Indistinguishable. Ten interleaved direct/tunnel pairs on the same model gave
a median delta of **+109 ms** with a range of -1.96 s to +1.85 s: the model's
own jitter is 20x the effect being measured, so the honest statement is that
the tunnel's cost is not resolvable above model noise on multi-second
generations.

One visible difference: frames arrive in bursts every ~12.6 ms through the
tunnel (p50, max 159 ms) instead of back-to-back as they do direct. Total time
is unchanged, so it is coalescing, not delaying.

A reasoning model streams `delta.reasoning` / `reasoning_content` for hundreds
of frames before any `delta.content` appears. A client that only reads
`content` sees nothing for seconds and calls it a hang. That is a client bug,
and my own first parser had it. **measured**

### 9.3 What this changes

At 2-9 s generation times, 123 ms is 1-5% of a turn. The tunnel is not what
makes this API inconvenient; the 200-in-flight cap, the ephemeral URL and the
provisioning window are.
