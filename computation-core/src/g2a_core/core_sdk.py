from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Protocol

from .jcs import jcs_sha256, parse_strict_json
from .memory import InMemoryCompanionStore, MemoryRecord, SpoilerGuard
from .schemas import (
    G2A_WIRE_VERSION,
    CapabilityEntry,
    FrozenScope,
    G2AError,
    MethodCallQueue,
    PlanDecision,
    ProjectedContext,
    QueueReceipt,
)
from .ssrf import validate_outbound_url_against_ssrf


def generate_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def default_hub_runtime_file() -> str:
    override = os.environ.get("G2A_HUB_RUNTIME_FILE")
    if override:
        return override
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return os.path.join(local_app, "G2A", "hub.runtime.json")
    return os.path.join(os.path.expanduser("~"), ".g2a", "hub.runtime.json")


def discover_local_hub_manifest(runtime_file: Optional[str] = None, verify_health: bool = True) -> Dict[str, Any]:
    path = runtime_file or default_hub_runtime_file()
    if not os.path.exists(path):
        raise G2AError("unavailable", 503, f"Local Intelligent Hub runtime manifest not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        info = json.load(f)
    core_url = str(info["core_url"]).rstrip("/")
    if verify_health:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{core_url}/g2a/v1/health", timeout=2.0) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            if health.get("status") != "ok":
                raise G2AError("unavailable", 503, "Local Intelligent Hub Core health check failed")
    return info


class CognitivePlannerPort(Protocol):
    def plan(
        self,
        capabilities: List[CapabilityEntry],
        context: ProjectedContext,
        recalled_memories: List[MemoryRecord],
    ) -> PlanDecision:
        ...


class CoreSDK:
    """Tier 3 Computation Core SDK implementing Chapter 4 of G2A_PSEUDOCODE_API (V0.0.1)."""

    def __init__(
        self,
        planner: CognitivePlannerPort,
        memory_store: Optional[InMemoryCompanionStore] = None,
        allow_loopback: bool = False,
        request_timeout_sec: float = 8.0,
        heartbeat_interval_sec: float = 5.0,
        stream_mode: str = "sse",  # "sse" | "websocket"
        ssl_context: Optional[ssl.SSLContext] = None,
        custom_post_fn: Optional[Callable[[str, str, Dict[str, Any], str, Optional[str]], Dict[str, Any]]] = None,
    ) -> None:
        self.planner = planner
        self.memory_store = memory_store or InMemoryCompanionStore()
        self.allow_loopback = allow_loopback
        self.request_timeout_sec = request_timeout_sec
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.stream_mode = stream_mode
        self.ssl_context = ssl_context
        self._custom_post_fn = custom_post_fn

        self.capability_cache: Dict[str, List[CapabilityEntry]] = {}
        self.slot_digest_index: Dict[str, str] = {}
        self.pending_confirm_queues: Dict[str, MethodCallQueue] = {}
        self.completed_receipts: List[QueueReceipt] = []
        self.seen_event_ids: set[str] = set()
        self.refusal_cooldowns: Dict[str, int] = {}

        self.hub_endpoint: str = ""
        self.auth_token: str = ""
        self.session_id: Optional[str] = None
        self.scope: Optional[FrozenScope] = None
        self.negotiated_extensions: List[str] = []
        self.actions_paused: bool = False
        self.closed: bool = False
        self.latest_context: Optional[ProjectedContext] = None

        self._stop_event = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._sse_thread: Optional[threading.Thread] = None
        self.on_receipt_callback: Optional[Callable[[QueueReceipt], None]] = None

    def connect(
        self,
        hub_endpoint: str,
        auth_token: str,
        slot_id: str,
        requested_scope: Optional[Dict[str, Any]] = None,
        consumption_cap: Optional[int] = None,
        extensions: Optional[Dict[str, Any]] = None,
        player_id: Optional[str] = None,
        faction_id: Optional[str] = None,
        start_background_loops: bool = True,
    ) -> FrozenScope:
        validate_outbound_url_against_ssrf(hub_endpoint, allow_loopback=self.allow_loopback)
        self.hub_endpoint = hub_endpoint.rstrip("/")
        self.auth_token = auth_token
        self.closed = False
        self._stop_event.clear()

        cached_digest = self.slot_digest_index.get(slot_id)
        auth_payload: Dict[str, Any] = {
            "slot_id": slot_id,
            "requested_scope": requested_scope or {"allow_chat": True},
        }
        if player_id:
            auth_payload["player_id"] = player_id
        if faction_id:
            auth_payload["faction_id"] = faction_id
        if cached_digest:
            auth_payload["cached_capability_digest"] = cached_digest
        if consumption_cap is not None:
            auth_payload["consumption_cap"] = consumption_cap

        reply = self._post_envelope(
            "session.authorize",
            auth_payload,
            session_id=None,
            extensions=extensions,
        )
        cap_digest = reply["capability_table_digest"]
        raw_table = reply.get("capability_table")
        if raw_table is not None:
            caps = [CapabilityEntry.from_dict(item) for item in raw_table]
            caps_sorted = sorted(caps, key=lambda c: c.method_id)
            verified = jcs_sha256("g2a.cap.v1", [c.to_dict() for c in caps_sorted])
            if verified != cap_digest:
                raise G2AError("invalid_arguments", 400, "Hub returned mismatched capability_table_digest")
            self.capability_cache[cap_digest] = caps_sorted

        self.slot_digest_index[slot_id] = cap_digest
        self.session_id = reply["session_id"]
        self.scope = FrozenScope.from_dict(reply["frozen_scope"])
        self.negotiated_extensions = list(reply.get("negotiated_extensions") or [])

        if start_background_loops:
            self._start_heartbeat_loop_async()
            if self.stream_mode == "websocket":
                self._start_websocket_stream_async()
            else:
                self._start_event_stream_async()
        return self.scope

    def connect_local_hub(
        self,
        auth_token: str,
        slot_id: str,
        runtime_file: Optional[str] = None,
        **kwargs: Any,
    ) -> FrozenScope:
        self.allow_loopback = True
        manifest = discover_local_hub_manifest(runtime_file=runtime_file)
        return self.connect(str(manifest["core_url"]), auth_token, slot_id, **kwargs)

    def shutdown_session_cleanly(self, notify_hub: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self._stop_event.set()
        if notify_hub and self.session_id:
            try:
                self._post_envelope("session.close", {}, session_id=self.session_id)
            except Exception:
                pass

    # --- Reconnect & Resume State Machine (§4.2) ---

    def on_stream_reconnected(self) -> None:
        if self.closed or not self.session_id:
            return
        try:
            res = self._post_envelope("session.resume", {}, session_id=self.session_id)
            self.actions_paused = bool(res.get("actions_paused", False))
            if isinstance(res.get("latest_context"), dict):
                self.latest_context = ProjectedContext.from_dict(res["latest_context"])
        except G2AError as err:
            if err.code in ("revoked", "permission_denied"):
                self.shutdown_session_cleanly(notify_hub=False)
            else:
                time.sleep(0.5)

    def on_event_session_resumed(self, fresh_ctx: ProjectedContext) -> None:
        self.actions_paused = False
        self.on_context_received(fresh_ctx)

    # --- Context Consumption & Queue Submission Recovery (§4.3) ---

    def pull_context(self) -> ProjectedContext:
        if not self.session_id or self.closed:
            raise G2AError("revoked", 403, "Session is closed")
        res = self._post_envelope("context.pull", {}, session_id=self.session_id)
        ctx = ProjectedContext.from_dict(res)
        self.latest_context = ctx
        return ctx

    def on_context_received(self, ctx: ProjectedContext, _replan_depth: int = 0) -> Optional[QueueReceipt]:
        if self.closed or self.actions_paused or not self.session_id or not self.scope:
            return None
        self.latest_context = ctx
        caps = self.capability_cache.get(ctx.capability_table_digest, [])
        allowed_tags = set(ctx.allowed_timeline_tags) if ctx.allowed_timeline_tags is not None else None
        memories = self.memory_store.recall_safe(
            self.scope.player_id,
            SpoilerGuard(
                current_epoch=ctx.epoch,
                game_origin_id=self.scope.game_instance_id,
                allowed_timeline_tags=allowed_tags,
            ),
        )
        decision = self.planner.plan(caps, ctx, memories)

        if decision.reply_text and self.scope.allow_chat:
            self._post_envelope(
                "chat.send",
                {
                    "message_id": generate_id("msg"),
                    "audience": decision.audience,
                    "text": decision.reply_text,
                    "provenance": "ai_companion",
                },
                session_id=self.session_id,
            )

        if decision.planned_steps:
            is_cmd = ctx.player_command is not None
            queue = MethodCallQueue(
                queue_id=generate_id("Q"),
                epoch=ctx.epoch,
                based_on_rev=ctx.rev,
                trigger="player_command" if is_cmd else "proactive",
                supersedes=is_cmd,
                intent_summary=decision.intent_summary or "Execute planned companion queue",
                steps=decision.planned_steps,
            )
            return self.submit_queue_with_standard_recovery(queue, _replan_depth=_replan_depth)
        return None

    def submit_queue_with_standard_recovery(
        self,
        queue: MethodCallQueue,
        _replan_depth: int = 0,
    ) -> Optional[QueueReceipt]:
        if self.closed or not self.session_id:
            return None
        try:
            res = self._post_envelope("queue.submit", queue.to_dict(), session_id=self.session_id)
            receipt = QueueReceipt.from_dict(res)
            self._record_receipt(receipt)
            return receipt
        except G2AError as err:
            if err.code == "confirmation_required":
                # Preserve original queue_id and steps intact while awaiting player decision!
                self.pending_confirm_queues[queue.queue_id] = queue
                return None
            if err.code == "stale_context":
                if err.fresh_context is not None and _replan_depth < 2:
                    return self.on_context_received(err.fresh_context, _replan_depth=_replan_depth + 1)
                return None
            if err.code == "outcome_unknown":
                # 504 timeout rule: NEVER blindly resend queue.submit or mint a new queue_id; reconcile via queue.query!
                return self.reconcile_unknown_queue(queue.queue_id)
            if err.code == "resource_limit":
                wait_sec = min((err.retry_after_ms or 500) / 1000.0, 2.0)
                time.sleep(max(wait_sec, 0.01))
                return None
            if err.code in ("revoked", "permission_denied") and "offline" in err.message.lower():
                self.shutdown_session_cleanly(notify_hub=False)
            return None

    def on_event_queue_confirmed(self, queue_id: str) -> Optional[QueueReceipt]:
        original = self.pending_confirm_queues.pop(queue_id, None)
        if original is not None:
            return self.submit_queue_with_standard_recovery(original)
        return None

    def on_event_queue_declined(self, queue_id: str, cooldown_ms: int = 60000) -> None:
        declined = self.pending_confirm_queues.pop(queue_id, None)
        if declined is not None:
            fp = jcs_sha256("g2a.fp.v1", [s.to_dict() for s in declined.steps])
            self.refusal_cooldowns[fp] = int(time.time() * 1000) + cooldown_ms

    def reconcile_unknown_queue(self, queue_id: str) -> Optional[QueueReceipt]:
        if not self.session_id or self.closed:
            return None
        for attempt in range(3):
            try:
                res = self._post_envelope("queue.query", {"queue_id": queue_id}, session_id=self.session_id)
                receipt = QueueReceipt.from_dict(res)
                self._record_receipt(receipt)
                return receipt
            except G2AError as err:
                if err.code in ("unavailable", "outcome_unknown") and attempt < 2:
                    time.sleep(0.05 * (2**attempt))
                    continue
                raise
        return None

    def handle_event_envelope(self, env: Dict[str, Any]) -> None:
        ev_id = env.get("event_id")
        if ev_id:
            if ev_id in self.seen_event_ids:
                return
            self.seen_event_ids.add(ev_id)

        ev_name = env.get("event")
        payload = env.get("payload") or {}
        if ev_name == "context.push" and isinstance(payload, dict):
            self.on_context_received(ProjectedContext.from_dict(payload))
        elif ev_name == "queue.confirmed" and isinstance(payload, dict):
            qid = payload.get("queue_id")
            if isinstance(qid, str):
                self.on_event_queue_confirmed(qid)
        elif ev_name == "queue.declined" and isinstance(payload, dict):
            qid = payload.get("queue_id")
            if isinstance(qid, str):
                self.on_event_queue_declined(qid, int(payload.get("cooldown_ms", 60000)))
        elif ev_name == "session.resumed" and isinstance(payload, dict):
            self.on_event_session_resumed(ProjectedContext.from_dict(payload))
        elif ev_name == "session.closed":
            self.shutdown_session_cleanly(notify_hub=False)

    # --- Transport Internals ---

    def _record_receipt(self, receipt: QueueReceipt) -> None:
        self.completed_receipts.append(receipt)
        if receipt.latest_context is not None:
            self.latest_context = receipt.latest_context
        if self.on_receipt_callback:
            self.on_receipt_callback(receipt)

    def _post_envelope(
        self,
        operation: str,
        payload: Dict[str, Any],
        session_id: Optional[str],
        extensions: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self._custom_post_fn is not None:
            return self._custom_post_fn(self.hub_endpoint, operation, payload, self.auth_token, session_id)

        url = f"{self.hub_endpoint}/g2a/v1/core"
        env: Dict[str, Any] = {
            "g2a_version": G2A_WIRE_VERSION,
            "request_id": generate_id("req"),
            "timestamp_ms": int(time.time() * 1000),
            "operation": operation,
            "payload": payload,
            "extensions": extensions or {},
        }
        if session_id is not None:
            env["session_id"] = session_id

        body = json.dumps(env, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
        }
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        handlers: List[Any] = []
        if "127.0.0.1" in url or "localhost" in url:
            handlers.append(urllib.request.ProxyHandler({}))
        if self.ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self.ssl_context))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=self.request_timeout_sec) as resp:
                raw = resp.read()
                parsed = parse_strict_json(raw)
                return self._unwrap_reply(parsed)
        except urllib.error.HTTPError as http_err:
            raw = http_err.read()
            if raw:
                parsed = parse_strict_json(raw)
                return self._unwrap_reply(parsed)
            if http_err.code == 504:
                raise G2AError("outcome_unknown", 504, "Gateway timeout") from http_err
            raise G2AError("unavailable", http_err.code, str(http_err)) from http_err
        except (TimeoutError, socket.timeout) as tex:
            raise G2AError("outcome_unknown", 504, f"Request timed out: {tex}") from tex
        except urllib.error.URLError as uex:
            if isinstance(uex.reason, (TimeoutError, socket.timeout)):
                raise G2AError("outcome_unknown", 504, f"Request timed out: {uex}") from uex
            raise G2AError("unavailable", 503, f"Hub connection failed: {uex}") from uex

    @staticmethod
    def _unwrap_reply(reply_env: Dict[str, Any]) -> Dict[str, Any]:
        acc = reply_env.get("acceptance")
        if acc == "accepted":
            return reply_env.get("result") or {}
        err_dict = reply_env.get("error") or {
            "code": "outcome_unknown" if acc == "unknown" else "invalid_request",
            "http_status": 504 if acc == "unknown" else 400,
            "message": "Request not accepted",
        }
        raise G2AError.from_dict(err_dict)

    def _start_heartbeat_loop_async(self) -> None:
        def _loop() -> None:
            while not self._stop_event.wait(self.heartbeat_interval_sec):
                if self.closed or not self.session_id:
                    return
                try:
                    self._post_envelope("session.heartbeat", {}, session_id=self.session_id)
                except G2AError as err:
                    if err.code in ("revoked", "permission_denied"):
                        self.shutdown_session_cleanly(notify_hub=False)
                        return

        self._hb_thread = threading.Thread(target=_loop, name="g2a-core-heartbeat", daemon=True)
        self._hb_thread.start()

    def _start_event_stream_async(self) -> None:
        def _stream_worker() -> None:
            first_connect = True
            while not self._stop_event.is_set() and not self.closed and self.session_id:
                stream_url = f"{self.hub_endpoint}/g2a/v1/core/events?session_id={self.session_id}"
                req = urllib.request.Request(
                    stream_url,
                    method="GET",
                    headers={
                        "Accept": "text/event-stream",
                        "Authorization": f"Bearer {self.auth_token}",
                    },
                )
                try:
                    if not first_connect:
                        self.on_stream_reconnected()
                        if self.closed:
                            return
                    first_connect = False
                    handlers: List[Any] = []
                    if "127.0.0.1" in stream_url or "localhost" in stream_url:
                        handlers.append(urllib.request.ProxyHandler({}))
                    if self.ssl_context is not None:
                        handlers.append(urllib.request.HTTPSHandler(context=self.ssl_context))
                    opener = urllib.request.build_opener(*handlers)
                    with opener.open(req, timeout=60.0) as resp:
                        data_lines: List[str] = []
                        while not self._stop_event.is_set() and not self.closed:
                            raw_line = resp.readline()
                            if not raw_line:
                                break
                            line = raw_line.decode("utf-8").rstrip("\r\n")
                            if line == "":
                                if data_lines:
                                    joined = "\n".join(data_lines)
                                    data_lines.clear()
                                    env = parse_strict_json(joined)
                                    self.handle_event_envelope(env)
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].lstrip(" "))
                except urllib.error.HTTPError as herr:
                    if herr.code in (401, 403):
                        self.shutdown_session_cleanly(notify_hub=False)
                        return
                    time.sleep(0.2)
                except Exception:
                    if self._stop_event.is_set() or self.closed:
                        return
                    time.sleep(0.2)

        self._sse_thread = threading.Thread(target=_stream_worker, name="g2a-core-sse", daemon=True)
        self._sse_thread.start()

    def _start_websocket_stream_async(self) -> None:
        """RFC 6455 WebSocket push stream consumer (Section 6.2 alternative to SSE, supporting TLS 1.3 wss://)."""

        def _recv_exact(sock: socket.socket, n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise EOFError("WebSocket closed")
                buf.extend(chunk)
            return bytes(buf)

        def _send_masked_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            hdr = bytearray([0x80 | (opcode & 0x0F)])
            ln = len(payload)
            if ln < 126:
                hdr.append(0x80 | ln)
            elif ln <= 65535:
                hdr.append(0x80 | 126)
                hdr.extend(struct.pack("!H", ln))
            else:
                hdr.append(0x80 | 127)
                hdr.extend(struct.pack("!Q", ln))
            sock.sendall(bytes(hdr) + mask + masked)

        def _ws_worker() -> None:
            first_connect = True
            while not self._stop_event.is_set() and not self.closed and self.session_id:
                parsed = urllib.parse.urlparse(self.hub_endpoint)
                is_tls = parsed.scheme in ("https", "wss")
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port or (443 if is_tls else 80)
                base_path = parsed.path.rstrip("/")
                ws_path = f"{base_path}/g2a/v1/core/ws?session_id={urllib.parse.quote(self.session_id)}"

                raw_sock: Optional[socket.socket] = None
                try:
                    if not first_connect:
                        self.on_stream_reconnected()
                        if self.closed:
                            return
                    first_connect = False

                    raw_sock = socket.create_connection((host, port), timeout=10.0)
                    if is_tls:
                        ctx = self.ssl_context or ssl.create_default_context()
                        raw_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
                    raw_sock.settimeout(60.0)

                    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
                    handshake = (
                        f"GET {ws_path} HTTP/1.1\r\n"
                        f"Host: {host}:{port}\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {ws_key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n"
                        f"Authorization: Bearer {self.auth_token}\r\n\r\n"
                    )
                    raw_sock.sendall(handshake.encode("ascii"))

                    resp_head = b""
                    while b"\r\n\r\n" not in resp_head:
                        chunk = raw_sock.recv(1)
                        if not chunk:
                            raise EOFError("Handshake closed")
                        resp_head += chunk
                    status_line = resp_head.split(b"\r\n", 1)[0].decode("latin1")
                    if " 101 " not in status_line:
                        if " 401 " in status_line or " 403 " in status_line:
                            self.shutdown_session_cleanly(notify_hub=False)
                            return
                        raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")

                    while not self._stop_event.is_set() and not self.closed:
                        b0, b1 = _recv_exact(raw_sock, 2)
                        opcode = b0 & 0x0F
                        masked = (b1 & 0x80) != 0
                        length = b1 & 0x7F
                        if length == 126:
                            length = struct.unpack("!H", _recv_exact(raw_sock, 2))[0]
                        elif length == 127:
                            length = struct.unpack("!Q", _recv_exact(raw_sock, 8))[0]
                        mask_key = _recv_exact(raw_sock, 4) if masked else b""
                        data = _recv_exact(raw_sock, length) if length > 0 else b""
                        if masked:
                            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
                        if opcode == 0x8:
                            _send_masked_frame(raw_sock, 0x8)
                            break
                        if opcode == 0x9:
                            _send_masked_frame(raw_sock, 0xA, data)
                        elif opcode == 0x1:
                            env = parse_strict_json(data)
                            self.handle_event_envelope(env)
                except Exception:
                    if self._stop_event.is_set() or self.closed:
                        return
                    time.sleep(0.2)
                finally:
                    if raw_sock is not None:
                        try:
                            raw_sock.close()
                        except Exception:
                            pass

        self._sse_thread = threading.Thread(target=_ws_worker, name="g2a-core-ws", daemon=True)
        self._sse_thread.start()
