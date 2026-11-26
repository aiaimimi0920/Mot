//! Zero-dependency HTTP/1.1 Keep-Alive transport, loopback execute listener, and outbound SSE pull stream.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::jcs::{canonicalize_utf8, parse_strict_json, JsonObject, JsonValue};
use crate::sha256::constant_time_eq;
use crate::{G2A_WIRE_VERSION, OriginExecuteRequest, OriginSdk};

pub const PULL_EXECUTE_SENTINEL: &str = "pull://origin.execute";

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

fn parse_http_host_port_path(base_url: &str, suffix: &str) -> Result<(String, u16, String), String> {
    let trimmed = base_url.trim_end_matches('/');
    let rest = trimmed
        .strip_prefix("http://")
        .ok_or_else(|| format!("Unsupported non-http loopback URL: {base_url}"))?;
    let (host_port, base_path) = match rest.find('/') {
        Some(idx) => (&rest[..idx], &rest[idx..]),
        None => (rest, ""),
    };
    let (host, port) = match host_port.rsplit_once(':') {
        Some((h, p)) => (
            h.trim_matches(['[', ']']).to_owned(),
            p.parse::<u16>().map_err(|_| "Invalid port".to_owned())?,
        ),
        None => (host_port.to_owned(), 80),
    };
    Ok((host, port, format!("{base_path}{suffix}")))
}

pub fn discover_local_hub_origin_url(explicit_manifest_path: Option<&str>) -> Result<String, String> {
    let path = if let Some(p) = explicit_manifest_path {
        p.to_owned()
    } else if let Ok(env_p) = std::env::var("G2A_HUB_RUNTIME_FILE") {
        if !env_p.is_empty() {
            env_p
        } else {
            default_runtime_manifest_path()?
        }
    } else {
        default_runtime_manifest_path()?
    };
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("Local Hub runtime manifest not found at {path}: {e}"))?;
    let doc = parse_strict_json(&raw)?;
    let obj = doc.as_object().ok_or_else(|| "Manifest root must be object".to_owned())?;
    obj.get("origin_url")
        .and_then(JsonValue::as_str)
        .map(|s| s.trim_end_matches('/').to_owned())
        .ok_or_else(|| "Missing origin_url in Hub runtime manifest".to_owned())
}

