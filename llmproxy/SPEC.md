# Standin contract (mock OpenAI-compatible model servers)

Sandbox facts that shape this file (measured on this host):

* `bind()` to any `AF_INET` address is `EACCES`. Only `AF_UNIX` sockets work.
* Outbound TCP/443 works; `python3` (stdlib only), `curl`, `cfrs` exist. No `pip`, no `jq`.
* Long-lived processes must be started with `setsid`.

So each standin is an HTTP/1.1 server on a **unix socket** and nothing else.

## Invocation

```sh
python3 standins/<name>.py --socket /workspace/llmproxy/run/<name>.sock
```

* `--socket` (default: `/workspace/llmproxy/run/<name>.sock`). Unlink a stale socket first.
* `chmod 0600` the socket.
* Serve with threads; several requests may be in flight at once.
* Print one line to stderr when listening: `standin <name> listening on <socket>`.
* Flush unbuffered: never buffer a whole response before sending it.

## Endpoints

| Method | Path | Answer |
| --- | --- | --- |
| POST | `/v1/chat/completions` | the completion |
| GET | `/healthz` | `{"ok":true,"standin":"<name>"}` |
| * | anything else | 404, `{"error":{"message":"not found","type":"invalid_request_error","code":"not_found"}}` |

Request body is JSON: `{"model": "...", "messages": [...], "stream": bool, ...}`.
`messages` is a list of `{"role","content"}`; `content` may be a string.

Every response carries `Connection: close`, and the server closes the socket
after the body. A non-stream response has a correct `Content-Length`.

## Digest (integrity check)

The point is to prove the tunnel did not mangle the request. Compute

```python
prompt_text = "\n".join(str(m.get("content") or "") for m in body.get("messages") or [])
digest = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]
```

and put `DIGEST <digest>` in the answer text. The caller can recompute it.

## Non-stream answer

```json
{"id":"chatcmpl-<8 hex>","object":"chat.completion","created":<unix ts>,"model":"<model from request>",
 "choices":[{"index":0,"message":{"role":"assistant","content":"<text>"},"finish_reason":"stop"}],
 "usage":{"prompt_tokens":<int>,"completion_tokens":<int>,"total_tokens":<int>}}
```

## Stream answer

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: close
```
No `Content-Length`, no `Transfer-Encoding`: the body runs to close.

Then, one write+flush per chunk:

```
data: {"id":"chatcmpl-<same>","object":"chat.completion.chunk","created":<ts>,"model":"<model>","choices":[{"index":0,"delta":{"content":"<piece>"},"finish_reason":null}]}

```

Finally one chunk with `"finish_reason":"stop"` and empty delta, then:

```
data: [DONE]

```

## Errors

JSON `{"error":{"message":"...","type":"...","code":"..."}}` with the right status.

## The four standins

| File | Model id | Behaviour |
| --- | --- | --- |
| `standins/echo.py` | `standin-echo` | Instant, deterministic. Explains the request in one short paragraph, ends with `DIGEST`. `stream:true` sends the whole text in a single chunk then `[DONE]`. |
| `standins/trickle.py` | `standin-trickle` | Streams 32 one-word chunks, `x_delay_ms` in the body (default 60, clamp 0..1000) between chunks, so a stream takes ~2 s. Ends with the `DIGEST` line. With `stream:false` it sleeps the same total and answers in one piece. |
| `standins/bulk.py` | `standin-bulk` | Big answers. `x_bytes` in the body (default 262144, clamp 4096..4194304) of deterministic numbered lines. Header `x-standin-bytes: <n>`. Works streamed (16 KiB pieces) and not. |
| `standins/flaky.py` | `standin-flaky` | Deterministic failure cycle, one case per request, chosen by `x_case` (body or header) if given, else `counter % 6`: `ok`, `429` (Retry-After: 3), `500`, `400` malformed-JSON body, `slow-headers` (5 s before the status line), `stream-abort` (5 chunks then close, no `[DONE]`). Header `x-standin-case: <case>`. |

## Style

* Python 3, stdlib only, one file per standin, no shared module.
* No shelling out, no listening on TCP, no threads left running on exit.
* Keep it under ~250 lines; no essay comments.
