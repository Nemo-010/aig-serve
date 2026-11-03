#!/usr/bin/env python3
"""standin-echo: deterministic mock OpenAI-compatible model server on a unix socket."""

import argparse
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time

NAME = "echo"
MODEL = "standin-echo"
DEFAULT_SOCKET = "/workspace/llmproxy/run/echo.sock"
MAX_BODY = 64 * 1024 * 1024
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}


def err_obj(message, typ, code):
    return {"error": {"message": message, "type": typ, "code": code}}

NOT_FOUND = err_obj("not found", "invalid_request_error", "not_found")


def prompt_text(messages):
    return "\n".join(str(m.get("content") or "") if isinstance(m, dict) else str(m)
                     for m in messages)


def answer_text(model, messages, digest):
    return (
        "standin-echo here: model %r sent %d message(s), %d characters of prompt text in all, "
        "and I answer at once from a fixed template so the reply is plain to check; the sha256 "
        "prefix below covers the joined message contents and nothing else, so if it matches the "
        "value you compute yourself the tunnel passed the request through intact. DIGEST %s"
        % (model, len(messages), len(prompt_text(messages)), digest)
    )


def usage_of(messages, text):
    p, c = max(1, len(prompt_text(messages)) // 4), max(1, len(text) // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def respond(conn, status, headers, body):
    """one write for the head, one for the body; nothing is buffered."""
    head = ["HTTP/1.1 %d %s" % (status, REASONS.get(status, "OK"))]
    head.extend("%s: %s" % kv for kv in headers)
    head.append("Connection: close")
    conn.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
    if body:
        conn.sendall(body)


def send_json(conn, status, obj):
    body = json.dumps(obj, separators=(",", ":")).encode()
    respond(conn, status, [("Content-Type", "application/json"),
                           ("Content-Length", str(len(body)))], body)


def send_error(conn, status, message, code, typ="invalid_request_error"):
    send_json(conn, status, err_obj(message, typ, code))


def send_stream(conn, model, created, cid, text):
    respond(conn, 200, [("Content-Type", "text/event-stream"),
                        ("Cache-Control", "no-cache")], None)
    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
    for delta, finish in (({"content": text}, None), ({}, "stop")):
        choice = {"index": 0, "delta": delta, "finish_reason": finish}
        conn.sendall(("data: " + json.dumps(dict(base, choices=[choice]),
                                           separators=(",", ":")) + "\n\n").encode())
    conn.sendall(b"data: [DONE]\n\n")


def handle_chat(conn, raw):
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return send_error(conn, 400, "invalid JSON body: %s" % exc, "invalid_json")
    if not isinstance(body, dict):
        return send_error(conn, 400, "body must be a JSON object", "invalid_body")
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return send_error(conn, 400, "messages must be a list", "invalid_messages")
    model = body.get("model")
    if not isinstance(model, str):
        model = MODEL if model is None else str(model)
    digest = hashlib.sha256(prompt_text(messages).encode()).hexdigest()[:16]
    text = answer_text(model, messages, digest)
    created = int(time.time())
    cid = "chatcmpl-" + hashlib.sha256(("id:" + digest).encode()).hexdigest()[:8]
    if str(body.get("stream")).lower() == "true":
        return send_stream(conn, model, created, cid, text)
    send_json(conn, 200, {
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": usage_of(messages, text),
    })


def read_request(rfile):
    line = rfile.readline(65536)
    if not line:
        return None
    parts = line.decode("latin-1").split()
    if len(parts) < 2:
        return None
    method, path = parts[0].upper(), parts[1].split("?", 1)[0]
    headers = {}
    while True:
        h = rfile.readline(65536)
        if not h or h in (b"\r\n", b"\n"):
            break
        key, _, val = h.decode("latin-1").partition(":")
        headers[key.strip().lower()] = val.strip()
    body, total = b"", 0
    try:
        n = int(headers.get("content-length") or 0)
    except ValueError:
        n = 0
    if n > 0:
        body = rfile.read(min(n, MAX_BODY)) or b""
    elif "chunked" in headers.get("transfer-encoding", "").lower():
        while True:
            size_line = rfile.readline(65536)
            if not size_line:
                break
            try:
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                break
            if size <= 0:
                rfile.readline(65536)
                break
            body += rfile.read(size) or b""
            total += size
            rfile.readline(65536)
            if total > MAX_BODY:
                break
    return method, path, headers, body


def handle(conn):
    try:
        req = read_request(conn.makefile("rb"))
        if req is None:
            return
        method, path, _headers, body = req
        if method == "GET" and path == "/healthz":
            send_json(conn, 200, {"ok": True, "standin": NAME})
        elif method == "POST" and path == "/v1/chat/completions":
            handle_chat(conn, body)
        else:
            send_json(conn, 404, NOT_FOUND)
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        pass
    except Exception as exc:
        try:
            send_error(conn, 500, "internal error: %s" % exc, "internal_error", "server_error")
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


class Server:
    def __init__(self, path):
        self.path = path
        self.sock = None
        self.threads = set()
        self.lock = threading.Lock()
        self.stop = False

    def shutdown(self, *_a):
        self.stop = True
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass

    def run(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(128)
        self.sock.settimeout(0.5)
        print("standin %s listening on %s" % (NAME, self.path), file=sys.stderr, flush=True)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop:
                    break
                continue
            t = threading.Thread(target=self._serve_one, args=(conn,), daemon=True)
            with self.lock:
                self.threads.add(t)
            t.start()
        with self.lock:
            pending = list(self.threads)
        for t in pending:
            t.join(timeout=1.0)
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _serve_one(self, conn):
        try:
            handle(conn)
        finally:
            with self.lock:
                self.threads.discard(threading.current_thread())


def main(argv=None):
    ap = argparse.ArgumentParser(description="standin-echo mock model server")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    srv = Server(os.path.abspath(ap.parse_args(argv).socket))
    signal.signal(signal.SIGTERM, srv.shutdown)
    signal.signal(signal.SIGINT, srv.shutdown)
    try:
        srv.run()
    finally:
        srv.shutdown()
        try:
            os.unlink(srv.path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
