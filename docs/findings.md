# Findings filed against `cfrs`

Every issue below came out of building and running the two tools in this
repository against `cfrs 0.3.0-beta.8` on a host with no `AF_INET` bind, no
root and no Cloudflare account. Each links the measurement that produced it.

Filed 2026-11-03.

## Bugs

| Issue | What happens | Evidence |
| --- | --- | --- |
| [#202](https://github.com/Azathothas/cfrs/issues/202) `connect -L` | the socket file survives exit, and a second instance silently takes the path; the first keeps its WebSocket and is unreachable, and `cfrs ps` lists neither | inode 316 → 319 on a second start; the file is still `srw-------` after both processes exit |
| [#208](https://github.com/Azathothas/cfrs/issues/208) foreground `tunnel` | `SIGTERM` is caught (`SigCgt`) and then ignored while a provisioning retry is pending: 20 minutes orphaned at `PPID=1`, 13 threads in `futex_do_wait`; `SIGKILL` is the only exit. `cfrs connect` honours the same signal, and `PR_SET_PDEATHSIG` works on this host, so the signal was delivered and dropped | a supervisor's `stop` returns while the process still holds its origin |

## Gaps and quality of life

| Issue | Ask | Why it matters here |
| --- | --- | --- |
| [#203](https://github.com/Azathothas/cfrs/issues/203) | the ready document and `cfrs ps` should print the `--forward` WebSocket endpoint | one tunnel serves both an HTTP origin and a raw stream; both scripts had to hard-code `/__cfrs/ws` |
| [#204](https://github.com/Azathothas/cfrs/issues/204) | `share --mode proxy --unix` | the only mode with an always-on PIN cannot run on a host that cannot bind `AF_INET`, which is the host class `cfrs` targets |
| [#205](https://github.com/Azathothas/cfrs/issues/205) | a credential in front of a `tunnel` (`warp proxy` already has `--proxy-user`) | a quick tunnel URL is the only secret and it leaks through chat, logs and screenshots |
| [#209](https://github.com/Azathothas/cfrs/issues/209) | let a child tunnel be named: `--name` requires `--detach`, and a detached instance has its own supervisor | a supervisor that owns the pid cannot list its own tunnel in `cfrs ps`, `logs` or `stop` |
| [#210](https://github.com/Azathothas/cfrs/issues/210) | `CFRS_ERROR_PROGRAM` should carry `retry_after_s` and `retryable`, and a retryable exit code distinct from fatal | honouring a rate limit currently means regexing `Retry-After: N s` out of English prose |

## Datapoint added to an existing issue

[#182](https://github.com/Azathothas/cfrs/issues/182#issuecomment-6605311803)
claims, in item 3, that a quick tunnel does not stream Server-Sent Events. It
does, on this host, at the origin's own pace: 33 frames with 60.3 ms gaps for a
60 ms origin, a 6.4 s stream intact, 17 frames for a 256 KiB body, 902 frames
over 9 s from a real provider, and 200 concurrent 6.4 s streams all reaching
`[DONE]`. The same comment gives a second host for the rate-limit window
(1166 s → 456 s → 441 s over 12 minutes: a fixed expiry, not a rolling penalty).

## What is emphatically not a `cfrs` problem

- **A `Host:` header that is not the tunnel name gets a `403`** with a 151-byte
  page. It looks like an outage; it is the edge routing on the name.
- **The edge re-chunks every answer.** No `Content-Length`, `Transfer-Encoding:
  chunked`. A client that reads a body as one blob sees chunk-size lines inside
  the SSE text and concludes the stream is corrupt. Two of these tools did
  exactly that before the parsers were fixed.
- **A new tunnel name is not resolvable for 10-40 s.** `cfrs` warns about it
  itself. A client using the host resolver can get AAAA-only answers and fail
  with `Network is unreachable` (101) while `curl` to the same name works.
- **200 requests in flight, then `429`** with an empty body and
  `cf-int-tunnel-request-limit-hit: tunnel`. Documented, and reproduced exactly.

## Not filed, deliberately

The one-hour provisioning wall is #182 and needs no second ticket. The
`--ready-timeout` that does not bound provisioning is #182 item 2. Prose error
lines under `--log-format json` are #181 item 2. Each of those keeps its
evidence in the existing thread rather than a duplicate.
