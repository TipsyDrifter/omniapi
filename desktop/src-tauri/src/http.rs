//! The two requests the shell makes to the service, over a plain socket (no HTTP client crate
//! for two calls): `GET /api/health` and `POST /api/shutdown`. HTTP/1.0 + `Connection: close`
//! so the reply is the whole stream; no `Origin` header, so the service's local-only guard
//! (1.3-M2-e) treats the shell like the CLI.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use crate::supervisor::Health;

pub const PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

fn request(port: u16, method: &str, path: &str, timeout: Duration) -> Option<String> {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let mut s = TcpStream::connect_timeout(&addr, timeout).ok()?;
    s.set_read_timeout(Some(timeout)).ok()?;
    s.set_write_timeout(Some(timeout)).ok()?;
    let body = if method == "POST" { "Content-Length: 0\r\n" } else { "" };
    write!(s, "{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n{body}Connection: close\r\n\r\n").ok()?;
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).ok()?;
    Some(String::from_utf8_lossy(&buf).into_owned())
}

/// Status code and JSON body of a raw HTTP response.
fn split(resp: &str) -> Option<(u16, serde_json::Value)> {
    let status: u16 = resp.lines().next()?.split_whitespace().nth(1)?.parse().ok()?;
    let body = resp.split_once("\r\n\r\n")?.1;
    let v = serde_json::from_str(body.trim()).ok()?;
    Some((status, v))
}

/// What a `/api/health` reply means. `{"status": "ok"}` = up; another status with a pid =
/// the process answers but its runtime is not ready yet; anything else = not up.
pub fn parse_health(resp: &str) -> Health {
    let Some((200, v)) = split(resp) else { return Health::None };
    let Some(pid) = v.get("pid").and_then(|p| p.as_u64()).map(|p| p as u32) else { return Health::None };
    match v.get("status").and_then(|s| s.as_str()) {
        Some("ok") => Health::Ok { pid },
        Some(_) => Health::Booting { pid },
        None => Health::None,
    }
}

pub fn health(port: u16) -> Health {
    request(port, "GET", "/api/health", PROBE_TIMEOUT).map(|r| parse_health(&r)).unwrap_or(Health::None)
}

/// The version the service reports (for the log only).
pub fn version(port: u16) -> Option<String> {
    let r = request(port, "GET", "/api/health", PROBE_TIMEOUT)?;
    let (_, v) = split(&r)?;
    v.get("version").and_then(|s| s.as_str()).map(str::to_string)
}

/// Ask the service to shut down by itself. `Ok(pid)` = it accepted and named its pid;
/// `Err` says why not (an old service without the endpoint answers 404/405).
pub fn parse_shutdown(resp: &str) -> Result<u32, String> {
    let (status, v) = split(resp).ok_or_else(|| "unreadable reply".to_string())?;
    if status != 200 {
        return Err(format!("HTTP {status}: {}", v.get("detail").and_then(|d| d.as_str()).unwrap_or("")));
    }
    match (v.get("ok").and_then(|o| o.as_bool()), v.get("pid").and_then(|p| p.as_u64())) {
        (Some(true), Some(pid)) => Ok(pid as u32),
        _ => Err(format!("unexpected reply {v}")),
    }
}

pub fn shutdown(port: u16) -> Result<u32, String> {
    let r = request(port, "POST", "/api/shutdown", Duration::from_secs(5)).ok_or_else(|| "no answer".to_string())?;
    parse_shutdown(&r)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn resp(status: &str, body: &str) -> String {
        format!("HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}", body.len())
    }

    #[test]
    fn health_replies() {
        assert_eq!(parse_health(&resp("200 OK", r#"{"status":"ok","version":"1.2.0","pid":42,"ts":1}"#)), Health::Ok { pid: 42 });
        assert_eq!(parse_health(&resp("200 OK", r#"{"status":"starting","pid":42}"#)), Health::Booting { pid: 42 });
        assert_eq!(parse_health(&resp("503 Service Unavailable", r#"{"status":"ok","pid":42}"#)), Health::None);
        assert_eq!(parse_health(&resp("200 OK", "<html>not us</html>")), Health::None);
        assert_eq!(parse_health(&resp("200 OK", r#"{"status":"ok"}"#)), Health::None, "no pid = not an OmniAPI service");
        assert_eq!(parse_health(""), Health::None);
    }

    #[test]
    fn shutdown_replies() {
        assert_eq!(parse_shutdown(&resp("200 OK", r#"{"ok":true,"pid":7}"#)), Ok(7));
        assert!(parse_shutdown(&resp("404 Not Found", r#"{"detail":"Not Found"}"#)).unwrap_err().contains("404"));
        assert!(parse_shutdown(&resp("503 Service Unavailable", r#"{"detail":"not started by omni serve"}"#)).unwrap_err().contains("omni serve"));
        assert!(parse_shutdown("garbage").is_err());
    }

    #[test]
    fn nothing_listening_is_none() {
        // port 1 on loopback: refused at once on Windows
        assert_eq!(health(1), Health::None);
        assert!(shutdown(1).is_err());
    }
}