fn default_runtime_manifest_path() -> Result<String, String> {
    if cfg!(windows) {
        if let Ok(local_app) = std::env::var("LOCALAPPDATA") {
            if !local_app.is_empty() {
                return Ok(format!("{local_app}\\G2A\\hub.runtime.json"));
            }
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        if !home.is_empty() {
            return Ok(format!("{home}/.g2a/hub.runtime.json"));
        }
    }
    Err("Cannot determine G2A hub.runtime.json path".to_owned())
}

pub struct HttpHubTransportClient {
    host: String,
    port: u16,
    path: String,
    shared_token: String,
    conn: Mutex<Option<TcpStream>>,
    seq: Mutex<u64>,
}

impl HttpHubTransportClient {
    pub fn new(hub_origin_url: &str, shared_token: &str) -> Result<Self, String> {
        let (host, port, path) = parse_http_host_port_path(hub_origin_url, "/g2a/v1/origin")?;
        Ok(Self {
            host,
            port,
            path,
            shared_token: shared_token.to_owned(),
            conn: Mutex::new(None),
            seq: Mutex::new(0),
        })
    }

    pub fn from_local_hub_discovery(explicit_manifest: Option<&str>, shared_token: &str) -> Result<Self, String> {
        let url = discover_local_hub_origin_url(explicit_manifest)?;
        Self::new(&url, shared_token)
    }

    pub fn post_to_hub(&self, operation: &str, payload: &JsonValue) -> Result<JsonValue, String> {
        let req_num = {
            let mut g = self.seq.lock().map_err(|_| "Mutex poisoned".to_owned())?;
            *g += 1;
            *g
        };
        let mut env = JsonObject::new();
        env.insert("g2a_version".to_owned(), JsonValue::from(G2A_WIRE_VERSION));
        env.insert("request_id".to_owned(), JsonValue::from(format!("rs-orig-{}-{}", now_ms(), req_num)));
        env.insert("timestamp_ms".to_owned(), JsonValue::from(now_ms()));
        env.insert("operation".to_owned(), JsonValue::from(operation));
        env.insert("payload".to_owned(), payload.clone());
        env.insert("extensions".to_owned(), JsonValue::Object(JsonObject::new()));

        let body_str = canonicalize_utf8(&JsonValue::Object(env))?;
        let body_bytes = body_str.as_bytes();
        let auth_hdr = if self.shared_token.is_empty() {
            String::new()
        } else {
            format!("Authorization: Bearer {}\r\n", self.shared_token)
        };
        let req_head = format!(
            "POST {} HTTP/1.1\r\nHost: {}:{}\r\nContent-Type: application/json\r\nConnection: keep-alive\r\n{}Content-Length: {}\r\n\r\n",
            self.path, self.host, self.port, auth_hdr, body_bytes.len()
        );

        let mut guard = self.conn.lock().map_err(|_| "Mutex poisoned".to_owned())?;
        for attempt in 0..2 {
            if guard.is_none() {
                let s = TcpStream::connect((self.host.as_str(), self.port))
                    .map_err(|e| format!("Connect failed to {}:{}: {e}", self.host, self.port))?;
                let _ = s.set_nodelay(true);
                let _ = s.set_read_timeout(Some(Duration::from_secs(8)));
                let _ = s.set_write_timeout(Some(Duration::from_secs(8)));
                *guard = Some(s);
            }
            let io_res = (|| -> Result<(u16, String, bool), std::io::Error> {
                let stream = guard.as_mut().unwrap();
                stream.write_all(req_head.as_bytes())?;
                stream.write_all(body_bytes)?;
                stream.flush()?;
                read_http_response(stream)
            })();
            match io_res {
                Ok((status, resp_body, peer_close)) => {
                    if peer_close {
                        *guard = None;
                    }
                    if !(200..300).contains(&status) {
                        return Err(format!("Hub HTTP {status}: {resp_body}"));
                    }
                    return parse_strict_json(&resp_body);
                }
                Err(e) => {
                    *guard = None;
                    if attempt == 1 {
                        return Err(format!("HTTP transport error: {e}"));
                    }
                }
            }
        }
        Err("Unreachable transport retry exhaustion".to_owned())
    }
}

fn read_http_response(stream: &mut TcpStream) -> Result<(u16, String, bool), std::io::Error> {
    let mut reader = BufReader::new(stream);
    let mut status_line = String::new();
    if reader.read_line(&mut status_line)? == 0 {
        return Err(std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "EOF reading status"));
    }
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(500);
    let mut content_len = 0usize;
    let mut close_conn = false;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        let trimmed = line.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            break;
        }
        if let Some((k, v)) = trimmed.split_once(':') {
            if k.eq_ignore_ascii_case("content-length") {
                content_len = v.trim().parse().unwrap_or(0);
            } else if k.eq_ignore_ascii_case("connection") && v.trim().eq_ignore_ascii_case("close") {
                close_conn = true;
            }
        }
    }
    let mut body = vec![0u8; content_len];
    reader.read_exact(&mut body)?;
    let s = String::from_utf8(body)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    Ok((status, s, close_conn))
}

pub struct LoopbackExecuteListener {
    execute_url: String,
    stop_flag: Arc<AtomicBool>,
    bound_port: u16,
    worker: Option<JoinHandle<()>>,
}

impl LoopbackExecuteListener {
    pub fn start(sdk: Arc<OriginSdk>, port: u16, shared_token: &str) -> Result<Self, String> {
        let listener = TcpListener::bind(("127.0.0.1", port))
            .map_err(|e| format!("Failed to bind loopback execute listener: {e}"))?;
        let bound_port = listener
            .local_addr()
            .map_err(|e| format!("local_addr failed: {e}"))?
            .port();
        let execute_url = format!("http://127.0.0.1:{bound_port}/g2a/v1/origin/execute");
        let stop_flag = Arc::new(AtomicBool::new(false));
        let stop_clone = Arc::clone(&stop_flag);
        let tok = shared_token.to_owned();

        let worker = thread::spawn(move || {
            while !stop_clone.load(Ordering::Relaxed) {
                let Ok((stream, _)) = listener.accept() else {
                    break;
                };
                if stop_clone.load(Ordering::Relaxed) {
                    break;
                }
                let sdk_ref = Arc::clone(&sdk);
                let tok_ref = tok.clone();
                let stop_inner = Arc::clone(&stop_clone);
                thread::spawn(move || handle_execute_client(stream, sdk_ref, &tok_ref, stop_inner));
            }
        });

        Ok(Self {
            execute_url,
            stop_flag,
            bound_port,
            worker: Some(worker),
        })
    }

