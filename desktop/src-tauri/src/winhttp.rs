//! One HTTP(S) GET through Windows' own WinHTTP (the system's TLS, certificate store and proxy
//! settings; no HTTP client crate for one request a day). Used by the new-version check.

use std::time::Duration;

#[cfg(windows)]
pub fn get(url: &str, user_agent: &str, timeout: Duration) -> Result<(u16, String), String> {
    use std::ffi::c_void;
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Networking::WinHttp::*;

    let u = tauri::Url::parse(url).map_err(|e| format!("bad url {url}: {e}"))?;
    let secure = match u.scheme() {
        "https" => true,
        "http" => false,
        s => return Err(format!("unsupported scheme {s}")),
    };
    let host = u.host_str().ok_or("url without host")?.to_string();
    let port = u.port_or_known_default().unwrap_or(if secure { 443 } else { 80 });
    let mut object = u.path().to_string();
    if let Some(q) = u.query() {
        object.push('?');
        object.push_str(q);
    }
    let w = |s: &str| s.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
    let (ua, host_w, obj_w, verb, headers) = (w(user_agent), w(&host), w(&object), w("GET"), w("Accept: application/vnd.github+json\r\n"));
    let ms = timeout.as_millis().min(i32::MAX as u128) as i32;

    struct H(*mut c_void);
    impl Drop for H {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { WinHttpCloseHandle(self.0) };
            }
        }
    }
    // WinHTTP's codes are not in the system message table, so name the usual ones
    let last = || {
        let code = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
        let what = match code {
            12002 => "timed out",
            12007 => "name not resolved (offline?)",
            12029 => "cannot connect",
            12030 => "connection dropped",
            12175 => "TLS / certificate problem",
            _ => "",
        };
        format!("winhttp error {code} {what}").trim_end().to_string()
    };
    unsafe {
        let session = H(WinHttpOpen(ua.as_ptr(), WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, null(), null(), 0));
        if session.0.is_null() {
            return Err(last());
        }
        WinHttpSetTimeouts(session.0, ms, ms, ms, ms);
        let conn = H(WinHttpConnect(session.0, host_w.as_ptr(), port, 0));
        if conn.0.is_null() {
            return Err(last());
        }
        let req = H(WinHttpOpenRequest(conn.0, verb.as_ptr(), obj_w.as_ptr(), null(), null(), null(), if secure { WINHTTP_FLAG_SECURE } else { 0 }));
        if req.0.is_null() {
            return Err(last());
        }
        if WinHttpSendRequest(req.0, headers.as_ptr(), u32::MAX, null(), 0, 0, 0) == 0 {
            return Err(last());
        }
        if WinHttpReceiveResponse(req.0, null_mut()) == 0 {
            return Err(last());
        }
        let mut status: u32 = 0;
        let mut len = std::mem::size_of::<u32>() as u32;
        if WinHttpQueryHeaders(req.0, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, null(), &mut status as *mut u32 as *mut c_void, &mut len, null_mut()) == 0 {
            return Err(last());
        }
        let mut body = Vec::new();
        let mut buf = vec![0u8; 16 * 1024];
        loop {
            let mut read: u32 = 0;
            if WinHttpReadData(req.0, buf.as_mut_ptr() as *mut c_void, buf.len() as u32, &mut read) == 0 {
                return Err(last());
            }
            if read == 0 {
                break;
            }
            body.extend_from_slice(&buf[..read as usize]);
            if body.len() > 4 * 1024 * 1024 {
                return Err("reply larger than 4 MB".into());
            }
        }
        Ok((status as u16, String::from_utf8_lossy(&body).into_owned()))
    }
}

#[cfg(not(windows))]
pub fn get(_url: &str, _user_agent: &str, _timeout: Duration) -> Result<(u16, String), String> {
    Err("only implemented on Windows".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn plain_http_get_against_a_local_listener() {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let t = std::thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut req = [0u8; 2048];
            let n = s.read(&mut req).unwrap();
            let head = String::from_utf8_lossy(&req[..n]).to_string();
            let body = r#"{"tag_name":"v9.9.9"}"#;
            write!(s, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            head
        });
        let (code, body) = get(&format!("http://127.0.0.1:{port}/repos/x/y/releases/latest?a=1"), "OmniAPI-desktop/test", Duration::from_secs(5)).unwrap();
        let head = t.join().unwrap();
        assert_eq!(code, 200);
        assert!(body.contains("v9.9.9"));
        assert!(head.starts_with("GET /repos/x/y/releases/latest?a=1 HTTP/1.1"), "{head}");
        assert!(head.contains("User-Agent: OmniAPI-desktop/test"), "{head}");
    }

    #[test]
    fn nothing_listening_is_an_error() {
        assert!(get("http://127.0.0.1:1/", "t", Duration::from_secs(3)).is_err());
        assert!(get("ftp://x/", "t", Duration::from_secs(3)).is_err());
    }
}
