//! One client connection: read the request, pick a provider, forward it, and
//! stream the answer back.

use std::io::{BufReader, Write};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::config::Config;
use crate::http::{self, Head};
use crate::stats::Stats;
use crate::tunnel::TunnelState;
use crate::upstream;

const UPSTREAM_TIMEOUT_SECS: u64 = 300;

fn json_error(w: &mut impl Write, status: u16, code: &str, message: &str) {
    let body = serde_json::json!({
        "error": {"message": message, "type": "relay_error", "code": code, "param": null}
    })
    .to_string();
    let head = Head {
        start: format!("HTTP/1.1 {status} {}", reason(status)),
        headers: vec![
            ("Content-Type".into(), "application/json".into()),
            ("Content-Length".into(), body.len().to_string()),
            ("Connection".into(), "close".into()),
            ("Access-Control-Allow-Origin".into(), "*".into()),
        ],
    };
    let _ = w.write_all(&head.to_bytes());
    let _ = w.write_all(body.as_bytes());
    let _ = w.flush();
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "Status",
    }
}

fn require_auth(cfg: &Config, head: &Head) -> bool {
    let presented = head
        .get("authorization")
        .and_then(|v| {
            let v = v.trim();
            v.strip_prefix("Bearer ")
                .or_else(|| v.strip_prefix("bearer "))
                .map(|s| s.trim().to_string())
        })
        .or_else(|| head.get("x-api-key").map(|v| v.trim().to_string()))
        .unwrap_or_default();
    cfg.authorized(&presented)
}

pub fn handle(
    stream: std::os::unix::net::UnixStream,
    cfg: Arc<Config>,
    stats: Arc<Stats>,
    tunnel: Arc<TunnelState>,
) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(120)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(UPSTREAM_TIMEOUT_SECS)));
    let mut reader = BufReader::new(&stream);
    let mut writer = &stream;

    let head = match http::read_head(&mut reader) {
        Ok(Some(h)) => h,
        _ => return,
    };
    let path = head.path().split('?').next().unwrap_or("/").to_string();
    let method = head.method().to_string();

    // Unauthenticated: liveness, for the tunnel watchdog and for a person.
    if path == "/healthz" {
        let url = tunnel.url();
        let body = serde_json::json!({
            "ok": url.is_some(),
            "tunnel": url,
            "cfrs_pid": *tunnel.cfrs_pid.lock().unwrap(),
            "restarts": *tunnel.restarts.lock().unwrap(),
            "last_error": tunnel.last_error.lock().unwrap().clone(),
            "providers": cfg.names(),
        })
        .to_string();
        let h = Head {
            start: "HTTP/1.1 200 OK".into(),
            headers: vec![
                ("Content-Type".into(), "application/json".into()),
                ("Content-Length".into(), body.len().to_string()),
                ("Connection".into(), "close".into()),
            ],
        };
        let _ = writer.write_all(&h.to_bytes());
        let _ = writer.write_all(body.as_bytes());
        let _ = writer.flush();
        return;
    }

    if !require_auth(&cfg, &head) {
        stats.auth_fail.fetch_add(1, Ordering::Relaxed);
        json_error(&mut writer, 401, "invalid_api_key", "missing or wrong token: send Authorization: Bearer <token>");
        return;
    }

    if path == "/__relay/stats" {
        let body = stats.snapshot(&cfg);
        let h = Head {
            start: "HTTP/1.1 200 OK".into(),
            headers: vec![
                ("Content-Type".into(), "application/json".into()),
                ("Content-Length".into(), body.len().to_string()),
                ("Connection".into(), "close".into()),
            ],
        };
        let _ = writer.write_all(&h.to_bytes());
        let _ = writer.write_all(body.as_bytes());
        let _ = writer.flush();
        return;
    }

    let body = match http::read_body(&mut reader, &head) {
        Ok(b) => b,
        Err(e) => {
            json_error(&mut writer, 400, "bad_body", &format!("cannot read the body: {e}"));
            return;
        }
    };

    // Routing: a model prefix, a header, or the default provider.
    let mut model_field = None;
    let mut doc: Option<Value> = None;
    if !body.is_empty() {
        if let Ok(v) = serde_json::from_slice::<Value>(&body) {
            if let Some(m) = v.get("model").and_then(|m| m.as_str()) {
                model_field = Some(m.to_string());
            }
            doc = Some(v);
        }
    }
    let hint = head.get("x-relay-provider").map(|s| s.to_string());
    let (picked, model) = match &model_field {
        Some(m) => cfg.route(m, hint.as_deref()).expect("a provider"),
        None => (
            cfg.default,
            String::new(), // no model in the body: forward untouched
        ),
    };
    let provider = &cfg.providers[picked];

    let mut out_body = body.clone();
    if let (Some(d), true) = (doc.as_mut(), !model.is_empty()) {
        if let Some(obj) = d.as_object_mut() {
            obj.insert("model".into(), Value::String(model.clone()));
            if let Ok(b) = serde_json::to_vec(d) {
                out_body = b;
            }
        }
    }

    // The client speaks the OpenAI surface (/v1/...), and a provider base already
    // ends in /v1, so the shared prefix must not be doubled.
    let mut rel = path.as_str();
    if provider.base_path.ends_with("/v1") {
        if let Some(rest) = rel.strip_prefix("/v1") {
            rel = rest;
        }
    }
    let upstream_path = format!("{}{}", provider.base_path, rel);
    let mut req = Head {
        start: format!("{method} {upstream_path} HTTP/1.1"),
        headers: vec![
            ("Host".into(), provider.host.clone()),
            ("Authorization".into(), format!("Bearer {}", provider.key)),
            ("Content-Type".into(), "application/json".into()),
            ("Accept".into(), head.get("accept").unwrap_or("text/event-stream").to_string()),
            ("Accept-Encoding".into(), "identity".into()),
            ("Connection".into(), "close".into()),
        ],
    };
    if !out_body.is_empty() {
        req.headers.push(("Content-Length".into(), out_body.len().to_string()));
    }

    stats.begin();
    let started = Instant::now();
    let mut tls = match upstream::connect(&provider.host, Duration::from_secs(30)) {
        Ok(s) => s,
        Err(e) => {
            stats.end(picked, &provider.name, 502, 0, started.elapsed());
            json_error(&mut writer, 502, "upstream_connect", &format!("{}: {e}", provider.name));
            return;
        }
    };
    if let Err(e) = tls.write_all(&req.to_bytes()).and_then(|_| tls.write_all(&out_body)) {
        stats.end(picked, &provider.name, 502, 0, started.elapsed());
        json_error(&mut writer, 502, "upstream_write", &format!("{}: {e}", provider.name));
        return;
    }
    let _ = tls.flush();

    let mut up = BufReader::new(tls);
    match http::relay_response(&mut up, &mut writer) {
        Ok((status, bytes)) => {
            stats.end(picked, &provider.name, status, bytes, started.elapsed());
        }
        Err(e) => {
            stats.end(picked, &provider.name, 502, 0, started.elapsed());
            json_error(&mut writer, 502, "upstream_read", &format!("{}: {e}", provider.name));
        }
    }
    let _ = writer.flush();
}
