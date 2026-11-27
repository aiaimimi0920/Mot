from __future__ import annotations

import base64
import hashlib
import hmac
import json
import queue
import socket
import struct
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .jcs import parse_strict_json
from .schemas import G2AError

MAX_RELAY_BODY_BYTES = 4 << 20
MAX_RELAY_SESSION_HISTORY = 256
MAX_RELAY_EVENTS_PER_SESSION = 64
_WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_encode_server_frame(opcode: int, payload: bytes = b"") -> bytes:
    hdr = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        hdr.append(n)
    elif n <= 65535:
        hdr.append(126)
        hdr.extend(struct.pack("!H", n))
    else:
        hdr.append(127)
        hdr.extend(struct.pack("!Q", n))
    return bytes(hdr) + payload


def _ws_recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("WebSocket closed")
        buf.extend(chunk)
    return bytes(buf)


def _token_sha256(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class CloudRelayServer:
    """
    Cloud-side NAT Outbound Relay for G2A Section 2.2 Row 3:
      - Home-PC Intelligent Hubs dial OUTBOUND to GET /g2a/v1/relay/down?hub_id=<id>
        and POST /g2a/v1/relay/up?hub_id=<id>;
      - Any standard G2A Computation Core connects transparently to
        http(s)://<relay>/hubs/<id>/g2a/v1/core and /hubs/<id>/g2a/v1/core/events!
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        relay_token: str = "",
        idle_keepalive_sec: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.relay_token = relay_token
        self.idle_keepalive_sec = idle_keepalive_sec
        self._lock = threading.Lock()
        self._hub_downlinks: Dict[str, "queue.Queue[Optional[Dict[str, Any]]]"] = {}
        self._pending_replies: Dict[str, tuple[threading.Event, Dict[str, Any]]] = {}
        self._sse_subscribers: Dict[str, set["queue.Queue[Optional[Dict[str, Any]]]"]] = {}
        self._event_history: OrderedDict[str, List[Dict[str, Any]]] = OrderedDict()
        self._session_token_digests: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._httpd: Optional[ThreadingHTTPServer] = None

    def _record_verified_session(self, hub_id: str, session_id: str, core_bearer: str) -> None:
        if not hub_id or not session_id or not core_bearer:
            return
        digest = _token_sha256(core_bearer)
        key = (hub_id, session_id)
        with self._lock:
            self._session_token_digests[key] = digest
            self._session_token_digests.move_to_end(key)
            while len(self._session_token_digests) > MAX_RELAY_SESSION_HISTORY:
                self._session_token_digests.popitem(last=False)

    def _evict_verified_session(self, hub_id: str, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            if hub_id:
                self._session_token_digests.pop((hub_id, session_id), None)
            else:
                stale = [k for k in self._session_token_digests if k[1] == session_id]
                for k in stale:
                    self._session_token_digests.pop(k, None)

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        h, p = self._httpd.server_address
        return f"http://{h}:{p}"

    def is_hub_connected(self, hub_id: str) -> bool:
        with self._lock:
            return hub_id in self._hub_downlinks

    def start(self) -> str:
        relay_ref = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _check_relay_token(self, qs: Optional[Dict[str, list[str]]] = None) -> bool:
                if not relay_ref.relay_token:
                    return True
                auth = self.headers.get("Authorization", "")
                cand = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if not cand and qs is not None:
                    cand = (qs.get("access_token") or [""])[0]
                return bool(cand) and hmac.compare_digest(cand.encode("utf-8"), relay_ref.relay_token.encode("utf-8"))

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)

                # 1. Home Hub Outbound Downlink SSE (/g2a/v1/relay/down?hub_id=...)
                if parsed.path == "/g2a/v1/relay/down":
                    if not self._check_relay_token(qs):
                        self._send_json(401, {"error": "unauthorized_relay_token"})
                        return
                    hub_id = (qs.get("hub_id") or [""])[0]
                    if not hub_id:
                        self._send_json(400, {"error": "missing_hub_id"})
                        return

                    q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=256)
                    with relay_ref._lock:
                        old_q = relay_ref._hub_downlinks.get(hub_id)
                        if old_q is not None:
                            try:
                                old_q.put_nowait(None)
                            except queue.Full:
                                pass
                        relay_ref._hub_downlinks[hub_id] = q
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "keep-alive")
                        self.end_headers()
                        self.wfile.write(b": relay-downlink-ready\n\n")
                        self.wfile.flush()
                        while True:
                            try:
                                item = q.get(timeout=relay_ref.idle_keepalive_sec)
                            except queue.Empty:
                                self.wfile.write(b": g2a-keepalive\n\n")
                                self.wfile.flush()
                                continue
                            if item is None:
                                break
                            raw = json.dumps(item, ensure_ascii=False).encode("utf-8")
                            self.wfile.write(b"event: core_req\ndata: " + raw + b"\n\n")
                            self.wfile.flush()
                    except Exception:
                        pass
                    finally:
                        with relay_ref._lock:
                            if relay_ref._hub_downlinks.get(hub_id) is q:
                                relay_ref._hub_downlinks.pop(hub_id, None)
                    return

                # 2. Computation Core SSE Stream (/hubs/<hub_id>/g2a/v1/core/events?session_id=...)
                if parsed.path.startswith("/hubs/") and parsed.path.endswith("/g2a/v1/core/events"):
                    parts = parsed.path.strip("/").split("/")
                    hub_id = parts[1] if len(parts) >= 2 else ""
                    session_id = (qs.get("session_id") or [""])[0]
                    if not session_id:
                        self._send_json(400, {"error": "missing_session_id"})
                        return

                    # Enforce Core bearer & session ownership on Relay SSE stream (§4.1)
                    auth_hdr = self.headers.get("Authorization", "")
                    core_bearer = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
                    if not core_bearer:
                        core_bearer = (qs.get("access_token") or [""])[0].strip()
                    if not core_bearer:
                        self._send_json(
                            401,
                            {
                                "g2a_version": "0.0.1",
                                "request_id": "",
                                "session_id": session_id,
                                "timestamp_ms": int(time.time() * 1000),
                                "acceptance": "not_accepted",
                                "result": None,
                                "error": {
                                    "code": "unauthenticated",
                                    "http_status": 401,
                                    "message": "Missing Core Bearer token on Relay SSE stream",
                                },
                            },
                        )
                        return

                    with relay_ref._lock:
                        down_q = relay_ref._hub_downlinks.get(hub_id)
                        cached_digest = relay_ref._session_token_digests.get((hub_id, session_id))
                    if down_q is not None:
                        hb_status, hb_reply, hb_retry = self._dispatch_downlink_rpc(
                            down_q,
                            core_bearer,
                            {
                                "g2a_version": "0.0.1",
                                "request_id": f"relay-auth-{uuid.uuid4().hex[:8]}",
                                "session_id": session_id,
                                "timestamp_ms": int(time.time() * 1000),
                                "operation": "session.heartbeat",
                                "payload": {},
                                "extensions": {},
                            },
                            orig_req_id="",
                        )
                        if hb_status != 200:
                            self._send_json(hb_status, hb_reply, retry_after=hb_retry)
                            return
                        relay_ref._record_verified_session(hub_id, session_id, core_bearer)
                    else:
                        if cached_digest is None:
                            self._send_json(
                                503,
                                {
                                    "g2a_version": "0.0.1",
                                    "request_id": "",
                                    "session_id": session_id,
                                    "timestamp_ms": int(time.time() * 1000),
                                    "acceptance": "not_accepted",
                                    "result": None,
                                    "error": {
                                        "code": "unavailable",
                                        "http_status": 503,
                                        "message": "Target Hub tunnel offline",
                                    },
                                },
                            )
                            return
                        if not hmac.compare_digest(cached_digest, _token_sha256(core_bearer)):
                            self._send_json(
                                403,
                                {
                                    "g2a_version": "0.0.1",
                                    "request_id": "",
                                    "session_id": session_id,
                                    "timestamp_ms": int(time.time() * 1000),
                                    "acceptance": "not_accepted",
                                    "result": None,
                                    "error": {
                                        "code": "permission_denied",
                                        "http_status": 403,
                                        "message": "Core token does not match verified session principal",
                                    },
                                },
                            )
                            return

                    replay = (qs.get("replay") or [""])[0] == "1"
                    last_event_id = self.headers.get("Last-Event-ID", "").strip()
                    if not last_event_id:
                        last_event_id = (qs.get("last_event_id") or [""])[0].strip()

                    ev_q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=256)
                    with relay_ref._lock:
                        hist = list(relay_ref._event_history.get(session_id, []))
                        if last_event_id:
                            found_idx = -1
                            for idx, h_ev in enumerate(hist):
                                if h_ev.get("event_id") == last_event_id:
                                    found_idx = idx
                                    break
                            to_replay = hist[found_idx + 1 :] if found_idx >= 0 else (hist if replay else [])
                        elif replay:
                            to_replay = hist
                        else:
                            to_replay = []
                        for h_ev in to_replay:
                            try:
                                ev_q.put_nowait(h_ev)
                            except queue.Full:
                                break
                        relay_ref._sse_subscribers.setdefault(session_id, set()).add(ev_q)
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "keep-alive")
                        self.end_headers()
                        self.wfile.write(b": relay-core-sse-ready\n\n")
                        self.wfile.flush()
                        while True:
                            try:
                                ev = ev_q.get(timeout=relay_ref.idle_keepalive_sec)
                            except queue.Empty:
                                self.wfile.write(b": g2a-keepalive\n\n")
                                self.wfile.flush()
                                continue
                            if ev is None:
                                break
                            raw = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                            ev_id = str(ev.get("event_id", "")).encode("utf-8")
                            ev_name = str(ev.get("event", "message")).encode("utf-8")
                            id_prefix = (b"id: " + ev_id + b"\n") if ev_id else b""
                            self.wfile.write(id_prefix + b"event: " + ev_name + b"\ndata: " + raw + b"\n\n")
                            self.wfile.flush()
                    except Exception:
                        pass
                    finally:
                        with relay_ref._lock:
                            subs_set = relay_ref._sse_subscribers.get(session_id)
                            if subs_set is not None:
                                subs_set.discard(ev_q)
                                if not subs_set:
                                    relay_ref._sse_subscribers.pop(session_id, None)
                    return

                # 3. Computation Core Health Probe (/hubs/<hub_id>/g2a/v1/health)
                if parsed.path.startswith("/hubs/") and parsed.path.endswith("/g2a/v1/health"):
                    parts = parsed.path.strip("/").split("/")
                    hub_id = parts[1] if len(parts) >= 2 else ""
                    online = relay_ref.is_hub_connected(hub_id)
                    self._send_json(
                        200 if online else 503,
                        {"status": "ok" if online else "offline", "g2a_version": "0.0.1", "interface": "core_relay", "hub_id": hub_id},
                    )
                    return

                # 4. Computation Core RFC 6455 WebSocket Stream (/hubs/<hub_id>/g2a/v1/core/ws?session_id=...)
                if parsed.path.startswith("/hubs/") and parsed.path.endswith("/g2a/v1/core/ws"):
                    parts = parsed.path.strip("/").split("/")
                    hub_id = parts[1] if len(parts) >= 2 else ""
                    session_id = (qs.get("session_id") or [""])[0]
                    if not session_id:
                        self._send_json(400, {"error": "missing_session_id"})
                        return
                    if self.headers.get("Upgrade", "").strip().lower() != "websocket":
                        self._send_json(400, {"error": "missing_websocket_upgrade"})
                        return
                    if self.headers.get("Sec-WebSocket-Version", "").strip() != "13":
                        self.send_response(426)
                        self.send_header("Sec-WebSocket-Version", "13")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    ws_key = self.headers.get("Sec-WebSocket-Key", "").strip()
                    if not ws_key:
                        self._send_json(400, {"error": "missing_sec_websocket_key"})
                        return

                    auth_hdr = self.headers.get("Authorization", "")
                    core_bearer = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
                    if not core_bearer:
                        core_bearer = (qs.get("access_token") or [""])[0].strip()
                    if not core_bearer:
                        self._send_json(
                            401,
                            {
                                "g2a_version": "0.0.1",
                                "request_id": "",
                                "session_id": session_id,
                                "timestamp_ms": int(time.time() * 1000),
                                "acceptance": "not_accepted",
                                "result": None,
                                "error": {"code": "unauthenticated", "http_status": 401, "message": "Missing Core Bearer token on Relay WS stream"},
                            },
                        )
                        return

                    with relay_ref._lock:
                        down_q = relay_ref._hub_downlinks.get(hub_id)
                        cached_digest = relay_ref._session_token_digests.get((hub_id, session_id))
                    if down_q is not None:
                        hb_status, hb_reply, hb_retry = self._dispatch_downlink_rpc(
                            down_q,
                            core_bearer,
                            {
                                "g2a_version": "0.0.1",
                                "request_id": f"relay-ws-auth-{uuid.uuid4().hex[:8]}",
                                "session_id": session_id,
                                "timestamp_ms": int(time.time() * 1000),
                                "operation": "session.heartbeat",
                                "payload": {},
                                "extensions": {},
                            },
                            orig_req_id="",
                        )
                        if hb_status != 200:
                            self._send_json(hb_status, hb_reply, retry_after=hb_retry)
                            return
                        relay_ref._record_verified_session(hub_id, session_id, core_bearer)
                    else:
                        if cached_digest is None:
                            self._send_json(
                                503,
                                {
                                    "g2a_version": "0.0.1",
                                    "request_id": "",
                                    "session_id": session_id,
                                    "timestamp_ms": int(time.time() * 1000),
                                    "acceptance": "not_accepted",
                                    "result": None,
                                    "error": {"code": "unavailable", "http_status": 503, "message": "Target Hub tunnel offline"},
                                },
                            )
                            return
                        if not hmac.compare_digest(cached_digest, _token_sha256(core_bearer)):
                            self._send_json(
                                403,
                                {
                                    "g2a_version": "0.0.1",
                                    "request_id": "",
                                    "session_id": session_id,
                                    "timestamp_ms": int(time.time() * 1000),
                                    "acceptance": "not_accepted",
                                    "result": None,
                                    "error": {"code": "permission_denied", "http_status": 403, "message": "Core token mismatch"},
                                },
                            )
                            return

                    replay = (qs.get("replay") or [""])[0] == "1"
                    last_event_id = self.headers.get("Last-Event-ID", "").strip()
                    if not last_event_id:
                        last_event_id = (qs.get("last_event_id") or [""])[0].strip()

                    ev_q = queue.Queue(maxsize=256)
                    with relay_ref._lock:
                        hist = list(relay_ref._event_history.get(session_id, []))
                        if last_event_id:
                            found_idx = -1
                            for idx, h_ev in enumerate(hist):
                                if h_ev.get("event_id") == last_event_id:
                                    found_idx = idx
                                    break
                            to_replay = hist[found_idx + 1 :] if found_idx >= 0 else (hist if replay else [])
                        elif replay:
                            to_replay = hist
                        else:
                            to_replay = []
                        for h_ev in to_replay:
                            try:
                                ev_q.put_nowait(h_ev)
                            except queue.Full:
                                break
                        relay_ref._sse_subscribers.setdefault(session_id, set()).add(ev_q)

                    accept_val = base64.b64encode(
                        hashlib.sha1((ws_key + _WS_MAGIC_GUID).encode("ascii")).digest()
                    ).decode("ascii")
                    self.close_connection = True
                    raw_sock = self.connection
                    write_lock = threading.Lock()
                    ws_closed = threading.Event()

                    def _ws_send(opcode: int, payload: bytes = b"") -> bool:
                        with write_lock:
                            try:
                                raw_sock.sendall(_ws_encode_server_frame(opcode, payload))
                                return True
                            except OSError:
                                return False

                    def _ws_reader() -> None:
                        try:
                            while not ws_closed.is_set():
                                b0, b1 = _ws_recv_exact(raw_sock, 2)
                                fin = (b0 & 0x80) != 0
                                rsv = b0 & 0x70
                                opcode = b0 & 0x0F
                                masked = (b1 & 0x80) != 0
                                length = b1 & 0x7F
                                if rsv != 0 or not masked or (opcode >= 0x8 and (not fin or length > 125)):
                                    _ws_send(0x8, struct.pack("!H", 1002))
                                    break
                                if length == 126:
                                    length = struct.unpack("!H", _ws_recv_exact(raw_sock, 2))[0]
                                elif length == 127:
                                    length = struct.unpack("!Q", _ws_recv_exact(raw_sock, 8))[0]
                                if length > (1 << 20):
                                    _ws_send(0x8, struct.pack("!H", 1009))
                                    break
                                mask_key = _ws_recv_exact(raw_sock, 4)
                                data = _ws_recv_exact(raw_sock, length) if length > 0 else b""
                                if data:
                                    data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
                                if opcode == 0x8:
                                    _ws_send(0x8, b"")
                                    break
                                if opcode == 0x9:
                                    _ws_send(0xA, data)
                        except (EOFError, OSError):
                            pass
                        finally:
                            ws_closed.set()
                            try:
                                ev_q.put_nowait(None)
                            except queue.Full:
                                pass

                    try:
                        handshake_resp = (
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
                        ).encode("ascii")
                        raw_sock.sendall(handshake_resp)
                        raw_sock.settimeout(None)
                        threading.Thread(target=_ws_reader, daemon=True).start()
                        while not ws_closed.is_set():
                            try:
                                ev = ev_q.get(timeout=relay_ref.idle_keepalive_sec)
                            except queue.Empty:
                                if not _ws_send(0x9, b"g2a"):
                                    break
                                continue
                            if ev is None:
                                _ws_send(0x8, struct.pack("!H", 1000))
                                break
                            raw = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                            if not _ws_send(0x1, raw):
                                break
                    except OSError:
                        pass
                    finally:
                        ws_closed.set()
                        with relay_ref._lock:
                            subs_set = relay_ref._sse_subscribers.get(session_id)
                            if subs_set is not None:
                                subs_set.discard(ev_q)
                                if not subs_set:
                                    relay_ref._sse_subscribers.pop(session_id, None)
                    return

                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "invalid_content_length"})
                    return
                if length < 0 or length > MAX_RELAY_BODY_BYTES:
                    self._send_json(413, {"error": "payload_too_large"})
                    return
                raw_body = self.rfile.read(length)

                # 1. Home Hub Outbound Uplink (/g2a/v1/relay/up?hub_id=...)
                if parsed.path == "/g2a/v1/relay/up":
                    if not self._check_relay_token():
                        self._send_json(401, {"error": "unauthorized_relay_token"})
                        return
                    try:
                        up = parse_strict_json(raw_body)
                        if not isinstance(up, dict):
                            raise G2AError("invalid_request", 400, "Expected JSON object")
                    except (G2AError, UnicodeDecodeError) as jerr:
                        self._send_json(400, {"error": str(jerr)})
                        return
                    kind = up.get("kind")
                    if kind == "reply":
                        t_id = str(up.get("tunnel_req_id", ""))
                        with relay_ref._lock:
                            entry = relay_ref._pending_replies.get(t_id)
                        if entry is not None:
                            ev_obj, holder = entry
                            holder["status"] = int(up.get("http_status", 200))
                            holder["retry_after"] = str(up.get("retry_after") or "")
                            holder["reply"] = up.get("reply")
                            ev_obj.set()
                    elif kind == "event":
                        ev_env = up.get("event")
                        if isinstance(ev_env, dict):
                            sid = str(ev_env.get("session_id", ""))
                            is_closed_ev = ev_env.get("event") == "session.closed"
                            up_hub_id = (parse_qs(parsed.query).get("hub_id") or [""])[0]
                            if is_closed_ev and sid:
                                relay_ref._evict_verified_session(up_hub_id, sid)
                            with relay_ref._lock:
                                if sid:
                                    hist_list = relay_ref._event_history.setdefault(sid, [])
                                    relay_ref._event_history.move_to_end(sid)
                                    hist_list.append(ev_env)
                                    if len(hist_list) > MAX_RELAY_EVENTS_PER_SESSION:
                                        del hist_list[:-MAX_RELAY_EVENTS_PER_SESSION]
                                    while len(relay_ref._event_history) > MAX_RELAY_SESSION_HISTORY:
                                        relay_ref._event_history.popitem(last=False)
                                subs = list(relay_ref._sse_subscribers.get(sid, set()))
                            for sub_q in subs:
                                try:
                                    sub_q.put_nowait(ev_env)
                                    if is_closed_ev:
                                        sub_q.put_nowait(None)
                                except queue.Full:
                                    pass
                    self._send_json(200, {"ok": True})
                    return

                # 2. Computation Core Request (/hubs/<hub_id>/g2a/v1/core)
                if parsed.path.startswith("/hubs/") and parsed.path.endswith("/g2a/v1/core"):
                    parts = parsed.path.strip("/").split("/")
                    hub_id = parts[1]
                    try:
                        body_obj = parse_strict_json(raw_body)
                        if not isinstance(body_obj, dict):
                            raise G2AError("invalid_request", 400, "Expected RequestEnvelope JSON object")
                    except (G2AError, UnicodeDecodeError) as jerr:
                        self._send_json(400, {
                            "g2a_version": "0.0.1", "request_id": "", "timestamp_ms": 0,
                            "acceptance": "not_accepted", "result": None,
                            "error": {"code": "invalid_request", "http_status": 400, "message": str(jerr)},
                        })
                        return
                    orig_req_id = str(body_obj.get("request_id", ""))
                    with relay_ref._lock:
                        down_q = relay_ref._hub_downlinks.get(hub_id)
                    if down_q is None:
                        self._send_json(503, {
                            "g2a_version": "0.0.1", "request_id": orig_req_id, "timestamp_ms": 0,
                            "acceptance": "not_accepted", "result": None,
                            "error": {"code": "unavailable", "http_status": 503, "message": "Target Hub tunnel offline"}
                        })
                        return

                    auth_hdr = self.headers.get("Authorization", "")
                    core_bearer = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
                    status, reply_obj, retry_after = self._dispatch_downlink_rpc(
                        down_q, core_bearer, body_obj, orig_req_id=orig_req_id
                    )
                    if status == 200 and isinstance(reply_obj, dict):
                        res_obj = reply_obj.get("result")
                        verified_sid = str(
                            body_obj.get("session_id")
                            or (res_obj.get("session_id") if isinstance(res_obj, dict) else "")
                            or ""
                        )
                        if body_obj.get("operation") == "session.close" and verified_sid:
                            relay_ref._evict_verified_session(hub_id, verified_sid)
                        elif verified_sid and core_bearer:
                            relay_ref._record_verified_session(hub_id, verified_sid, core_bearer)
                    self._send_json(status, reply_obj, retry_after=retry_after)
                    return

                self._send_json(404, {"error": "not_found"})

            def _dispatch_downlink_rpc(
                self,
                down_q: "queue.Queue[Optional[Dict[str, Any]]]",
                core_bearer: str,
                body_obj: Dict[str, Any],
                orig_req_id: str,
            ) -> tuple[int, Any, str]:
                t_id = f"treq-{uuid.uuid4().hex[:12]}"
                done_ev = threading.Event()
                holder: Dict[str, Any] = {}
                with relay_ref._lock:
                    relay_ref._pending_replies[t_id] = (done_ev, holder)
                try:
                    down_q.put_nowait({
                        "tunnel_req_id": t_id,
                        "core_bearer": core_bearer,
                        "body": body_obj,
                    })
                    if not done_ev.wait(timeout=12.0):
                        return (
                            504,
                            {
                                "g2a_version": "0.0.1",
                                "request_id": orig_req_id,
                                "timestamp_ms": 0,
                                "acceptance": "unknown",
                                "result": None,
                                "error": {
                                    "code": "outcome_unknown",
                                    "http_status": 504,
                                    "message": "Hub tunnel reply timeout",
                                },
                            },
                            "",
                        )
                    return holder["status"], holder["reply"], holder.get("retry_after", "")
                except queue.Full:
                    return (
                        429,
                        {
                            "g2a_version": "0.0.1",
                            "request_id": orig_req_id,
                            "timestamp_ms": 0,
                            "acceptance": "not_accepted",
                            "result": None,
                            "error": {
                                "code": "resource_limit",
                                "http_status": 429,
                                "message": "Relay downlink buffer full",
                            },
                        },
                        "1",
                    )
                finally:
                    with relay_ref._lock:
                        relay_ref._pending_replies.pop(t_id, None)

            def _send_json(self, status: int, obj: Any, retry_after: str = "") -> None:
                out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                want_close = self.headers.get("Connection", "").strip().lower() == "close"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                if retry_after:
                    self.send_header("Retry-After", retry_after)
                self.send_header("Connection", "close" if want_close else "keep-alive")
                self.end_headers()
                self.wfile.write(out)

        class _QuietThreadingHTTPServer(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                return

        self._httpd = _QuietThreadingHTTPServer((self.host, self.port), _Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        with self._lock:
            for q in self._hub_downlinks.values():
                q.put(None)
            for subs in self._sse_subscribers.values():
                for sq in subs:
                    sq.put(None)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