    pub fn execute_url(&self) -> &str {
        &self.execute_url
    }
}

impl Drop for LoopbackExecuteListener {
    fn drop(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(("127.0.0.1", self.bound_port));
        if let Some(h) = self.worker.take() {
            let _ = h.join();
        }
    }
}

fn handle_execute_client(
    stream: TcpStream,
    sdk: Arc<OriginSdk>,
    shared_token: &str,
    stop_flag: Arc<AtomicBool>,
) {
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    let Ok(mut writer) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(stream);

    while !stop_flag.load(Ordering::Relaxed) {
        let mut req_line = String::new();
        match reader.read_line(&mut req_line) {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        let mut headers = HashMap::new();
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
            let trimmed = line.trim_end_matches(['\r', '\n']);
            if trimmed.is_empty() {
                break;
            }
            if let Some((k, v)) = trimmed.split_once(':') {
                headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_owned());
            }
        }
        let want_close = headers
            .get("connection")
            .map(|v| v.eq_ignore_ascii_case("close"))
            .unwrap_or(false);
        let content_len: usize = headers
            .get("content-length")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        if content_len > (4 << 20) {
            let _ = write_json_reply(&mut writer, 413, "", "not_accepted", None, Some(("invalid_request", 413, "Payload exceeds 4 MiB")), true);
            return;
        }
        let mut body = vec![0u8; content_len];
        if reader.read_exact(&mut body).is_err() {
            return;
        }

        if !shared_token.is_empty() {
            let expected = format!("Bearer {shared_token}");
            let got = headers.get("authorization").map(String::as_str).unwrap_or("");
            if !constant_time_eq(got.as_bytes(), expected.as_bytes()) {
                let _ = write_json_reply(
                    &mut writer,
                    401,
                    "",
                    "not_accepted",
                    None,
                    Some(("unauthenticated", 401, "Invalid Origin Bearer token")),
                    want_close,
                );
                if want_close {
                    return;
                }
                continue;
            }
        }

        let body_str = match String::from_utf8(body) {
            Ok(s) => s,
            Err(_) => {
                let _ = write_json_reply(&mut writer, 400, "", "not_accepted", None, Some(("invalid_request", 400, "Invalid UTF-8")), want_close);
                if want_close {
                    return;
                }
                continue;
            }
        };

        let parsed = parse_strict_json(&body_str);
        match parsed {
            Ok(root) => {
                let Some(obj) = root.as_object() else {
                    let _ = write_json_reply(&mut writer, 400, "", "not_accepted", None, Some(("invalid_request", 400, "Expected JSON object")), want_close);
                    continue;
                };
                let req_id = obj.get("request_id").and_then(JsonValue::as_str).unwrap_or("").to_owned();
                let op = obj.get("operation").and_then(JsonValue::as_str).unwrap_or("");
                if op != "origin.execute" {
                    let _ = write_json_reply(&mut writer, 400, &req_id, "not_accepted", None, Some(("invalid_request", 400, "Expected origin.execute")), want_close);
                    continue;
                }
                let Some(payload_obj) = obj.get("payload").and_then(JsonValue::as_object) else {
                    let _ = write_json_reply(&mut writer, 400, &req_id, "not_accepted", None, Some(("invalid_request", 400, "Missing payload")), want_close);
                    continue;
                };
                match OriginExecuteRequest::from_json(payload_obj) {
                    Ok(exec_req) => {
                        let rep = sdk.handle_origin_execute(&exec_req);
                        let _ = write_json_reply(&mut writer, 200, &req_id, "accepted", Some(rep.to_json()), None, want_close);
                    }
                    Err(msg) => {
                        let _ = write_json_reply(&mut writer, 400, &req_id, "not_accepted", None, Some(("invalid_arguments", 400, &msg)), want_close);
                    }
                }
            }
            Err(err) => {
                let _ = write_json_reply(&mut writer, 400, "", "not_accepted", None, Some(("invalid_request", 400, &err)), want_close);
            }
        }
        if want_close {
            return;
        }
    }
}

