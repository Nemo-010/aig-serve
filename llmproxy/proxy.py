#!/usr/bin/env python3
"""llmproxy - OpenAI-compatible router in front of mock model backends.

Listens on a unix socket (this host refuses AF_INET binds), checks a bearer
token, picks a standin by model name, forwards /v1/chat/completions verbatim,
and relays the answer back without buffering it.
"""

import argparse
import json
import os
import socket
import stat
import sys
import threading
import time
import uuid

RUN = os.path.dirname(os.path.abspath(__file__)) + "/run"

MODELS = {
    "standin-echo": {"socket": RUN + "/echo.sock", "note": "instant, deterministic"},
    "standin-trickle": {"socket": RUN + "/trickle.sock", "note": "streams 32 chunks at ~60ms"},
    "standin-bulk": {"socket": RUN + "/bulk.sock", "note": "big answers, 256KiB default"},
    "standin-flaky": {"socket": RUN + "/flaky.sock", "note": "failure rotation: 429/500/400/slow/abort"},
}

# A real upstream, if one is configured in the environment. The key is read
# from the env var named here and never logged, echoed or written to disk.
REAL = {
    "base": os.environ.get("REAL_BASE", "https://kenari.id/v1"),
    "key_env": os.environ.get("REAL_KEY_ENV", "KENARI_API_KEY"),
    "prefix": os.environ.get("REAL_PREFIX", "real/"),
}
_models_cache = {"at": 0.0, "data": []}


def read_body_all(sock):
    """Read a whole response body, de-chunking if the peer used chunked framing."""
    buf = b""
    out = b""
    chunked = None
    while True:
        c = sock.recv(65536)
        if not c:
            break
        buf += c
        if chunked is None and b"\r\n\r\n" in buf:
            head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1").lower()
            chunked = "transfer-encoding:" in head and "chunked" in head
    _, _, body = buf.partition(b"\r\n\r\n")
    if not chunked:
        return body
    while True:
        if b"\r\n" not in body:
            break
        line, _, rest = body.partition(b"\r\n")
        try:
            n = int(line.split(b";")[0] or b"0", 16)
        except ValueError:
            break
        if n == 0:
            break
        out += rest[:n]
        body = rest[n + 2:]
    return out


def real_key():
    return os.environ.get(REAL["key_env"], "")


def real_parts():
    """base https://host/v1 -> (host, "/v1")"""
    rest = REAL["base"].split("//", 1)[1]
    host, _, tail = rest.partition("/")
    return host.split(":")[0], ("/" + tail).rstrip("/")


def split_real(model):
    """real/<upstream-id> -> upstream id, else None."""
    if not real_key() or not model.startswith(REAL["prefix"]):
        return None
    return model[len(REAL["prefix"]):]


def real_models(force=False):
    """Models the real upstream lists, cached for a minute."""
    now = time.time()
    if not force and _models_cache["data"] and now - _models_cache["at"] < 60:
        return _models_cache["data"]
    out = []
    try:
        import ssl
        host, base_path = real_parts()
        ctx = ssl.create_default_context()
        raw = socket.create_connection((host, 443), timeout=10)
        tls = ctx.wrap_socket(raw, server_hostname=host)
        tls.settimeout(15)
        tls.sendall(("GET %s/models HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
                     "Connection: close\r\n\r\n" % (base_path, host, real_key())).encode())
        body = read_body_all(tls)
        tls.close()
        doc = json.loads(body)
        out = [m.get("id") for m in (doc.get("data") or []) if m.get("id")]
        _models_cache.update({"at": now, "data": out})
    except Exception as exc:  # noqa: BLE001 - an unreachable upstream is not fatal
        log({"event": "real_models_failed", "error": repr(exc)})
    return out


ALIASES = {
    "echo": "standin-echo", "trickle": "standin-trickle",
    "bulk": "standin-bulk", "flaky": "standin-flaky",
    "auto": "standin-echo", "gpt-4o-mini": "standin-echo",
    "gpt-4o": "standin-trickle", "gpt-3.5-turbo": "standin-echo",
}

