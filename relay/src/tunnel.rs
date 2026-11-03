//! The tunnel: cfrs is a child process, and this module is its supervisor.
//!
//! Rules:
//! - cfrs is started with `--json` so its ready document can be parsed for the URL.
//! - `--no-restart` is passed: there is exactly one supervisor here, and two
//!   would fight.
//! - the child gets PR_SET_PDEATHSIG, so a SIGKILLed daemon cannot leave an
//!   orphan tunnel behind.
//! - when cfrs exits, it is started again with a backoff that honours a
//!   Retry-After in its last words.
//! - when the public URL stops answering, the child is killed so that the
//!   supervisor starts a fresh one.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub struct TunnelState {
    pub url: Mutex<Option<String>>,
    pub cfrs_pid: Mutex<Option<u32>>,
    pub restarts: Mutex<u32>,
    pub last_error: Mutex<Option<String>>,
    pub ready: AtomicBool,
}

impl TunnelState {
    pub fn new() -> Arc<TunnelState> {
        Arc::new(TunnelState {
            url: Mutex::new(None),
            cfrs_pid: Mutex::new(None),
            restarts: Mutex::new(0),
            last_error: Mutex::new(None),
            ready: AtomicBool::new(false),
        })
    }
    pub fn url(&self) -> Option<String> {
        self.url.lock().ok().and_then(|g| g.clone())
    }
}

pub struct TunnelConfig {
    pub cfrs: String,
    pub socket: String,
    pub forward: bool,
    pub url_file: String,
}

fn log(msg: &str) {
    eprintln!("[tunnel] {msg}");
}

/// Pull a `Retry-After: N s` out of a cfrs line, if it is there.
fn retry_after_hint(line: &str) -> Option<u64> {
    let idx = line.find("Retry-After:")?;
    let rest = &line[idx + "Retry-After:".len()..];
    let num: String = rest.trim().chars().take_while(|c| c.is_ascii_digit()).collect();
    num.parse::<u64>().ok()
}

fn url_of(line: &str) -> Option<String> {
    let doc: serde_json::Value = serde_json::from_str(line).ok()?;
    if doc.get("event").and_then(|e| e.as_str()) == Some("ready")
        || doc.get("event").and_then(|e| e.as_str()) == Some("verified")
    {
        return doc.get("url").and_then(|u| u.as_str()).map(|s| s.to_string());
    }
    None
}

/// Start cfrs once and follow it until it exits. Returns the suggested backoff.
fn run_once(cfg: &TunnelConfig, state: &TunnelState, stop: &Arc<AtomicBool>) -> Duration {
    let mut cmd = Command::new(&cfg.cfrs);
    cmd.arg("tunnel").arg("--unix").arg(&cfg.socket);
    if cfg.forward {
        cmd.arg("--forward").arg(format!("unix:{}", cfg.socket));
    }
    // `--name`, `--ready-timeout` and `--no-restart` all require `--detach`, and a
    // detached instance has its own supervisor. This daemon supervises, so the
    // child runs in the foreground and is watched directly.
    cmd.arg("--json");
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null());

    // A daemon that dies must not leave a tunnel behind.
    unsafe {
        use std::os::unix::process::CommandExt;
        cmd.pre_exec(|| {
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM as libc::c_ulong, 0, 0, 0);
            Ok(())
        });
    }

    let mut child: Child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            log(&format!("cannot start {}: {e}", cfg.cfrs));
            *state.last_error.lock().unwrap() = Some(format!("spawn: {e}"));
            return Duration::from_secs(30);
        }
    };
    *state.cfrs_pid.lock().unwrap() = Some(child.id());
    log(&format!("cfrs started, pid {}", child.id()));

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let hint: Arc<Mutex<Option<u64>>> = Arc::new(Mutex::new(None));

    if let Some(err) = stderr {
        let hint = Arc::clone(&hint);
        std::thread::spawn(move || {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                if let Some(secs) = retry_after_hint(&line) {
                    *hint.lock().unwrap() = Some(secs);
                }
                log(&line);
            }
        });
    }
    if let Some(out) = stdout {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            if let Some(url) = url_of(&line) {
                log(&format!("url {url}"));
                *state.url.lock().unwrap() = Some(url);
                state.ready.store(true, Ordering::SeqCst);
            } else if !line.trim().is_empty() {
                log(&line);
            }
        }
    }

    // stdout closed: the child is on its way out.
    let status = child.wait();
    *state.cfrs_pid.lock().unwrap() = None;
    state.ready.store(false, Ordering::SeqCst);
    *state.url.lock().unwrap() = None;
    let _ = std::fs::remove_file(&cfg.url_file);
    log(&format!("cfrs exited: {status:?}"));
    if stop.load(Ordering::SeqCst) {
        return Duration::from_secs(0);
    }
    if let Some(secs) = *hint.lock().unwrap() {
        // The service gives a fixed expiry, and asking again does not extend it
        // (measured: 1166 -> 456 -> 441 over 12 minutes, all while retrying), so a
        // short poll is the right shape: notice the window opening, do not sleep
        // half an hour past it.
        let jitter = (std::process::id() % 17) as u64;
        let wait = Duration::from_secs(secs.min(300) + jitter);
        log(&format!(
            "cfrs asked to wait {secs}s; polling again in {}s",
            wait.as_secs()
        ));
        *state.last_error.lock().unwrap() =
            Some(format!("provisioning rate limited (Retry-After {secs}s)"));
        return wait;
    }
    Duration::from_secs(5)
}

