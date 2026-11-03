#!/usr/bin/env python3
"""standin-bulk: mock OpenAI-compatible model server on a unix socket; big answers."""

import argparse
import hashlib
import json
import os
import signal
import socketserver
import stat
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler

NAME = "bulk"
MODEL = "standin-bulk"
DEFAULT_SOCKET = "/workspace/llmproxy/run/bulk.sock"
DEFAULT_BYTES, MIN_BYTES, MAX_BYTES = 262144, 4096, 4194304
PIECE = 16384
MAX_BODY = 32 * 1024 * 1024
FILLER = "standin-bulk deterministic payload line"


def messages_text(body):
    try:
        return "\n".join(str(m.get("content") or "") for m in body.get("messages") or [])
    except Exception:
        return ""


def prompt_digest(body):
    return hashlib.sha256(messages_text(body).encode()).hexdigest()[:16]


def wanted_bytes(body, headers):
    try:
        n = int(body.get("x_bytes", headers.get("x_bytes")))
    except (TypeError, ValueError):
        n = DEFAULT_BYTES
    return max(MIN_BYTES, min(MAX_BYTES, n))


def build_text(x_bytes, digest):
    """Exactly x_bytes of numbered lines, the last line being the DIGEST."""
    tail = "\nDIGEST " + digest
    target, parts, total, i = x_bytes - len(tail), [], 0, 0
    while total < target:
        line = "line %07d %s\n" % (i, FILLER)
        parts.append(line[: target - total])
        total += len(parts[-1])
        i += 1
    return "".join(parts) + tail


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "standin-bulk/1.0"
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj, extra=None):
        data = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def send_err(self, code, message, etype, ecode):
        self.send_json(code, {"error": {"message": message, "type": etype, "code": ecode}})

    def not_found(self):
        self.send_err(404, "not found", "invalid_request_error", "not_found")

    def read_json(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raw = b""
            while True:
                size = int(self.rfile.readline(65536).split(b";")[0].strip(), 16)
                if not size:
                    while self.rfile.readline(65536) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                raw += self.rfile.read(size)
                self.rfile.read(2)
        else:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                raise ValueError("bad Content-Length")
            raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/healthz":
            self.send_json(200, {"ok": True, "standin": NAME})
        else:
            self.not_found()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/v1/chat/completions":
            return self.not_found()
        try:
            body = self.read_json()
        except ValueError as exc:
            return self.send_err(400, "invalid JSON body: %s" % exc,
                                 "invalid_request_error", "invalid_json")
        model = body.get("model")
        if not isinstance(model, str) or not model:
            model = MODEL
        content = build_text(wanted_bytes(body, self.headers), prompt_digest(body))
        data = content.encode()
        cid, created = "chatcmpl-" + uuid.uuid4().hex[:8], int(time.time())
        if body.get("stream"):
            return self.stream_out(cid, created, model, data)
        prompt = max(1, len(messages_text(body)) // 4)
        done = max(1, len(data) // 4)
        self.send_json(
            200,
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": done,
                          "total_tokens": prompt + done},
            },
            {"x-standin-bytes": str(len(data))},
        )

    def stream_out(self, cid, created, model, data):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("x-standin-bytes", str(len(data)))
            self.end_headers()
            for offset in range(0, len(data) + PIECE, PIECE):
                if offset < len(data):
                    delta = {"content": data[offset:offset + PIECE].decode("utf-8", "replace")}
                    reason = None
                else:
                    delta, reason = {}, "stop"
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
                self.wfile.write(b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_HEAD(self):
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_found


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)

    def __init__(self, path, handler):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(st.st_mode):
                raise SystemExit("refusing to clobber non-socket %s" % path)
            os.unlink(path)
        super().__init__(path, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="standin-bulk mock model server")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    path = parser.parse_args(argv).socket
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    server = Server(path, Handler)
    os.chmod(path, 0o600)

    def bye(*_):
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