MAX_HEAD = 64 * 1024
LOCK = threading.Lock()
STATS = {"requests": 0, "unauthorized": 0, "unknown_model": 0, "upstream_error": 0,
         "inflight": 0, "max_inflight": 0, "lat": [], "bytes_out": 0, "per_model": {}}
LOGPATH = None


def log(rec):
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = json.dumps(rec, separators=(",", ":"))
    if LOGPATH:
        with LOCK:
            with open(LOGPATH, "a") as f:
                f.write(line + "\n")
    sys.stderr.write(line + "\n")


def errbody(msg, typ="invalid_request_error", code=None, status=400):
    return status, {"error": {"message": msg, "type": typ, "code": code,
                              "param": None}}


def fail(cs, status, msg, typ="invalid_request_error", code=None, extra=None):
    _, doc = errbody(msg, typ, code, status)
    send_response(cs, status, doc, extra=extra)


def read_head(sock, deadline=15.0):
    """Read an HTTP head; return (start_line, [(k,v)], raw_head, rest)."""
    sock.settimeout(deadline)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(16384)
        if not chunk:
            if not buf:
                return None
            break
        buf += chunk
        if len(buf) > MAX_HEAD:
            raise ValueError("head too large")
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    start, hdrs = lines[0], []
    for ln in lines[1:]:
        if ":" in ln:
            k, _, v = ln.partition(":")
            hdrs.append((k.strip().lower(), v.strip()))
    return start, hdrs, head, rest


def read_body(sock, hdrs, rest):
    def get(name):
        for k, v in hdrs:
            if k == name:
                return v
        return None
    te = (get("transfer-encoding") or "").lower()
    cl = get("content-length")
    body = rest
    sock.settimeout(60.0)
    if "chunked" in te:
        out = b""
        while True:
            while b"\r\n" not in body:
                body += sock.recv(65536)
            line, _, body = body.partition(b"\r\n")
            n = int(line.split(b";")[0], 16)
            if n == 0:
                while len(body) < 2:
                    body += sock.recv(65536)
                break
            while len(body) < n + 2:
                body += sock.recv(65536)
            out += body[:n]
            body = body[n + 2:]
        return out
    if cl is not None:
        n = int(cl)
        while len(body) < n:
            chunk = sock.recv(min(65536, n - len(body)))
            if not chunk:
                break
            body += chunk
        return body[:n]
    return body


def send_response(cs, status, obj=None, raw=None, extra=None, ctype="application/json"):
    if raw is None:
        raw = json.dumps(obj).encode()
    reasons = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
               404: "Not Found", 413: "Payload Too Large", 429: "Too Many Requests",
               502: "Bad Gateway", 504: "Gateway Timeout", 500: "Internal Server Error"}
    head = ["HTTP/1.1 %d %s" % (status, reasons.get(status, "Status"))]
    if status != 204:
        head.append("Content-Type: " + ctype)
        head.append("Content-Length: %d" % len(raw))
    for k, v in (extra or {}).items():
        head.append("%s: %s" % (k, v))
    head.append("Access-Control-Allow-Origin: *")
    head.append("Connection: close")
    try:
        cs.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + (b"" if status == 204 else raw))
    except OSError:
        pass


