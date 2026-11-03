//! HTTP/1.1 reading and forwarding: just enough of it, and nothing that buffers
//! a response.

use std::io::{self, BufRead, BufReader, Read, Write};


pub const MAX_HEAD: usize = 64 * 1024;
pub const MAX_BODY: usize = 64 * 1024 * 1024;

pub struct Head {
    pub start: String,
    pub headers: Vec<(String, String)>,
}

impl Head {
    pub fn get(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    pub fn method(&self) -> &str {
        self.start.split(' ').next().unwrap_or("")
    }

    pub fn path(&self) -> &str {
        self.start.split(' ').nth(1).unwrap_or("/")
    }

    pub fn status(&self) -> u16 {
        self.start.split(' ').nth(1).and_then(|s| s.parse().ok()).unwrap_or(0)
    }

    pub fn to_bytes(&self) -> Vec<u8> {
        let mut out = String::with_capacity(256);
        out.push_str(&self.start);
        out.push_str("\r\n");
        for (k, v) in &self.headers {
            out.push_str(k);
            out.push_str(": ");
            out.push_str(v);
            out.push_str("\r\n");
        }
        out.push_str("\r\n");
        out.into_bytes()
    }
}

/// Read a head, leaving everything after it in the reader.
pub fn read_head<R: BufRead>(r: &mut R) -> io::Result<Option<Head>> {
    let mut start = String::new();
    let n = r.read_line(&mut start)?;
    if n == 0 {
        return Ok(None);
    }
    let start = start.trim_end_matches(['\r', '\n']).to_string();
    if start.is_empty() {
        return Ok(None);
    }
    let mut headers = Vec::new();
    let mut total = start.len();
    loop {
        let mut line = String::new();
        let n = r.read_line(&mut line)?;
        if n == 0 {
            break;
        }
        total += n;
        if total > MAX_HEAD {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "head too large"));
        }
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            break;
        }
        if let Some((k, v)) = line.split_once(':') {
            headers.push((k.trim().to_string(), v.trim().to_string()));
        }
    }
    Ok(Some(Head { start, headers }))
}

/// Read a request body: Content-Length, chunked, or nothing.
pub fn read_body<R: BufRead>(r: &mut R, head: &Head) -> io::Result<Vec<u8>> {
    let chunked = head
        .get("transfer-encoding")
        .map(|v| v.to_ascii_lowercase().contains("chunked"))
        .unwrap_or(false);
    if chunked {
        let mut out = Vec::new();
        loop {
            let mut line = String::new();
            if r.read_line(&mut line)? == 0 {
                break;
            }
            let size_hex = line.trim().split(';').next().unwrap_or("0");
            let n = usize::from_str_radix(size_hex, 16)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "bad chunk size"))?;
            if n == 0 {
                let mut trailer = String::new();
                while r.read_line(&mut trailer)? > 0 && trailer != "\r\n" && !trailer.is_empty() {
                    trailer.clear();
                }
                break;
            }
            if out.len() + n > MAX_BODY {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "body too large"));
            }
            let mut piece = vec![0u8; n];
            r.read_exact(&mut piece)?;
            out.extend_from_slice(&piece);
            let mut crlf = [0u8; 2];
            r.read_exact(&mut crlf)?;
        }
        return Ok(out);
    }
    if let Some(len) = head.get("content-length").and_then(|v| v.trim().parse::<usize>().ok()) {
        if len > MAX_BODY {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "body too large"));
        }
        let mut buf = vec![0u8; len];
        r.read_exact(&mut buf)?;
        return Ok(buf);
    }
    Ok(Vec::new())
}

enum Framing {
    Length(u64),
    Chunked,
    UntilEof,
}

fn framing(head: &Head) -> Framing {
    if head
        .get("transfer-encoding")
        .map(|v| v.to_ascii_lowercase().contains("chunked"))
        .unwrap_or(false)
    {
        return Framing::Chunked;
    }
    if let Some(n) = head.get("content-length").and_then(|v| v.trim().parse::<u64>().ok()) {
        return Framing::Length(n);
    }
    Framing::UntilEof
}

/// Copy a response body to the client as it arrives: no buffering, no re-framing.
pub fn relay_body_with<R: BufRead, W: Write>(r: &mut R, w: &mut W, head: &Head) -> io::Result<u64> {
    let mut total = 0u64;
    match framing(head) {
        Framing::Length(n) => {
            let mut left = n;
            let mut buf = [0u8; 32 * 1024];
            while left > 0 {
                let want = std::cmp::min(left as usize, buf.len());
                let got = r.read(&mut buf[..want])?;
                if got == 0 {
                    break;
                }
                w.write_all(&buf[..got])?;
                w.flush()?;
                left -= got as u64;
                total += got as u64;
            }
        }
        Framing::Chunked => loop {
            let mut line = Vec::new();
            if r.read_until(b'\n', &mut line)? == 0 {
                break;
            }
            w.write_all(&line)?;
            w.flush()?;
            total += line.len() as u64;
            let hex = String::from_utf8_lossy(&line);
            let size_hex = hex.trim().split(';').next().unwrap_or("0");
            let n = usize::from_str_radix(size_hex, 16).unwrap_or(0);
            if n == 0 {
                // trailers, then the final blank line
                loop {
                    let mut t = Vec::new();
                    if r.read_until(b'\n', &mut t)? == 0 {
                        break;
                    }
                    let done = t == b"\r\n" || t == b"\n";
                    w.write_all(&t)?;
                    w.flush()?;
                    total += t.len() as u64;
                    if done {
                        break;
                    }
                }
                break;
            }
            let mut left = n + 2;
            let mut buf = vec![0u8; 32 * 1024];
            while left > 0 {
                let want = std::cmp::min(left, buf.len());
                let got = r.read(&mut buf[..want])?;
                if got == 0 {
                    break;
                }
                w.write_all(&buf[..got])?;
                w.flush()?;
                left -= got;
                total += got as u64;
            }
        },
        Framing::UntilEof => {
            let mut buf = [0u8; 32 * 1024];
            loop {
                let got = r.read(&mut buf)?;
                if got == 0 {
                    break;
                }
                w.write_all(&buf[..got])?;
                w.flush()?;
                total += got as u64;
            }
        }
    }
    Ok(total)
}

/// Read an upstream answer and forward it, with the hop-by-hop headers dropped
/// and `Connection: close` added.
pub fn relay_response<W: Write>(up: &mut BufReader<impl Read>, w: &mut W) -> io::Result<(u16, u64)> {
    let head = match read_head(up)? {
        Some(h) => h,
        None => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "no upstream head")),
    };
    let status = head.status();
    let mut out = Head {
        start: head.start.clone(),
        headers: Vec::new(),
    };
    for (k, v) in &head.headers {
        let kl = k.to_ascii_lowercase();
        if matches!(
            kl.as_str(),
            "connection" | "keep-alive" | "proxy-authenticate" | "te" | "trailer" | "upgrade"
        ) {
            continue;
        }
        out.headers.push((k.clone(), v.clone()));
    }
    out.headers.push(("Connection".into(), "close".into()));
    w.write_all(&out.to_bytes())?;
    w.flush()?;
    let n = relay_body_with(up, w, &head)?;
    Ok((status, n))
}
