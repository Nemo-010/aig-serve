//! Providers, routing and caller tokens.
//!
//! A provider is a base URL plus the name of the environment variable that holds
//! its key. The key is read once at start-up, kept in memory, and never logged,
//! never written and never put in an argv.

use std::fs;
use std::sync::Arc;

pub struct Provider {
    pub name: String,
    pub host: String,
    pub base_path: String,
    pub key: String,
}

pub struct Config {
    pub providers: Vec<Provider>,
    pub default: usize,
    pub tokens: Vec<String>,
}

fn split_base(url: &str) -> (String, String) {
    let rest = url.split_once("//").map(|(_, r)| r).unwrap_or(url);
    let (host, tail) = match rest.split_once('/') {
        Some((h, t)) => (h, t),
        None => (rest, ""),
    };
    let host = host.split(':').next().unwrap_or(host).to_string();
    let base_path = if tail.is_empty() {
        String::new()
    } else {
        format!("/{}", tail.trim_end_matches('/'))
    };
    (host, base_path)
}

fn provider(name: &str, base_env: &str, key_env: &str, fallback_base: &str) -> Option<Provider> {
    let key = std::env::var(key_env).ok().filter(|k| !k.is_empty())?;
    let base = std::env::var(base_env).unwrap_or_else(|_| fallback_base.to_string());
    let (host, base_path) = split_base(&base);
    Some(Provider {
        name: name.to_string(),
        host,
        base_path,
        key,
    })
}

impl Config {
    pub fn load(tokens_path: &str, default_name: &str) -> Result<Arc<Config>, String> {
        let mut providers = Vec::new();
        if let Some(p) = provider(
            "kenari",
            "RELAY_KENARI_BASE",
            "KENARI_API_KEY",
            "https://kenari.id/v1",
        ) {
            providers.push(p);
        }
        if let Some(p) = provider(
            "commandcode",
            "RELAY_COMMANDCODE_BASE",
            "COMMANDCODE_API_KEY",
            "https://api.commandcode.ai/provider/v1",
        ) {
            providers.push(p);
        }
        if providers.is_empty() {
            return Err("no provider key in the environment (KENARI_API_KEY, COMMANDCODE_API_KEY)"
                .into());
        }
        let default = providers
            .iter()
            .position(|p| p.name == default_name || (default_name == "cc" && p.name == "commandcode"))
            .unwrap_or(0);
        let text = fs::read_to_string(tokens_path)
            .map_err(|e| format!("{tokens_path}: {e}"))?;
        let tokens: Vec<String> = text
            .lines()
            .map(|l| l.trim().to_string())
            .filter(|l| !l.is_empty() && !l.starts_with('#'))
            .collect();
        if tokens.is_empty() {
            return Err(format!("{tokens_path} has no token"));
        }
        Ok(Arc::new(Config {
            providers,
            default,
            tokens,
        }))
    }

    /// Constant-time token check. An empty or missing token never matches.
    pub fn authorized(&self, presented: &str) -> bool {
        if presented.is_empty() {
            return false;
        }
        let mut hit = 0u8;
        for t in &self.tokens {
            if t.len() == presented.len() {
                let mut diff = 0u8;
                for (a, b) in t.bytes().zip(presented.bytes()) {
                    diff |= a ^ b;
                }
                hit |= (diff == 0) as u8;
            }
        }
        hit == 1
    }

    /// `kenari/deepseek-v4-1-flash` -> (provider index, model id without prefix)
    pub fn route(&self, model: &str, hint: Option<&str>) -> Option<(usize, String)> {
        if let Some(h) = hint {
            if let Some(i) = self.resolve_name(h) {
                return Some((i, model.to_string()));
            }
        }
        for (i, p) in self.providers.iter().enumerate() {
            let mut aliases = vec![p.name.clone()];
            if p.name == "commandcode" {
                aliases.push("cc".into());
            }
            for a in aliases {
                for sep in ['/', ':'] {
                    let prefix = format!("{a}{sep}");
                    if let Some(rest) = model.strip_prefix(&prefix) {
                        return Some((i, rest.to_string()));
                    }
                }
            }
        }
        Some((self.default, model.to_string()))
    }

    fn resolve_name(&self, name: &str) -> Option<usize> {
        self.providers.iter().position(|p| {
            p.name == name || (p.name == "commandcode" && name == "cc")
        })
    }

    pub fn names(&self) -> Vec<&str> {
        self.providers.iter().map(|p| p.name.as_str()).collect()
    }
}