PLAY = r"""<!doctype html><meta charset=utf-8><title>llm proxy playground</title>
<style>
body{font:14px/1.6 ui-monospace,SFMono-Regular,monospace;max-width:52rem;margin:3rem auto;padding:0 1rem;color:#111}
textarea{width:100%;min-height:7rem;font:inherit;padding:.6rem;box-sizing:border-box}
input,select,button{font:inherit;padding:.4rem .6rem}
#out{white-space:pre-wrap;border:1px solid #ccc;padding:.8rem;min-height:6rem;margin-top:1rem;background:#fafafa}
#meta{color:#666;font-size:12px;margin-top:.5rem;white-space:pre-wrap}
.row{display:flex;gap:.6rem;align-items:center;margin:.6rem 0;flex-wrap:wrap}
</style>
<h2>llm proxy playground</h2>
<p>Talks straight to this origin, over whatever the tunnel is doing. Nothing is stored; the key stays in this tab.</p>
<div class=row>
  <input id=key placeholder="sk-..." size=44>
  <select id=model><option>standin-echo</option><option>standin-trickle</option>
  <option>standin-bulk</option><option>standin-flaky</option></select>
  <label><input type=checkbox id=stream checked> stream</label>
  <button id=go>send</button>
  <button id=ping>ping /healthz</button>
</div>
<textarea id=prompt>say hello and prove the tunnel is intact</textarea>
<div id=out></div><div id=meta></div>
<script>
const $=id=>document.getElementById(id);
const meta=t=>{$('meta').textContent=t};
$('ping').onclick=async()=>{const t0=performance.now();
 try{const r=await fetch('/healthz',{cache:'no-store'});meta('healthz '+r.status+' in '+
 (performance.now()-t0).toFixed(1)+' ms\n'+await r.text());}catch(e){meta('healthz failed: '+e)}};
$('go').onclick=async()=>{
  const body={model:$('model').value,messages:[{role:'user',content:$('prompt').value}],
              stream:$('stream').checked};
  $('out').textContent='';const t0=performance.now();let first=null,chunks=0,bytes=0;
  try{
    const r=await fetch('/v1/chat/completions',{method:'POST',cache:'no-store',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+$('key').value},
      body:JSON.stringify(body)});
    if(!$('stream').checked){
      const txt=await r.text();$('out').textContent=txt;
      meta('status '+r.status+' · '+(performance.now()-t0).toFixed(0)+' ms · '+txt.length+' bytes');
      return;}
    const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
    while(true){const {value,done}=await rd.read();if(done)break;
      if(first===null)first=performance.now()-t0;chunks++;bytes+=value.length;
      buf+=dec.decode(value,{stream:true});let i;
      while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i).trim();buf=buf.slice(i+1);
        if(!line.startsWith('data:'))continue;const p=line.slice(5).trim();
        if(p==='[DONE]'){buf='';$('meta').textContent+='\n[DONE]';continue;}
        try{const d=JSON.parse(p);for(const c of d.choices||[])
          if(c.delta&&c.delta.content)$('out').textContent+=c.delta.content;}catch(e){}}}
    meta('status '+r.status+' · first byte '+(first||0).toFixed(0)+' ms · total '+
      (performance.now()-t0).toFixed(0)+' ms · '+chunks+' reads · '+bytes+' bytes');
  }catch(e){meta('failed: '+e)}}
</script>
"""


def relay_real(cs, path, body, rid, model, upstream_id):
    """Forward to the real upstream and pump its bytes back unbuffered."""
    import ssl
    host, base_path = real_parts()
    ctx = ssl.create_default_context()
    raw = socket.create_connection((host, 443), timeout=15)
    up = ctx.wrap_socket(raw, server_hostname=host)
    up.settimeout(300)
    up_path = path
    if base_path.endswith("/v1") and up_path.startswith("/v1"):
        up_path = up_path[3:]          # the client already spoke /v1
    head = ["POST %s%s HTTP/1.1" % (base_path, up_path), "Host: " + host,
            "Authorization: Bearer " + real_key(), "Content-Type: application/json",
            "Accept: text/event-stream", "Content-Length: %d" % len(body),
            "Connection: close"]
    t0 = time.time()
    up.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
    parsed = read_head(up, deadline=300.0)
    if parsed is None:
        raise OSError("real upstream closed with no answer")
    start, uh, _, rest = parsed
    status = int(start.split(" ")[1])
    out = ["HTTP/1.1 " + start.split(" ", 2)[1]]
    keep = ("content-type", "content-length", "transfer-encoding", "cache-control",
            "retry-after", "x-request-id")
    for name in keep:
        for k, v in uh:
            if k == name:
                out.append("%s: %s" % (k, v))
    out.append("x-proxy-model: " + model)
    out.append("x-proxy-upstream: real")
    out.append("x-proxy-request-id: " + rid)
    out.append("Access-Control-Allow-Origin: *")
    out.append("Connection: close")
    cs.sendall(("\r\n".join(out) + "\r\n\r\n").encode())
    nbytes = 0
    if rest:
        cs.sendall(rest)
        nbytes += len(rest)
    while True:
        buf = up.recv(65536)
        if not buf:
            break
        cs.sendall(buf)
        nbytes += len(buf)
    try:
        up.close()
    except OSError:
        pass
    return status, nbytes, round((time.time() - t0) * 1000, 1)