fn write_json_reply(
    writer: &mut TcpStream,
    status: u16,
    req_id: &str,
    acceptance: &str,
    result: Option<JsonValue>,
    err_tuple: Option<(&str, i64, &str)>,
    close_conn: bool,
) -> std::io::Result<()> {
    let mut env = JsonObject::new();
    env.insert("g2a_version".to_owned(), JsonValue::from(G2A_WIRE_VERSION));
    env.insert("request_id".to_owned(), JsonValue::from(req_id));
    env.insert("timestamp_ms".to_owned(), JsonValue::from(now_ms()));
    env.insert("acceptance".to_owned(), JsonValue::from(acceptance));
    env.insert("result".to_owned(), result.unwrap_or(JsonValue::Null));
    if let Some((code, http_st, msg)) = err_tuple {
        let mut e = JsonObject::new();
        e.insert("code".to_owned(), JsonValue::from(code));
        e.insert("http_status".to_owned(), JsonValue::from(http_st));
        e.insert("message".to_owned(), JsonValue::from(msg));
        env.insert("error".to_owned(), JsonValue::Object(e));
    } else {
        env.insert("error".to_owned(), JsonValue::Null);
    }
    let body = canonicalize_utf8(&JsonValue::Object(env)).unwrap_or_else(|_| "{}".to_owned());
    let conn_hdr = if close_conn { "close" } else { "keep-alive" };
    let wire = format!(
        "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: {conn_hdr}\r\n\r\n{body}",
        body.len()
    );
    writer.write_all(wire.as_bytes())?;
    writer.flush()
}

pub struct OutboundExecuteStreamClient {
    stop_flag: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl OutboundExecuteStreamClient {
    pub fn start(
        sdk: Arc<OriginSdk>,
        hub_origin_url: &str,
        slot_id: &str,
        shared_token: &str,
    ) -> Result<Self, String> {
        let (host, port, stream_path) = parse_http_host_port_path(
            hub_origin_url,
            &format!("/g2a/v1/origin/execute-stream?slot_id={slot_id}"),
        )?;
        let (_, _, reply_path) = parse_http_host_port_path(hub_origin_url, "/g2a/v1/origin/execute-reply")?;
        let stop_flag = Arc::new(AtomicBool::new(false));
        let stop_clone = Arc::clone(&stop_flag);
        let tok = shared_token.to_owned();

        let worker = thread::spawn(move || {
            let mut reply_conn: Option<TcpStream> = None;
            while !stop_clone.load(Ordering::Relaxed) {
                let Ok(mut sse_sock) = TcpStream::connect((host.as_str(), port)) else {
                    thread::sleep(Duration::from_millis(120));
                    continue;
                };
                let _ = sse_sock.set_nodelay(true);
                let auth_hdr = if tok.is_empty() {
                    String::new()
                } else {
                    format!("Authorization: Bearer {tok}\r\n")
                };
                let get_req = format!(
                    "GET {stream_path} HTTP/1.1\r\nHost: {host}:{port}\r\nAccept: text/event-stream\r\n{auth_hdr}\r\n"
                );
                if sse_sock.write_all(get_req.as_bytes()).is_err() {
                    thread::sleep(Duration::from_millis(120));
                    continue;
                }
                let mut reader = BufReader::new(sse_sock);
                // Skip HTTP response headers
                let mut ok_200 = false;
                let mut status_ln = String::new();
                if reader.read_line(&mut status_ln).unwrap_or(0) > 0 && status_ln.contains(" 200 ") {
                    ok_200 = true;
                }
                loop {
                    let mut h = String::new();
                    if reader.read_line(&mut h).unwrap_or(0) == 0 || h.trim_end_matches(['\r', '\n']).is_empty() {
                        break;
                    }
                }
                if !ok_200 {
                    thread::sleep(Duration::from_millis(150));
                    continue;
                }

                let mut data_lines: Vec<String> = Vec::new();
                while !stop_clone.load(Ordering::Relaxed) {
                    let mut ln = String::new();
                    match reader.read_line(&mut ln) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {}
                    }
                    let trimmed = ln.trim_end_matches(['\r', '\n']);
                    if trimmed.is_empty() {
                        if !data_lines.is_empty() {
                            let joined = data_lines.join("\n");
                            data_lines.clear();
                            if let Ok(env_val) = parse_strict_json(&joined) {
                                if let Some(env_obj) = env_val.as_object() {
                                    let req_id = env_obj.get("request_id").and_then(JsonValue::as_str).unwrap_or("");
                                    let reply_body = if let Some(p_obj) = env_obj.get("payload").and_then(JsonValue::as_object) {
                                        match OriginExecuteRequest::from_json(p_obj) {
                                            Ok(ex_req) => {
                                                let rep = sdk.handle_origin_execute(&ex_req);
                                                build_reply_envelope_json(req_id, "accepted", Some(rep.to_json()), None)
                                            }
                                            Err(msg) => build_reply_envelope_json(req_id, "not_accepted", None, Some(("invalid_arguments", 400, &msg))),
                                        }
                                    } else {
                                        build_reply_envelope_json(req_id, "not_accepted", None, Some(("invalid_request", 400, "Missing payload")))
                                    };
                                    post_keepalive_reply(&mut reply_conn, &host, port, &reply_path, &tok, &reply_body);
                                }
                            }
                        }
                    } else if let Some(rest) = trimmed.strip_prefix("data:") {
                        data_lines.push(rest.trim_start_matches(' ').to_owned());
                    }
                }
            }
        });

        Ok(Self {
            stop_flag,
            worker: Some(worker),
        })
    }
}

