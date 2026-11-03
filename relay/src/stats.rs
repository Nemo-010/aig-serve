//! Counters, in one place.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use crate::config::Config;

pub struct Stats {
    pub requests: AtomicU64,
    pub auth_fail: AtomicU64,
    pub inflight: AtomicU64,
    pub max_inflight: AtomicU64,
    pub bytes_out: AtomicU64,
    pub errors: AtomicU64,
    pub by_provider: Mutex<Vec<(String, u64, u64)>>, // name, count, bytes
    pub lat_ms: Mutex<Vec<u64>>,
}

impl Stats {
    pub fn new() -> Stats {
        Stats {
            requests: AtomicU64::new(0),
            auth_fail: AtomicU64::new(0),
            inflight: AtomicU64::new(0),
            max_inflight: AtomicU64::new(0),
            bytes_out: AtomicU64::new(0),
            errors: AtomicU64::new(0),
            by_provider: Mutex::new(Vec::new()),
            lat_ms: Mutex::new(Vec::new()),
        }
    }

    pub fn begin(&self) {
        self.requests.fetch_add(1, Ordering::Relaxed);
        let now = self.inflight.fetch_add(1, Ordering::Relaxed) + 1;
        self.max_inflight.fetch_max(now, Ordering::Relaxed);
    }

    pub fn end(&self, _idx: usize, name: &str, status: u16, bytes: u64, took: Duration) {
        self.inflight.fetch_sub(1, Ordering::Relaxed);
        self.bytes_out.fetch_add(bytes, Ordering::Relaxed);
        if status >= 400 {
            self.errors.fetch_add(1, Ordering::Relaxed);
        }
        if let Ok(mut v) = self.by_provider.lock() {
            if let Some(row) = v.iter_mut().find(|r| r.0 == name) {
                row.1 += 1;
                row.2 += bytes;
            } else {
                v.push((name.to_string(), 1, bytes));
            }
        }
        if let Ok(mut v) = self.lat_ms.lock() {
            v.push(took.as_millis() as u64);
            if v.len() > 500 {
                v.remove(0);
            }
        }
    }

    pub fn snapshot(&self, cfg: &Config) -> String {
        let mut lat = self.lat_ms.lock().map(|v| v.clone()).unwrap_or_default();
        lat.sort_unstable();
        let p = |q: f64| -> u64 {
            if lat.is_empty() {
                return 0;
            }
            lat[std::cmp::min(lat.len() - 1, (lat.len() as f64 * q) as usize)]
        };
        let per = self
            .by_provider
            .lock()
            .map(|v| {
                v.iter()
                    .map(|(n, c, b)| serde_json::json!({"provider": n, "requests": c, "bytes": b}))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        serde_json::json!({
            "requests": self.requests.load(Ordering::Relaxed),
            "auth_fail": self.auth_fail.load(Ordering::Relaxed),
            "inflight": self.inflight.load(Ordering::Relaxed),
            "max_inflight": self.max_inflight.load(Ordering::Relaxed),
            "bytes_out": self.bytes_out.load(Ordering::Relaxed),
            "errors": self.errors.load(Ordering::Relaxed),
            "latency_ms": {"n": lat.len(), "p50": p(0.50), "p95": p(0.95), "max": lat.last().copied().unwrap_or(0)},
            "providers": cfg.names(),
            "per_provider": per,
        })
        .to_string()
    }
}
