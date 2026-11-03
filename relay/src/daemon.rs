//! Daemon plumbing: detach, one instance only, and a way to ask it to stop.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_sig: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

/// SIGTERM/SIGINT set a flag the accept loop checks. The listen socket has a
/// timeout, so the loop notices within a second.
pub fn install_signals(_stop: Arc<AtomicBool>) {
    unsafe {
        let mut sa: libc::sigaction = std::mem::zeroed();
        sa.sa_sigaction = on_signal as *const () as usize;
        libc::sigemptyset(&mut sa.sa_mask);
        sa.sa_flags = 0;
        libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
        libc::sigaction(libc::SIGINT, &sa, std::ptr::null_mut());
        libc::signal(libc::SIGHUP, libc::SIG_IGN);
    }
}

/// Double fork, new session, stdio to the log. The pidfile is locked for as long
/// as the daemon lives, so a second start fails instead of fighting.
pub fn daemonize(logfile: &str) -> Result<(), String> {
    if let Some(dir) = std::path::Path::new(logfile).parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logfile)
        .map_err(|e| format!("{logfile}: {e}"))?;

    unsafe {
        match libc::fork() {
            -1 => return Err("fork failed".into()),
            0 => {}
            _ => {
                // parent waits a moment, then exits 0
                std::thread::sleep(std::time::Duration::from_millis(300));
                std::process::exit(0);
            }
        }
        if libc::setsid() == -1 {
            return Err("setsid failed".into());
        }
        match libc::fork() {
            -1 => return Err("fork failed".into()),
            0 => {}
            _ => std::process::exit(0),
        }

        let devnull = File::open("/dev/null").map_err(|e| e.to_string())?;
        let log_fd = log.as_raw_fd();
        libc::dup2(devnull.as_raw_fd(), 0);
        libc::dup2(log_fd, 1);
        libc::dup2(log_fd, 2);
        std::mem::forget(log);
        std::mem::forget(devnull);
    }

    // All paths are absolute; moving to / keeps a mount from pinning us.
    let root = std::ffi::CString::new("/").unwrap();
    unsafe {
        libc::chdir(root.as_ptr());
    }
    eprintln!("relay daemonised, pid {}", std::process::id());
    Ok(())
}

/// Record the pid and hold an exclusive lock for the life of the process, so a
/// second copy fails instead of racing for the same socket.
pub fn claim_pidfile(pidfile: &str) -> Result<(), String> {
    if let Some(dir) = std::path::Path::new(pidfile).parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let mut f = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(pidfile)
        .map_err(|e| format!("{pidfile}: {e}"))?;
    let rc = unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        return Err(format!("another relay holds {pidfile}"));
    }
    let _ = f.set_len(0);
    let _ = writeln!(f, "{}", std::process::id());
    let _ = f.flush();
    std::mem::forget(f);
    Ok(())
}
