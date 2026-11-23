from __future__ import annotations

import json
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse


class CloudRelayServer:
    """
    Cloud-side NAT Outbound Relay for G2A Section 2.2 Row 3:
      - Home-PC Intelligent Hubs dial OUTBOUND to GET /g2a/v1/relay/down?hub_id=<id>
        and POST /g2a/v1/relay/up?hub_id=<id>;
      - Any standard G2A Computation Core connects transparently to
        http(s)://<relay>/hubs/<id>/g2a/v1/core and /hubs/<id>/g2a/v1/core/events!
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, relay_token: str = "") -> None:
        self.host = host
        self.port = port
        self.relay_token = relay_token
        self._lock = threading.Lock()
        self._hub_downlinks: Dict[str, "queue.Queue[Optional[Dict[str, Any]]]"] = {}
        self._pending_replies: Dict[str, tuple[threading.Event, Dict[str, Any]]] = {}
        self._sse_subscribers: Dict[str, set["queue.Queue[Optional[Dict[str, Any]]]"]] = {}
        self._httpd: Optional[ThreadingHTTPServer] = None

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

            def _check_relay_token(self) -> bool:
                if not relay_ref.relay_token:
                    return True
                return self.headers.get("Authorization", "") == f"Bearer {relay_ref.relay_token}"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)

                # 1. Home Hub Outbound Downlink SSE (/g2a/v1/relay/down?hub_id=...)
                if parsed.path == "/g2a/v1/relay/down":
                    if not self._check_relay_token():
                        self._send_json(401, {"error": "unauthorized_relay_token"})
                        return
                    hub_id = (qs.get("hub_id") or [""])[0]
                    if not hub_id:
                        self._send_json(400, {"error": "missing_hub_id"})
                        return

                    q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
                    with relay_ref._lock:
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
                            item = q.get()
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
                    session_id = (qs.get("session_id") or [""])[0]
                    if not session_id:
                        self._send_json(400, {"error": "missing_session_id"})
                        return
                    ev_q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
                    with relay_ref._lock:
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
                            ev = ev_q.get()
                            if ev is None:
                                break
                            raw = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                            ev_name = str(ev.get("event", "message")).encode("utf-8")
                            self.wfile.write(b"event: " + ev_name + b"\ndata: " + raw + b"\n\n")
                            self.wfile.flush()
                    except Exception:
                        pass
                    finally:
                        with relay_ref._lock:
                            relay_ref._sse_subscribers.get(session_id, set()).discard(ev_q)
                    return

                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)

                # 1. Home Hub Outbound Uplink (/g2a/v1/relay/up?hub_id=...)
                if parsed.path == "/g2a/v1/relay/up":
                    if not self._check_relay_token():
                        self._send_json(401, {"error": "unauthorized_relay_token"})
                        return
                    up = json.loads(raw_body.decode("utf-8"))
                    kind = up.get("kind")
                    if kind == "reply":
                        t_id = str(up.get("tunnel_req_id", ""))
                        with relay_ref._lock:
                            entry = relay_ref._pending_replies.get(t_id)
                        if entry is not None:
                            ev_obj, holder = entry
                            holder["status"] = int(up.get("http_status", 200))
                            holder["reply"] = up.get("reply")
                            ev_obj.set()
                    elif kind == "event":
                        ev_env = up.get("event")
                        if isinstance(ev_env, dict):
                            sid = str(ev_env.get("session_id", ""))
                            with relay_ref._lock:
                                subs = list(relay_ref._sse_subscribers.get(sid, set()))
                            for sub_q in subs:
                                sub_q.put(ev_env)
                    self._send_json(200, {"ok": True})
                    return

                # 2. Computation Core Request (/hubs/<hub_id>/g2a/v1/core)
                if parsed.path.startswith("/hubs/") and parsed.path.endswith("/g2a/v1/core"):
                    parts = parsed.path.strip("/").split("/")
                    hub_id = parts[1]
                    with relay_ref._lock:
                        down_q = relay_ref._hub_downlinks.get(hub_id)
                    if down_q is None:
                        self._send_json(503, {
                            "g2a_version": "0.0.1", "request_id": "", "timestamp_ms": 0,
                            "acceptance": "not_accepted", "result": None,
                            "error": {"code": "unavailable", "http_status": 503, "message": "Target Hub tunnel offline"}
                        })
                        return

                    auth_hdr = self.headers.get("Authorization", "")
                    core_bearer = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
                    t_id = f"treq-{uuid.uuid4().hex[:12]}"
                    done_ev = threading.Event()
                    holder: Dict[str, Any] = {}
                    with relay_ref._lock:
                        relay_ref._pending_replies[t_id] = (done_ev, holder)
                    try:
                        down_q.put({
                            "tunnel_req_id": t_id,
                            "core_bearer": core_bearer,
                            "body": json.loads(raw_body.decode("utf-8")),
                        })
                        if not done_ev.wait(timeout=8.0):
                            self._send_json(504, {
                                "g2a_version": "0.0.1", "request_id": "", "timestamp_ms": 0,
                                "acceptance": "unknown", "result": None,
                                "error": {"code": "outcome_unknown", "http_status": 504, "message": "Hub tunnel reply timeout"}
                            })
                            return
                        self._send_json(holder["status"], holder["reply"])
                    finally:
                        with relay_ref._lock:
                            relay_ref._pending_replies.pop(t_id, None)
                    return

                self._send_json(404, {"error": "not_found"})

            def _send_json(self, status: int, obj: Any) -> None:
                out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Connection", "close")
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
