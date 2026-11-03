#!/usr/bin/env python3
"""standin-trickle: mock OpenAI-compatible model server on a unix socket."""

import argparse
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time

NAME = "trickle"
DEFAULT_SOCKET = "/workspace/llmproxy/run/trickle.sock"
N_CHUNKS = 32
DEFAULT_DELAY_MS = 60
MIN_DELAY_MS = 0
MAX_DELAY_MS = 1000
WORDS = (
    "this standin answers slowly on purpose so that a streaming client can "
    "watch every single chunk arrive over the wire and measure the gap "
    "between them while the tunnel stays honest"
).split()
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}


def prompt_text(body):
    msgs = body.get("messages") or []
    return "\n".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict))


def digest_of(body):
    return hashlib.sha256(prompt_text(body).encode()).hexdigest()[:16]


def build_pieces(digest):
    words = list(WORDS)
    while len(words) < N_CHUNKS - 1:
        words.append("more")
    return [w + " " for w in words[: N_CHUNKS - 1]] + ["DIGEST " + digest + "\n"]


def delay_ms(body, headers):
    raw = body["x_delay_ms"] if "x_delay_ms" in body else headers.get("x-delay-ms")
    try:
        return max(MIN_DELAY_MS, min(MAX_DELAY_MS, int(float(raw))))
    except (TypeError, ValueError):
        return DEFAULT_DELAY_MS


def send_json(conn, status, obj, extra=()):
    data = json.dumps(obj, separators=(",", ":")).encode()
    head = "\r\n".join(
        [
            "HTTP/1.1 %d %s" % (status, REASONS.get(status, "OK")),
            "Content-Type: application/json",
            "Content-Length: %d" % len(data),
            "Connection: close",
        ]
        + list(extra)
    )
    conn.sendall((head + "\r\n\r\n").encode() + data)


def send_error(conn, status, message, code, kind="invalid_request_error"):
    send_json(conn, status, {"error": {"message": message, "type": kind, "code": code}})


def chunk_line(cid, created, model, delta, finish):
    return "data: " + json.dumps(
        {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        },
        separators=(",", ":"),
    ) + "\n\n"


class Reader:
    def __init__(self, conn):
        self.conn = conn
        self.buf = b""

    def _fill(self):
        data = self.conn.recv(65536)
        if not data:
            raise EOFError
        self.buf += data

    def read_until(self, sep):
        while sep not in self.buf:
            self._fill()
        end = self.buf.index(sep) + len(sep)
        out, self.buf = self.buf[:end], self.buf[end:]
        return out

    def read_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def stream_reply(conn, model, digest, delay):
    cid = "chatcmpl-" + os.urandom(4).hex()
    created = int(time.time())
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
    )
    for piece in build_pieces(digest):
        conn.sendall(chunk_line(cid, created, model, {"content": piece}, None).encode())
        time.sleep(delay)
    conn.sendall(chunk_line(cid, created, model, {}, "stop").encode())
    conn.sendall(b"data: [DONE]\n\n")


def json_reply(conn, model, body, digest, delay):
    time.sleep(N_CHUNKS * delay)
    text = "".join(build_pieces(digest))
    prompt_tokens = len(prompt_text(body).split())
    send_json(
        conn,
        200,
        {
            "id": "chatcmpl-" + os.urandom(4).hex(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": N_CHUNKS,
                      "total_tokens": prompt_tokens + N_CHUNKS},
        },
    )


def close_quietly(conn):
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


def handle(conn):
    try:
        conn.settimeout(300.0)
        reader = Reader(conn)
        head = reader.read_until(b"\r\n\r\n").decode("latin-1")
        lines = head.split("\r\n")
        method, target = lines[0].split(" ")[:2]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, val = line.split(":", 1)
                headers[key.strip().lower()] = val.strip()
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        raw = reader.read_exact(length) if length > 0 else b""
        path = target.split("?", 1)[0].rstrip("/") or "/"

        if method == "GET" and path == "/healthz":
            send_json(conn, 200, {"ok": True, "standin": NAME})
            return
        if method != "POST" or path != "/v1/chat/completions":
            send_error(conn, 404, "not found", "not_found")
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            send_error(conn, 400, "malformed json body", "malformed_json")
            return
        if not isinstance(body, dict):
            send_error(conn, 400, "body must be a json object", "invalid_body")
            return

        model = body.get("model")
        if not isinstance(model, str) or not model:
            model = "standin-" + NAME
        digest = digest_of(body)
        delay = delay_ms(body, headers) / 1000.0
        if body.get("stream"):
            stream_reply(conn, model, digest, delay)
        else:
            json_reply(conn, model, body, digest, delay)
    except (EOFError, OSError, ValueError):
        pass
    except Exception:
        try:
            send_error(conn, 500, "internal error", "internal_error", "server_error")
        except Exception:
            pass
    finally:
        close_quietly(conn)


def main(argv=None):
    ap = argparse.ArgumentParser(description="standin-trickle unix-socket server")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    path = ap.parse_args(argv).socket

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    try:
        os.unlink(path)
    except OSError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(128)
    print("standin %s listening on %s" % (NAME, path), file=sys.stderr, flush=True)

    def cleanup():
        try:
            srv.close()
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass

    def on_signal(_signum, _frame):
        cleanup()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
