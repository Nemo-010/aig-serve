#!/usr/bin/env python3
"""Compare a real model direct against the same model through the tunnel.

Same request body both ways, so the difference is the path and nothing else.

  python3 tests/realbench.py --model deepseek-v4-1-flash --n 5
  python3 tests/realbench.py --model deepseek-v4-1-flash --long 900
"""

import argparse
import json
import os
import socket
import ssl
import statistics
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import probe  # noqa: E402

DIRECT_HOST = "kenari.id"


def parse_sse(body):
    """content chars, reasoning chars, frames, done, usage, per-frame byte chunks."""
    content, reason, frames, done, usage = [], [], 0, False, {}
    for line in body.decode("utf-8", "replace").split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if p == "[DONE]":
            done = True
            continue
        try:
            doc = json.loads(p)
        except ValueError:
            continue
        frames += 1
        if doc.get("usage"):
            usage = doc["usage"]
        for ch in doc.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            r = d.get("reasoning_content") or d.get("reasoning")
            if r:
                reason.append(r)
    return {"content": "".join(content), "reasoning": "".join(reason),
            "frames": frames, "done": done, "usage": usage}


def direct_stream(model, messages, max_tokens):
    """Talk to the provider with no proxy in the way."""
    body = json.dumps({"model": model, "stream": True, "max_tokens": max_tokens,
                       "messages": messages}).encode()
    ctx = ssl.create_default_context()
    s = ctx.wrap_socket(socket.create_connection((DIRECT_HOST, 443), timeout=30),
                        server_hostname=DIRECT_HOST)
    s.settimeout(300)
    t0 = time.time()
    s.sendall((f"POST /v1/chat/completions HTTP/1.1\r\nHost: {DIRECT_HOST}\r\n"
               f"Authorization: Bearer {os.environ['KENARI_API_KEY']}\r\n"
               "Content-Type: application/json\r\nAccept: text/event-stream\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
    buf, ttfb, frames, first, text, reason = b"", None, 0, None, [], []
    usage = {}
    gaps = []
    while True:
        try:
            c = s.recv(65536)
        except socket.timeout:
            break
        if not c:
            break
        now = time.time() - t0
        if ttfb is None:
            ttfb = now
        buf += c
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            p = line[5:].strip()
            if p == b"[DONE]":
                continue
            try:
                doc = json.loads(p)
            except ValueError:
                continue
            frames += 1
            if first is None:
                first = now
            gaps.append(now)
            if doc.get("usage"):
                usage = doc["usage"]
            for ch in doc.get("choices") or []:
                d = ch.get("delta") or {}
                piece = d.get("content")
                if piece:
                    text.append(piece)
                r = d.get("reasoning_content") or d.get("reasoning")
                if r:
                    reason.append(r)
    total = time.time() - t0
    s.close()
    return {"ttfb": ttfb or 0, "first_delta": first or 0, "total": total, "frames": frames,
            "chars": len("".join(text)), "reasoning_chars": len("".join(reason)),
            "tokens": usage.get("completion_tokens") or 0,
            "gaps": [round(gaps[i] - gaps[i - 1], 3) for i in range(1, len(gaps))]}


def via_tunnel(model, messages, max_tokens):
    """Same request, but the body goes through the proxy and the quick tunnel."""
    r = probe.request("/v1/chat/completions",
                      json.dumps({"model": "real/" + model, "stream": True,
                                  "max_tokens": max_tokens, "messages": messages}),
                      key=probe.KEY)
    d = parse_sse(r["body"])
    ch = r["chunks"]
    # probe chunk timestamps are milliseconds; report seconds like the direct path
    g = [round((ch[i][0] - ch[i - 1][0]) / 1000.0, 3) for i in range(1, len(ch))]
    return {"ttfb": r["ttfb_ms"] / 1000, "first_delta": (ch[0][0] / 1000) if ch else 0,
            "total": r["total_ms"] / 1000, "frames": d["frames"], "chars": len(d["content"]),
            "reasoning_chars": len(d["reasoning"]), "tokens": d["usage"].get("completion_tokens") or 0,
            "done": d["done"], "gaps": g, "status": r["status"]}


def summarize(name, runs):
    ttfb = [r["ttfb"] for r in runs]
    tot = [r["total"] for r in runs]
    chars = [r["chars"] for r in runs]
    frames = [r["frames"] for r in runs]
    toks = [r.get("tokens") or 0 for r in runs]
    tps = (statistics.median(toks) / statistics.median(tot)) if any(toks) else \
          ((statistics.median(chars) / 4) / statistics.median(tot))
    print("%-22s n=%d  ttfb p50 %.2fs (min %.2f max %.2f)  total p50 %.2fs  "
          "frames %.0f  tokens %.0f  tok/s %.1f" %
          (name, len(runs), statistics.median(ttfb), min(ttfb), max(ttfb),
           statistics.median(tot), statistics.median(frames),
           statistics.median(toks), tps))
    return {"runs": len(runs), "ttfb_p50": round(statistics.median(ttfb), 3),
            "ttfb_min": round(min(ttfb), 3), "ttfb_max": round(max(ttfb), 3),
            "total_p50": round(statistics.median(tot), 3),
            "frames_p50": statistics.median(frames), "chars_p50": statistics.median(chars),
            "tok_per_s": round((statistics.median(chars) / 4) / statistics.median(tot), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-1-flash")
    ap.add_argument("--target", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--tokens", type=int, default=200)
    ap.add_argument("--long", type=int, default=0,
                    help="also run one completion this long, to test a long-lived stream")
    ap.add_argument("--paired", type=int, default=0,
                    help="interleave N direct/tunnel pairs and report the delta")
    ap.add_argument("--json-out")
    a = ap.parse_args()

    probe.TARGET, probe.KEY = a.target, a.key
    if not a.target.startswith("unix:"):
        probe.HOSTHDR = a.target.split("//", 1)[1].partition("/")[0]

    msgs = [{"role": "user",
             "content": "Count from 1 to 20, one number per line, then stop."}]
    out = {"model": a.model, "target": a.target, "n": a.n, "tokens": a.tokens}

    print("== direct to %s, no proxy, no tunnel ==" % DIRECT_HOST)
    d = [direct_stream(a.model, msgs, a.tokens) for _ in range(a.n)]
    out["direct"] = summarize("direct", d)

    print("== same request through the quick tunnel ==")
    t = [via_tunnel(a.model, msgs, a.tokens) for _ in range(a.n)]
    out["tunnel"] = summarize("tunnel", t)
    out["tunnel_statuses"] = sorted({r["status"] for r in t})
    out["tunnel_all_done"] = all(r.get("done") for r in t)

    if a.paired:
        print("== interleaved pairs: the model drifts, the pairing removes it ==")
        dd, dt, deltas = [], [], []
        for i in range(a.paired):
            x = direct_stream(a.model, msgs, a.tokens)
            y = via_tunnel(a.model, msgs, a.tokens)
            dd.append(x); dt.append(y)
            deltas.append(y["total"] - x["total"])
            print("  pair %d  direct %.2fs  tunnel %.2fs  delta %+.2fs  (ttfb %.2f / %.2f)"
                  % (i + 1, x["total"], y["total"], y["total"] - x["total"],
                     x["ttfb"], y["ttfb"]), flush=True)
        med = statistics.median(deltas)
        lo, hi = min(deltas), max(deltas)
        print("paired delta p50 %+.3fs  min %+.3fs  max %+.3fs  (n=%d)"
              % (med, lo, hi, len(deltas)))
        out["paired"] = {"n": len(deltas), "delta_p50": round(med, 3),
                         "delta_min": round(lo, 3), "delta_max": round(hi, 3),
                         "direct_total_p50": round(statistics.median([r["total"] for r in dd]), 3),
                         "tunnel_total_p50": round(statistics.median([r["total"] for r in dt]), 3),
                         "chars_equal": [r["chars"] for r in dd] == [r["chars"] for r in dt]}

    if a.long:
        print("== long completion (%d tokens), one run each ==" % a.long)
        dl = direct_stream(a.model, msgs, a.long)
        tl = via_tunnel(a.model, msgs, a.long)
        for name, r in (("direct", dl), ("tunnel", tl)):
            print("%-8s ttfb %.2fs total %.2fs frames %d chars %d done=%s" %
                  (name, r["ttfb"], r["total"], r["frames"], r["chars"], r.get("done", "-")))
            if r["gaps"]:
                g = sorted(r["gaps"])
                print("         inter-chunk gap p50 %.3fs max %.3fs" %
                      (g[len(g) // 2], g[-1]))
        out["long"] = {"tokens": a.long,
                       "direct": {k: v for k, v in dl.items() if k != "gaps"},
                       "tunnel": {k: v for k, v in tl.items() if k != "gaps"}}
        if tl["gaps"]:
            g = sorted(tl["gaps"])
            out["long"]["tunnel_gap_p50"] = g[len(g) // 2]
            out["long"]["tunnel_gap_max"] = g[-1]

    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
