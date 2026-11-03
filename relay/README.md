# relay

A minimal passthrough proxy for **real** LLM providers, with `cfrs` as a
supervised child. No standins, no mock models, no rewriting of answers.

```
client --HTTPS--> Cloudflare edge --quick tunnel--> cfrs --> relay.sock
                                                              |
                                                              +--> kenari.id/v1            (OpenAI shape)
                                                              '--> api.commandcode.ai/...  (OpenAI shape)
```

About 1.8 MB, five modules, four dependencies (`rustls`, `webpki-roots`,
`serde_json`, `libc`). No async runtime: one thread per connection.

## What it does and does not do

| Does | Does not |
| --- | --- |
| forwards the request to a provider and streams the answer back as it arrives | buffer a response, re-frame it, or translate between API shapes |
| picks the provider from `provider/model`, or `x-relay-provider` | invent models, rewrite content, or hold a retry queue |
| authenticates callers with a token file | log the provider key, or put it in an argv |
| starts, watches and restarts `cfrs` | let `cfrs` outlive the daemon (PDEATHSIG) |
| reports health and counters on `/healthz` and `__relay/stats` | serve a web page, a playground or a model list |

Requests are buffered (they must be, to read `model` for routing); responses
never are. That asymmetry is the point.

## Build

```sh
cargo build --release      # target/release/relay
```

Rust 1.89+. `ring` needs a C compiler; no `cmake`, so `aws-lc-rs` is not used.

## Run

```sh
KENARI_API_KEY=... COMMANDCODE_API_KEY=... \
  ./target/release/relay --daemon \
    --socket  run/relay.sock \
    --keys    /workspace/relay/keys.txt \
    --cfrs    /workspace/.scratch/cfrs-bin/cfrs \
    --url-file run/url.txt \
    --pidfile  run/relay.pid \
    --logfile  logs/relay.log
```

Relative paths are resolved against the current directory before the daemon
moves to `/`, so both forms work. `run.sh` does this for you.

A child tunnel cannot be named: cfrs requires `--detach` for `--name`, and a
detached instance has its own supervisor, which is the one thing this design
must not have. The daemon owns the pid instead (cfrs issue #209).

| Flag | Default | Meaning |
| --- | --- | --- |
| `--socket` | `run/relay.sock` | unix socket to listen on; `AF_INET` bind is `EACCES` on this host |
| `--keys` | `keys.txt` | one caller token per line, `#` comments; mode 0600 |
| `--provider` | `kenari` | default provider for a bare model id |
| `--cfrs` | `cfrs` | the binary to supervise |
| `--url-file` | `run/url.txt` | the public URL, rewritten when it changes, removed when the tunnel dies |
| `--pidfile` | `run/relay.pid` | flocked, so a second copy exits 75 |
| `--logfile` | `logs/relay.log` | where the daemon sends stdout/stderr |
| `--no-tunnel` | off | listen only, start no tunnel |
| `--no-forward` | off | do not expose the raw WebSocket endpoint |
| `--daemon` | off | double fork, setsid, stdio to the log, pidfile locked |

## Routing

```sh
# default provider (kenari)
{"model":"deepseek-v4-1-flash", ...}
# explicit, by prefix
{"model":"commandcode/gpt-6-astra", ...}
# explicit, by header (the model id is passed through untouched)
x-relay-provider: commandcode
```

The prefix is stripped before the request is forwarded, and only the `model`
field is rewritten; the rest of the body is the caller's bytes.

## Endpoints

| Path | Auth | Answer |
| --- | --- | --- |
| any path, any method | yes | forwarded to the provider; the answer comes back as it arrives |
| `GET /healthz` | **no** | `ok`, `tunnel`, `cfrs_pid`, `restarts`, `last_error`, `providers` |
| `GET /__relay/stats` | yes | counters, latency p50/p95, bytes, per provider |

`/healthz` is deliberately open: the daemon's own watchdog polls it through the
public URL, and so can yours.

## Self-heal

| Failure | What happens |
| --- | --- |
| `cfrs` exits | restarted with a backoff of 2 s doubling to 60 s |
| `cfrs` ignores `SIGTERM` | it does, while a provisioning retry is pending (cfrs issue #208), so a stop is `SIGTERM`, 5 s, then `SIGKILL` |
| `cfrs` is rate limited (`429`/`1015`) | the `Retry-After` is honoured **up to 300 s**, then polled again — the window has a fixed expiry, so polling notices it opening instead of sleeping an hour past it |
| the public URL stops answering | three misses at 30 s apart kills the child, and the supervisor starts a fresh tunnel |
| the daemon is `SIGKILL`ed | `cfrs` gets `SIGTERM` (`PR_SET_PDEATHSIG`); measured, a child that is waiting out a provisioning retry ignores it and survives as an orphan (cfrs #208), so on the next start a stale child is killed by the same escalation |
| the socket file is stale | unlinked and rebound, unless something is actually listening (then it exits 73) |
| a second copy starts | refused: the pidfile is flocked (exit 75) |
| `SIGTERM`/`SIGINT` | the child is stopped, the socket and pidfile removed |

## Measured on this host (2026-11-03)

Path cost with no model time, ten paired runs:

| Path | p50 delta vs direct |
| --- | --- |
| `relay` (local socket) | **−68 ms** |
| the Python reference proxy | +18 ms |

Both are inside the noise of the provider itself, which jitters by ±3 s per
call; a real generation is 2–9 s, so the proxy is not measurable against it.
What *is* measurable is the tunnel: ~123 ms per cold connection, ~50 ms warm.

End to end through the tunnel, real model: 319 frames, first delta at 2.65 s,
total 4.54 s, `[DONE]` present, and the reasoning frames stream as they arrive.

## Known limits

- One TLS connection per request to the provider (§ above). Pooling is the next
  100 lines and ~40 ms; it is not done.
- A provider key that is valid for `/models` but rejected for completions
  (`commandcode` at the time of writing) fails exactly as the provider says:
  the error body is passed through untouched.
- The tunnel URL is ephemeral. `run/url.txt` is the source of truth and is
  rewritten when the URL changes.