impl Drop for OutboundExecuteStreamClient {
    fn drop(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        let _ = self.worker.take();
    }
}

fn build_reply_envelope_json(
    req_id: &str,
    acceptance: &str,
    result: Option<JsonValue>,
    err_tuple: Option<(&str, i64, &str)>,
) -> String {
    let mut env = JsonObject::new();
    env.insert("g2a_version".to_owned(), JsonValue::from(G2A_WIRE_VERSION));
    env.insert("request_id".to_owned(), JsonValue::from(req_id));
    env.insert("timestamp_ms".to_owned(), JsonValue::from(now_ms()));
    env.insert("acceptance".to_owned(), JsonValue::from(acceptance));
    env.insert("result".to_owned(), result.unwrap_or(JsonValue::Null));
    if let Some((code, st, msg)) = err_tuple {
        let mut e = JsonObject::new();
        e.insert("code".to_owned(), JsonValue::from(code));
        e.insert("http_status".to_owned(), JsonValue::from(st));
        e.insert("message".to_owned(), JsonValue::from(msg));
        env.insert("error".to_owned(), JsonValue::Object(e));
    } else {
        env.insert("error".to_owned(), JsonValue::Null);
    }
    canonicalize_utf8(&JsonValue::Object(env)).unwrap_or_else(|_| "{}".to_owned())
}

fn post_keepalive_reply(
    conn_slot: &mut Option<TcpStream>,
    host: &str,
    port: u16,
    reply_path: &str,
    tok: &str,
    body_str: &str,
) {
    let auth_hdr = if tok.is_empty() {
        String::new()
    } else {
        format!("Authorization: Bearer {tok}\r\n")
    };
    let head = format!(
        "POST {reply_path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\nConnection: keep-alive\r\n{auth_hdr}Content-Length: {}\r\n\r\n",
        body_str.len()
    );
    for _ in 0..2 {
        if conn_slot.is_none() {
            if let Ok(s) = TcpStream::connect((host, port)) {
                let _ = s.set_nodelay(true);
                let _ = s.set_read_timeout(Some(Duration::from_secs(5)));
                *conn_slot = Some(s);
            } else {
                return;
            }
        }
        let ok = (|| -> std::io::Result<bool> {
            let s = conn_slot.as_mut().unwrap();
            s.write_all(head.as_bytes())?;
            s.write_all(body_str.as_bytes())?;
            s.flush()?;
            let (_, _, peer_close) = read_http_response(s)?;
            Ok(peer_close)
        })();
        match ok {
            Ok(peer_close) => {
                if peer_close {
                    *conn_slot = None;
                }
                return;
            }
            Err(_) => {
                *conn_slot = None;
            }
        }
    }
}