/// Does the public URL answer our own /healthz?
fn probe_url(url: &str) -> bool {
    let host = url
        .split_once("//")
        .map(|(_, r)| r)
        .unwrap_or(url)
        .split('/')
        .next()
        .unwrap_or("");
    if host.is_empty() {
        return false;
    }
    crate::upstream::https_get_status(host, "/healthz", 12).is_some_and(|s| (200..500).contains(&s))
}

pub fn supervise(cfg: TunnelConfig, state: Arc<TunnelState>, stop: Arc<AtomicBool>) {
    let mut backoff;
    let mut failures = 0u32;
    loop {
        if stop.load(Ordering::SeqCst) {
            return;
        }
        let wait = run_once(&cfg, &state, &stop);
        if stop.load(Ordering::SeqCst) {
            return;
        }
        *state.restarts.lock().unwrap() += 1;
        // A quick exit means the attempt failed: grow the backoff. A long-lived
        // run means it worked: start again small.
        if wait <= Duration::from_secs(5) {
            failures += 1;
            backoff = Duration::from_secs(2u64.pow(failures.min(5))).min(Duration::from_secs(60));
        } else {
            failures = 0;
            backoff = wait;
        }
        log(&format!("restarting cfrs in {:?}", backoff));
        std::thread::sleep(backoff);
    }
}

/// Stop the child: SIGTERM, then SIGKILL if it is still there.
///
/// cfrs installs a SIGTERM handler but a tunnel that is waiting out a
/// provisioning retry does not act on it (measured: 13 threads asleep in futex
/// 20 minutes after the signal, parent long gone), so escalation is not optional.
pub fn kill_child(state: &Arc<TunnelState>) {
    let pid = *state.cfrs_pid.lock().unwrap();
    let Some(pid) = pid else { return };
    log(&format!("stopping cfrs {pid}"));
    unsafe {
        libc::kill(pid as libc::pid_t, libc::SIGTERM);
    }
    for _ in 0..20 {
        std::thread::sleep(Duration::from_millis(250));
        if unsafe { libc::kill(pid as libc::pid_t, 0) } != 0 {
            return;
        }
    }
    log(&format!("cfrs {pid} ignored SIGTERM, sending SIGKILL"));
    unsafe {
        libc::kill(pid as libc::pid_t, libc::SIGKILL);
    }
}

/// Watch the URL: three failures in a row and the tunnel is considered dead.
pub fn watchdog(state: Arc<TunnelState>, stop: Arc<AtomicBool>, every: Duration) {
    let mut misses = 0;
    let mut started = Instant::now();
    loop {
        if stop.load(Ordering::SeqCst) {
            return;
        }
        std::thread::sleep(every);
        if stop.load(Ordering::SeqCst) {
            return;
        }
        if state.cfrs_pid.lock().map(|g| g.is_none()).unwrap_or(true) {
            misses = 0;
            continue;
        }
        let Some(url) = state.url() else { continue };
        // Give a fresh tunnel a minute before judging it.
        if started.elapsed() < Duration::from_secs(60) || state.ready.load(Ordering::SeqCst) == false {
            started = Instant::now();
            continue;
        }
        if probe_url(&url) {
            misses = 0;
        } else {
            misses += 1;
            log(&format!("watchdog: {url} did not answer ({misses}/3)"));
            if misses >= 3 {
                misses = 0;
                kill_child(&state);
                started = Instant::now();
            }
        }
    }
}

/// Write the public URL to a file, for anything that needs to read it.
pub fn write_url_file(path: &str, url: &str) {
    if let Ok(mut f) = std::fs::File::create(path) {
        let _ = f.write_all(url.as_bytes());
        let _ = f.write_all(b"\n");
    }
}
