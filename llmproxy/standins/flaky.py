#!/usr/bin/env python3
"""standin-flaky: mock OpenAI-compatible server with a deterministic failure cycle."""

import argparse, hashlib, itertools, json, os, re, signal, socketserver
import stat, sys, threading, time
from http.server import BaseHTTPRequestHandler

NAME = "flaky"
MODEL = "standin-flaky"
DEFAULT_SOCKET = "/workspace/llmproxy/run/flaky.sock"
CASES = ("ok", "429", "500", "400", "slow-headers", "stream-abort")
ALIASES = {"ok": "ok", "200": "ok", "429": "429", "500": "500", "400": "400",
           "malformed": "400", "400-malformed": "400", "slow": "slow-headers",
           "slow-headers": "slow-headers", "abort": "stream-abort",
           "streamabort": "stream-abort", "stream-abort": "stream-abort"}
SLOW_HEADERS_S, DELAY_MS, MAX_DELAY_MS = 5.0, 40, 1000
MAX_BODY, _seq, _seq_lock = 8 * 1024 * 1024, itertools.count(), threading.Lock()


def prompt_text(body):
    try:
        return "\n".join(str(m.get("content") or "") for m in body.get("messages") or [])
    except Exception:
        return ""


def digest_of(body):
    return hashlib.sha256(prompt_text(body).encode()).hexdigest()[:16]


def norm_case(value):
    key = str(value).strip().lower().replace("_", "-") if value is not None else ""
    return ALIASES.get(key) if key else None


def clamp_delay(value):
    try:
        return max(0, min(MAX_DELAY_MS, int(value)))
    except (TypeError, ValueError):
        return DELAY_MS


def pieces_of(text, want=10):
    toks = re.findall(r"\S+\s*", text)
    step = max(1, -(-len(toks) // want)) if toks else 1
    return ["".join(toks[i:i + step]) for i in range(0, len(toks), step)]


def answer_text(model, body, digest):
    return ("standin-flaky answered fresh: model %r brought %d message(s) and %d characters of "
            "prompt text, and this reply comes from a fixed template so every field can be "
            "checked by hand; the sha256 prefix below covers only the newline-joined message "
            "contents, so if it matches the value you compute yourself the request arrived "
            "undamaged. DIGEST %s"
            % (model, len(body.get("messages") or []), len(prompt_text(body)), digest))


def completion_id(digest):
    return "chatcmpl-" + hashlib.sha256(("id:" + digest).encode()).hexdigest()[:8]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "standin-flaky/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def write_out(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def send_json(self, code, obj, extra=None):
        data = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.write_out(data)
        self.close_connection = True

    def send_err(self, code, message, typ, errcode, extra=None):
        self.send_json(code, {"error": {"message": message, "type": typ, "code": errcode}}, extra)

    def not_found(self):
        self.send_err(404, "not found", "invalid_request_error", "not_found")

    def path_only(self):
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def read_raw(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = []
            while True:
                n = int(self.rfile.readline(65536).split(b";")[0], 16)
                if n <= 0:
                    return b"".join(out)
                out.append(self.rfile.read(n))
                self.rfile.read(2)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, MAX_BODY)) if n > 0 else b""

    def do_GET(self):
        if self.path_only() == "/healthz":
            self.send_json(200, {"ok": True, "standin": NAME})
        else:
            self.not_found()

    def do_POST(self):
        if self.path_only() != "/v1/chat/completions":
            self.not_found()
            return
        try:
            raw = self.read_raw()
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            self.send_err(400, "invalid JSON body: %s" % exc, "invalid_request_error",
                          "invalid_json")
            return

        forced = norm_case(body.get("x_case") or body.get("x_standin_case")
                           or self.headers.get("x-standin-case") or self.headers.get("x-case"))
        if forced is None:
            with _seq_lock:
                step = next(_seq)
            case = CASES[step % len(CASES)]
        else:
            case, step = forced, None
        model = body.get("model")
        if not isinstance(model, str) or not model:
            model = MODEL
        stream = body.get("stream") is True or str(body.get("stream")).lower() == "true"
        delay = clamp_delay(body.get("x_delay_ms", self.headers.get("x-delay-ms")))
        digest = digest_of(body)
        extra = {"x-standin-case": case,
                 "x-standin-counter": "-" if step is None else str(step)}

        if case == "429":
            return self.send_err(429, "rate limited by standin-flaky (Retry-After: 3)",
                                 "rate_limit_error", "rate_limit_exceeded",
                                 dict(extra, **{"Retry-After": "3"}))
        if case == "500":
            return self.send_err(500, "internal server error injected by standin-flaky",
                                 "server_error", "internal_error", extra)
        if case == "400":
            return self.malformed(extra)
        if case == "slow-headers":
            time.sleep(SLOW_HEADERS_S)
        elif case == "stream-abort":
            return self.abort_stream(model, digest, delay, extra)
        self.answer(model, body, digest, stream, delay, extra)

    def malformed(self, extra):
        data = (b'{"error": {"message": "malformed JSON from standin-flaky, body ends mid-ob'
                b'ject", "type": "invalid_request_error"')
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.write_out(data)
        self.close_connection = True

    def sse_headers(self, extra):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()

    def sse_chunk(self, base, content, finish=None):
        obj = dict(base, choices=[{"index": 0, "delta": content, "finish_reason": finish}])
        self.write_out(b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n")

    def answer(self, model, body, digest, stream, delay, extra):
        text = answer_text(model, body, digest)
        created, cid = int(time.time()), completion_id(digest)
        if stream:
            base = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model}
            return self.stream_out(base, delay, extra, pieces_of(text), True)
        ptoks = max(1, len(prompt_text(body)) // 4)
        ctoks = max(1, len(text) // 4)
        return self.send_json(200, {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": ptoks, "completion_tokens": ctoks,
                      "total_tokens": ptoks + ctoks}}, extra)

    def stream_out(self, base, delay, extra, parts, finish):
        self.sse_headers(extra)
        try:
            for piece in parts:
                self.sse_chunk(base, {"content": piece})
                if delay:
                    time.sleep(delay / 1000.0)
            if finish:
                self.sse_chunk(base, {}, "stop")
                self.write_out(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    def abort_stream(self, model, digest, delay, extra):
        base = {"id": completion_id(digest), "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model}
        self.stream_out(base, delay, extra, [
            "standin-flaky stream-abort: ", "this stream dies after five chunks ",
            "and never sends a stop chunk or a terminator line. ",
            "prompt digest ", digest], False)

    def do_HEAD(self):
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_found


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = block_on_close = allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, path, handler):
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise SystemExit("refusing to clobber non-socket %s" % path)
            os.unlink(path)
        super().__init__(path, handler)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if not isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            sys.stderr.write("standin %s: handler error: %r\n" % (NAME, exc))


def main(argv=None):
    ap = argparse.ArgumentParser(description="standin-flaky mock model server")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    path = ap.parse_args(argv).socket
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    server = Server(path, Handler)
    os.chmod(path, 0o600)

    def bye(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    sys.stderr.write("standin %s listening on %s\n" % (NAME, path))
    sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
