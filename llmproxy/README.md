# llmproxy: an OpenAI-shaped front door, tunnelled out with `cfrs`

A test rig for two questions: how well do `cfrs` quick tunnels carry an
OpenAI-compatible HTTP API, and how convenient is that API to use?

Nothing here runs inference. The four "models" are **standins**: small mock
servers that answer deterministically, so every number below is the cost of
the plumbing and nothing else.

```
client --HTTPS--> Cloudflare edge --quick tunnel--> cfrs --> proxy.sock --> llmproxy
                                                                        |--> echo.sock
                                                                        |--> trickle.sock
                                                                        |--> bulk.sock
                                                                        '--> flaky.sock
```

## Layout

| Path | What it is |
| --- | --- |
| `proxy.py` | The router: bearer auth, model lookup, verbatim forward, unbuffered relay back, JSONL request log, counters at `/metrics`. |
| `standins/echo.py` | Instant, one paragraph, ends with `DIGEST <sha256-16 of the joined message contents>`. |
| `standins/trickle.py` | 32 one-word chunks, `x_delay_ms` between them (default 60). The streaming test. |
| `standins/bulk.py` | Big bodies, `x_bytes` (default 262144, max 4194304). The throughput test. |
| `standins/flaky.py` | A failure rotation: `429`, `500`, `400`, 5 s of silence before the status line, a stream that dies after 5 chunks. |
| `tests/probe.py` | One suite, run against a unix socket or over HTTPS/HTTP, so local and tunnelled numbers compare directly. |
| `SPEC.md` | The contract the standins follow. |

## Up

```sh
./up.sh                        # four standins + the proxy
python3 tests/probe.py --target unix:/workspace/llmproxy/run/proxy.sock --key "$KEY"
```

`up.sh` starts everything with `setsid`, because a plain `&` child is reaped
here. Keys live in `keys.json` (mode 0600).

## Tunnel

```sh
cfrs tunnel --unix /workspace/llmproxy/run/proxy.sock \
            --forward unix:/workspace/llmproxy/run/proxy.sock
```

One quick tunnel, two ways in:

* **HTTPS origin** (`https://<name>.trycloudflare.com/v1/chat/completions`):
  ordinary HTTPS, no client needed, subject to the edge's quick-tunnel rules.
* **Raw stream** (`wss://<name>.trycloudflare.com/__cfrs/ws`): the same bytes
  over a WebSocket, so a client that runs
  `cfrs connect wss://<name>.trycloudflare.com/__cfrs/ws -L tcp://127.0.0.1:9100`
  gets a local port that behaves like a socket next to the proxy.

## Auth

Every `/v1/*` call needs `Authorization: Bearer <key>` (or `x-api-key`).
`/`, `/healthz` and `/metrics` do not. The key caps concurrent requests per
account; over the cap the answer is `429` with `Retry-After`.

## Endpoints

| Method | Path | Auth | Answer |
| --- | --- | --- | --- |
| POST | `/v1/chat/completions` | yes | OpenAI chat completion, `stream:true` gives SSE |
| GET | `/v1/models` | yes | the four standins |
| GET | `/healthz` | no | which upstreams have a socket |
| GET | `/metrics` | no | counters, latency percentiles, bytes out |
| GET | `/` | no | a one-page description |
| OPTIONS | any | no | CORS preflight |