class Key(object):
    def __init__(self, entry):
        self.key = entry["key"]
        self.name = entry.get("name", "anonymous")
        self.max_inflight = entry.get("max_inflight", 16)
        self.inflight = 0
        self.calls = 0


class Handler(object):
    def __init__(self, conn, keys):
        self.cs = conn
        self.keys = keys

    def auth(self, hdrs):
        def get(name):
            for k, v in hdrs:
                if k == name:
                    return v
            return ""
        tok = get("authorization")
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        elif not tok:
            tok = get("x-api-key")
        return self.keys.get(tok)

    def run(self):
        try:
            cs = self.cs
            parsed = read_head(cs)
            if parsed is None:
                return
            start, hdrs, _, rest = parsed
            parts = start.split(" ")
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1].split("?")[0]
            if any(k == "expect" and "100-continue" in v.lower() for k, v in hdrs):
                cs.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            body = read_body(cs, hdrs, rest) if method in ("POST", "PUT", "PATCH") else b""

            if method == "OPTIONS":
                return send_response(cs, 204, extra={
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, x-api-key",
                    "Access-Control-Max-Age": "600"})
            if path == "/" and method == "GET":
                return self.index()
            if path == "/play" and method == "GET":
                return send_response(cs, 200, raw=PLAY.encode(),
                                     ctype="text/html; charset=utf-8")
            if path == "/healthz":
                with LOCK:
                    live = {m: os.path.exists(c["socket"]) for m, c in MODELS.items()}
                return send_response(cs, 200, {"ok": all(live.values()), "upstreams": live})
            if path in ("/metrics", "/stats"):
                return self.metrics()

            key = self.auth(hdrs)
            if key is None:
                with LOCK:
                    STATS["unauthorized"] += 1
                return fail(cs, 401, "missing or wrong api key; send "
                            "Authorization: Bearer <key>", "authentication_error",
                            "invalid_api_key")
            if path == "/v1/models" and method == "GET":
                return self.models(key)
            if path in ("/v1/chat/completions", "/v1/completions"):
                return self.completion(key, path, body, method)
            return fail(cs, 404, "no such path: " + path, code="not_found")
        except Exception as exc:  # keep the server alive
            try:
                send_response(self.cs, 500, {"error": {"message": "proxy: %r" % (exc,),
                                                      "type": "proxy_error"}})
            except Exception:
                pass
        finally:
            try:
                self.cs.close()
            except Exception:
                pass

    def index(self):
        html = """<!doctype html><meta charset=utf-8><title>luciole llm proxy</title>
<style>body{font:14px/1.5 ui-monospace,monospace;max-width:46rem;margin:4rem auto;padding:0 1rem}
code{background:#eee;padding:.1rem .3rem}</style>
<h1>llm proxy</h1>
<p>OpenAI-compatible front door to four mock models, tunnelled out through
<code>cfrs</code>. It runs no inference: each model is a standin that answers
deterministically.</p>
<ul>%s</ul>
<p>Send <code>Authorization: Bearer &lt;key&gt;</code>. Endpoints:
<code>GET /v1/models</code>, <code>POST /v1/chat/completions</code>,
<code>GET /healthz</code>, <code>GET /metrics</code>.</p>
<p><a href="/play">Playground</a></p>
<p>Streaming through a quick tunnel: <code>stream:true</code> is answered as an
SSE body with no <code>Content-Length</code>; whether the Edge forwards it
incrementally is what this test measures. Use
<code>cfrs connect</code> on <code>/__cfrs/ws</code> for a raw byte stream.</p>
""" % "".join("<li><code>%s</code> - %s</li>" % (m, c["note"]) for m, c in MODELS.items())
        send_response(self.cs, 200, raw=html.encode(), ctype="text/html; charset=utf-8")

    def metrics(self):
        with LOCK:
            lat = sorted(STATS["lat"][-500:])
            def pct(p):
                if not lat:
                    return 0
                return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1)
            doc = {
                "requests": STATS["requests"],
                "unauthorized": STATS["unauthorized"],
                "unknown_model": STATS["unknown_model"],
                "upstream_error": STATS["upstream_error"],
                "inflight": STATS["inflight"],
                "max_inflight": STATS["max_inflight"],
                "latency_ms": {"n": len(lat), "p50": pct(0.5), "p95": pct(0.95),
                               "max": round(lat[-1], 1) if lat else 0},
                "bytes_out": STATS["bytes_out"],
                "per_model": STATS["per_model"],
            }
        send_response(self.cs, 200, doc)

    def models(self, key):
        data = [{"id": m, "object": "model", "created": 1735689600, "owned_by": "standin",
                 "notes": c["note"]} for m, c in MODELS.items()]
        if real_key():
            for mid in real_models():
                data.append({"id": REAL["prefix"] + mid, "object": "model",
                             "created": 1735689600, "owned_by": "real",
                             "notes": "real upstream: billed per call, and the "
                                      "key may allow only some ids"})
        send_response(self.cs, 200, {"object": "list", "data": data})

    def real_completion(self, key, path, body, req, want, upstream_id):
        with LOCK:
            if key.inflight >= key.max_inflight:
                return fail(self.cs, 429, "this key already has %d requests in flight"
                            % key.inflight, "rate_limit_error", "too_many_inflight",
                            {"Retry-After": "1"})
            key.inflight += 1
            key.calls += 1
            STATS["requests"] += 1
            STATS["inflight"] += 1
            STATS["max_inflight"] = max(STATS["max_inflight"], STATS["inflight"])
            pm = STATS["per_model"].setdefault(want, {"n": 0, "err": 0, "bytes": 0})
            pm["n"] += 1
        rid = uuid.uuid4().hex[:12]
        try:
            req2 = dict(req)
            req2["model"] = upstream_id
            status, nbytes, ms = relay_real(self.cs, path, json.dumps(req2).encode(),
                                            rid, want, upstream_id)
            with LOCK:
                STATS["lat"].append(ms)
                STATS["bytes_out"] += nbytes
                pm["bytes"] += nbytes
                if status >= 400:
                    STATS["upstream_error"] += 1
                    pm["err"] += 1
            log({"id": rid, "key": key.name, "model": want, "upstream": "real:" + upstream_id,
                 "stream": bool(req.get("stream")), "status": status, "ms": ms,
                 "bytes": nbytes, "messages": len(req.get("messages") or [])})
        except OSError as exc:
            with LOCK:
                STATS["upstream_error"] += 1
                pm["err"] += 1
            try:
                send_response(self.cs, 502, {"error": {
                    "message": "real upstream: %r" % (exc,), "type": "upstream_error"}})
            except OSError:
                pass
            log({"id": rid, "key": key.name, "model": want, "upstream": "real",
                 "status": 502, "error": repr(exc)})
        finally:
            with LOCK:
                key.inflight -= 1
                STATS["inflight"] -= 1

    def completion(self, key, path, body, method):
        try:
            req = json.loads(body or b"{}")
        except ValueError as exc:
            return fail(self.cs, 400, "body is not JSON: %s" % exc, code="invalid_json")
        want = str(req.get("model") or "")
        upstream_id = split_real(want)
        if upstream_id:
            return self.real_completion(key, path, body, req, want, upstream_id)
        model = ALIASES.get(want, want)
        if model not in MODELS:
            with LOCK:
                STATS["unknown_model"] += 1
            return fail(self.cs, 400, "unknown model %r; try one of: %s"
                        % (want, ", ".join(MODELS)), code="model_not_found")

        with LOCK:
            if key.inflight >= key.max_inflight:
                return fail(self.cs, 429, "this key already has %d requests in flight"
                            % key.inflight, "rate_limit_error", "too_many_inflight",
                            {"Retry-After": "1"})
            key.inflight += 1
            key.calls += 1
            STATS["requests"] += 1
            STATS["inflight"] += 1
            STATS["max_inflight"] = max(STATS["max_inflight"], STATS["inflight"])
            pm = STATS["per_model"].setdefault(want or model, {"n": 0, "err": 0, "bytes": 0})
            pm["n"] += 1
        rid = uuid.uuid4().hex[:12]
        t0 = time.time()
        up = None
        try:
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.settimeout(5.0)
            up.connect(MODELS[model]["socket"])
            head = ["%s %s HTTP/1.1" % (method, path),
                    "Host: standin",
                    "Content-Type: application/json",
                    "Content-Length: %d" % len(body),
                    "Connection: close"]
            # keep stream intent visible to the upstream
            up.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
            parsed = read_head(up, deadline=max(30.0, 5.0))
            if parsed is None:
                raise OSError("upstream closed with no answer")
            start, uh, _, urest = parsed
            status = int(start.split(" ")[1])
            if status >= 400:
                with LOCK:
                    STATS["upstream_error"] += 1
                    pm["err"] += 1

            def uget(name):
                for k, v in uh:
                    if k == name:
                        return v
                return None
            out = ["HTTP/1.1 " + start.split(" ", 2)[1]]
            for name in ("content-type", "content-length", "cache-control",
                         "retry-after", "x-standin-bytes", "x-standin-case",
                         "x-standin-counter"):
                v = uget(name)
                if v is not None:
                    out.append("%s: %s" % (name, v))
            out.append("x-proxy-model: " + model)
            out.append("x-proxy-request-id: " + rid)
            out.append("Access-Control-Allow-Origin: *")
            out.append("Connection: close")
            self.cs.sendall(("\r\n".join(out) + "\r\n\r\n").encode())
            nbytes = 0
            if status != 204 and method != "HEAD":
                if urest:
                    self.cs.sendall(urest)
                    nbytes += len(urest)
                up.settimeout(300.0)
                while True:
                    buf = up.recv(65536)
                    if not buf:
                        break
                    self.cs.sendall(buf)
                    nbytes += len(buf)
            ms = round((time.time() - t0) * 1000, 1)
            with LOCK:
                STATS["lat"].append(ms)
                STATS["bytes_out"] += nbytes
                pm["bytes"] += nbytes
            log({"id": rid, "key": key.name, "model": want or model, "upstream": model,
                 "stream": bool(req.get("stream")), "status": status, "ms": ms,
                 "bytes": nbytes, "messages": len(req.get("messages") or []),
                 "prompt_chars": sum(len(str(m.get("content") or ""))
                                     for m in (req.get("messages") or []))})
        except OSError as exc:
            with LOCK:
                STATS["upstream_error"] += 1
                pm["err"] += 1
            if pm.get("n") == pm.get("err"):
                pass
            try:
                send_response(self.cs, 502, {"error": {"message": "upstream %s: %r" % (model, exc),
                                                       "type": "upstream_error"}})
            except OSError:
                pass
            log({"id": rid, "key": key.name, "model": want or model, "upstream": model,
                 "status": 502, "error": repr(exc)})
        finally:
            if up is not None:
                try:
                    up.close()
                except OSError:
                    pass
            with LOCK:
                key.inflight -= 1
                STATS["inflight"] -= 1


