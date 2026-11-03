# aig-serve

Two ways to put a real model behind a `cfrs` quick tunnel, and the instruments
used to measure both.

| Path | What it is | Language |
| --- | --- | --- |
| [`relay/`](relay/) | A minimal **passthrough** proxy for real providers, with `cfrs` as a supervised child. Daemonises, self-heals, streams answers without buffering them. | Rust, 1.8 MB, 4 dependencies |
| [`llmproxy/`](llmproxy/) | A measurement rig: an OpenAI-shaped router, four deterministic standin models, and five instruments (`probe`, `bench`, `poll`, `harness`, `realbench`). | Python 3, stdlib only |
| [`docs/report.md`](docs/report.md) | What the tunnel actually does to an API: latency, throughput, streaming, the 200-in-flight cap, and where the numbers were wrong at first. | markdown |
| [`docs/findings.md`](docs/findings.md) | The `cfrs` issues this work produced, with the evidence behind each. | markdown |

Both were built for hosts with strong limits: no `AF_INET` bind, no root, no
account, no installable packages. Everything talks over `AF_UNIX`.

## The shape

```
client --HTTPS--> Cloudflare edge --cfrs quick tunnel--> unix socket --> relay --> provider
```

`relay` is the one you would deploy. `llmproxy` is the one that tells you
whether the tunnel is lying to you.

## Quick start (relay)

```sh
cd relay
cargo build --release        # Rust 1.89+, a C compiler for ring

KENARI_API_KEY=... \
  ./target/release/relay --daemon \
    --cfrs /path/to/cfrs \
    --socket run/relay.sock --keys keys.txt \
    --url-file run/url.txt --pidfile run/relay.pid --logfile logs/relay.log

cat run/url.txt              # the public URL, rewritten when it changes
curl --unix-socket run/relay.sock http://localhost/healthz
```

```
$ printf 'relay-<random>\n' > keys.txt && chmod 600 keys.txt
$ curl "$(cat run/url.txt)/v1/chat/completions" \
    -H "Authorization: Bearer $(cat keys.txt)" \
    -H 'Content-Type: application/json' \
    -d '{"model":"deepseek-v4-1-flash","stream":true,
         "messages":[{"role":"user","content":"hi"}]}'
```

## Quick start (llmproxy)

```sh
cd llmproxy
printf 'test-token\n' > keys.txt && chmod 600 keys.txt   # caller tokens, one per line

./up.sh                                    # four standins + the router, on unix sockets
python3 tests/harness.py suite --target unix:/workspace/llmproxy/run/proxy.sock --key test-token
./tunnel.sh                                # provision the cfrs tunnel, writes run/url.txt
python3 tests/harness.py suite --target "$(cat run/url.txt)" --key test-token
```

`proxy.py` reads `keys.json`; for the rig, `keys.json` is
`{"keys":[{"key":"test-token","name":"local","max_inflight":32}]}`.

## What is measured here, and what is not

Everything in `docs/report.md` is measured on one host, marked **measured** or
**inferred**. The short version:

- streaming works through a quick tunnel at the origin's own pace (60 ms pacing
  survives as 60.3 ms gaps), despite the documented "no SSE";
- the edge re-chunks every answer, so a client that reads a body as one blob
  sees corruption that is not there;
- 200 requests in flight is the hard limit, reproduced exactly with 220 long
  streams;
- the tunnel costs ~123 ms per cold connection, ~50 ms warm, and against a real
  2-9 s generation that is 1-5% — inside the model's own jitter;
- both proxies are unmeasurable against a real provider at ±3 s per call.

## Credentials

None in this repository, by construction:

- provider keys are read from the environment (`KENARI_API_KEY`,
  `COMMANDCODE_API_KEY`) and never logged, echoed or written;
- caller tokens live in untracked files (`keys.txt`, `keys.json`);
- the tunnel URL is written to `run/url.txt` at runtime and is untracked,
  because a quick tunnel gets a new name every time it is provisioned;
- `llmproxy/agent-test-template.md` is the agent brief with `<your-token>` and
  `<your-tunnel>` in place of a live target;
  `llmproxy/make_agent_test.py` fills them in on a running host.

## License

0BSD. See [LICENSE](LICENSE).
