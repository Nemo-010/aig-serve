//! TLS to the providers. One connection per request, which is what the measured
//! +8 ms reference implementation does too.

use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use rustls::pki_types::ServerName;
use rustls::{ClientConnection, RootCertStore, StreamOwned};

fn client_config() -> Arc<rustls::ClientConfig> {
    static CFG: OnceLock<Arc<rustls::ClientConfig>> = OnceLock::new();
    CFG.get_or_init(|| {
        let mut roots = RootCertStore::empty();
        roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        let cfg = rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        Arc::new(cfg)
    })
    .clone()
}

pub fn connect(host: &str, timeout: Duration) -> io::Result<StreamOwned<ClientConnection, TcpStream>> {
    let addr = format!("{host}:443");
    let tcp = TcpStream::connect(addr)?;
    tcp.set_read_timeout(Some(timeout))?;
    tcp.set_write_timeout(Some(timeout))?;
    tcp.set_nodelay(true)?;
    let name = ServerName::try_from(host.to_string())
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, format!("bad host: {e}")))?;
    let conn = ClientConnection::new(client_config(), name)
        .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("tls: {e}")))?;
    Ok(StreamOwned::new(conn, tcp))
}

/// A tiny GET, used by the watchdog to ask the tunnel for /healthz.
pub fn https_get_status(host: &str, path: &str, timeout_secs: u64) -> Option<u16> {
    let mut s = connect(host, Duration::from_secs(timeout_secs)).ok()?;
    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: relay-watchdog/1\r\nConnection: close\r\n\r\n"
    );
    s.write_all(req.as_bytes()).ok()?;
    s.flush().ok()?;
    let mut buf = [0u8; 256];
    let n = s.read(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf[..n]);
    let status = text.split(' ').nth(1)?.parse().ok()?;
    Some(status)
}
