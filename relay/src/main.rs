//! relay: a minimal passthrough proxy for real providers, with cfrs as a child.

mod config;
mod daemon;
mod handler;
mod http;
mod stats;
mod tunnel;
mod upstream;

use std::os::unix::net::UnixListener;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

struct Args {
    socket: String,
    keys: String,
    cfrs: String,
    url_file: String,
    pidfile: String,
    logfile: String,
    provider: String,
    daemon: bool,
    no_tunnel: bool,
    forward: bool,
}

fn usage() -> ! {
    eprintln!(
        "relay: a minimal passthrough proxy for real providers, tunnelled by cfrs\n\
         \n\
         usage: relay [options]\n\
         \n\
           --socket PATH      unix socket to listen on (default /run/relay.sock)\n\
           --keys PATH        file of caller tokens, one per line\n\
           --provider NAME    default provider for a bare model id (kenari|commandcode)\n\
           --cfrs PATH        cfrs binary (default: cfrs on PATH)\n\
           --url-file PATH    write the public URL here\n\
           --pidfile PATH     write the pid here\n\
           --logfile PATH     where the daemon sends its log\n\
           --no-tunnel        do not start cfrs; listen only\n\
           --no-forward       do not expose the raw WebSocket endpoint\n\
           --daemon           detach and run in the background\n\
         \n\
         providers come from the environment: KENARI_API_KEY (kenari.id) and\n\
         COMMANDCODE_API_KEY (api.commandcode.ai). Route with a model prefix,\n\
         e.g. {{\"model\":\"commandcode/gpt-6-astra\"}}, or x-relay-provider.\n"
    );
    std::process::exit(64)
}

fn parse_args() -> Args {
    let mut a = Args {
        socket: "run/relay.sock".into(),
        keys: "keys.txt".into(),
        cfrs: "cfrs".into(),
        url_file: "run/url.txt".into(),
        pidfile: "run/relay.pid".into(),
        logfile: "logs/relay.log".into(),
        provider: "kenari".into(),
        daemon: false,
        no_tunnel: false,
        forward: true,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut val = || it.next().unwrap_or_else(|| usage());
        match arg.as_str() {
            "--socket" => a.socket = val(),
            "--keys" => a.keys = val(),
            "--cfrs" => a.cfrs = val(),
            "--url-file" => a.url_file = val(),
            "--pidfile" => a.pidfile = val(),
            "--logfile" => a.logfile = val(),
            "--provider" => a.provider = val(),
            "--daemon" => a.daemon = true,
            "--no-tunnel" => a.no_tunnel = true,
            "--no-forward" => a.forward = false,
            "--help" | "-h" => usage(),
            other => {
                eprintln!("unknown argument: {other}");
                usage()
            }
        }
    }
    a
}

fn bind_socket(path: &str) -> std::io::Result<UnixListener> {
    if let Some(dir) = Path::new(path).parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    // A stale socket from a previous run is not a reason to fail.
    if Path::new(path).exists() {
        match std::os::unix::net::UnixStream::connect(path) {
            Ok(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::AddrInUse,
                    format!("{path} already has a listener"),
                ))
            }
            Err(_) => {
                let _ = std::fs::remove_file(path);
            }
        }
    }
    let l = UnixListener::bind(path)?;
    use std::os::unix::fs::PermissionsExt;
    if let Ok(meta) = std::fs::metadata(path) {
        let mut p = meta.permissions();
        p.set_mode(0o600);
        let _ = std::fs::set_permissions(path, p);
    }
    Ok(l)
}

/// Make a path absolute against the current directory. The daemon moves to `/`,
/// so every path it is given must survive that.
fn absolutize(p: &str) -> String {
    if p.starts_with('/') || p.contains('/') && p.starts_with("./") {
        return std::fs::canonicalize(p)
            .map(|x| x.to_string_lossy().into_owned())
            .unwrap_or_else(|_| p.to_string());
    }
    if p.starts_with('/') {
        return p.to_string();
    }
    match std::env::current_dir() {
        Ok(cwd) => {
            let joined = cwd.join(p);
            let parent = joined.parent().map(|d| d.to_path_buf());
            if let Some(dir) = parent {
                let _ = std::fs::create_dir_all(&dir);
            }
            joined.to_string_lossy().into_owned()
        }
        Err(_) => p.to_string(),
    }
}

fn main() {
    let mut args = parse_args();
    for field in [
        &mut args.socket, &mut args.keys, &mut args.url_file,
        &mut args.pidfile, &mut args.logfile,
    ] {
        *field = absolutize(field);
    }
    if args.daemon {
        if let Err(e) = daemon::daemonize(&args.logfile) {
            eprintln!("cannot daemonize: {e}");
            std::process::exit(70);
        }
    }
    if let Err(e) = daemon::claim_pidfile(&args.pidfile) {
        eprintln!("{e}");
        std::process::exit(75);
    }

    let cfg = match config::Config::load(&args.keys, &args.provider) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("config: {e}");
            std::process::exit(78);
        }
    };

    let stop = Arc::new(AtomicBool::new(false));
    daemon::install_signals(Arc::clone(&stop));

    let listener = match bind_socket(&args.socket) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("cannot listen on {}: {e}", args.socket);
            std::process::exit(73);
        }
    };
    eprintln!(
        "relay listening on {} (providers: {}, default {})",
        args.socket,
        cfg.names().join(", "),
        cfg.providers[cfg.default].name
    );

    let stats = Arc::new(stats::Stats::new());
    let state = tunnel::TunnelState::new();

    if !args.no_tunnel {
        let tcfg = tunnel::TunnelConfig {
            cfrs: args.cfrs.clone(),
            socket: args.socket.clone(),
            forward: args.forward,
            url_file: args.url_file.clone(),
        };
        {
            let state = Arc::clone(&state);
            let stop = Arc::clone(&stop);
            std::thread::spawn(move || tunnel::supervise(tcfg, state, stop));
        }
        {
            let state = Arc::clone(&state);
            let stop = Arc::clone(&stop);
            let url_file = args.url_file.clone();
            std::thread::spawn(move || {
                // publish the URL once it is known
                let mut last = String::new();
                while !stop.load(Ordering::SeqCst) {
                    if let Some(u) = state.url() {
                        if u != last {
                            tunnel::write_url_file(&url_file, &u);
                            last = u;
                        }
                    }
                    std::thread::sleep(Duration::from_secs(1));
                }
            });
        }
        {
            let state = Arc::clone(&state);
            let stop = Arc::clone(&stop);
            std::thread::spawn(move || tunnel::watchdog(state, stop, Duration::from_secs(30)));
        }
    }

    for conn in listener.incoming() {
        if stop.load(Ordering::SeqCst) {
            break;
        }
        match conn {
            Ok(stream) => {
                let cfg = Arc::clone(&cfg);
                let stats = Arc::clone(&stats);
                let state = Arc::clone(&state);
                std::thread::spawn(move || handler::handle(stream, cfg, stats, state));
            }
            Err(e) => eprintln!("accept: {e}"),
        }
    }

    tunnel::kill_child(&state);
    let _ = std::fs::remove_file(&args.socket);
    let _ = std::fs::remove_file(&args.pidfile);
    eprintln!("relay stopped");
}