class Server(object):
    def __init__(self, sock, keys):
        self.sock = sock
        self.keys = keys

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            t = threading.Thread(target=Handler(conn, self.keys).run, daemon=True)
            t.start()


def load_keys(path):
    keys = {}
    with open(path) as f:
        doc = json.load(f)
    for entry in doc.get("keys", []):
        k = Key(entry)
        keys[k.key] = k
    if not keys:
        raise SystemExit("no keys in " + path)
    return keys


def main():
    global LOGPATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=RUN + "/proxy.sock")
    ap.add_argument("--keys", default=os.path.dirname(os.path.abspath(__file__)) + "/keys.json")
    ap.add_argument("--log", default=os.path.dirname(os.path.abspath(__file__)) + "/logs/requests.jsonl")
    a = ap.parse_args()
    LOGPATH = a.log
    os.makedirs(os.path.dirname(a.socket), exist_ok=True)
    os.makedirs(os.path.dirname(LOGPATH), exist_ok=True)
    keys = load_keys(a.keys)
    try:
        os.unlink(a.socket)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(a.socket)
    os.chmod(a.socket, 0o600)
    s.listen(128)
    sys.stderr.write("llmproxy listening on %s (%d keys)\n" % (a.socket, len(keys)))
    sys.stderr.flush()
    Server(s, keys).serve()


if __name__ == "__main__":
    main()
