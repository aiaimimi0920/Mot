from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Set

from .schemas import G2A_WIRE_VERSION, VALID_ERROR_CODES


def _raw_post(url: str, raw_bytes: bytes, token: str = "core-conf") -> tuple[int, Dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=raw_bytes, method="POST", headers=headers)
    try:
        with opener.open(req, timeout=4.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as herr:
        body = herr.read().decode("utf-8")
        return herr.code, json.loads(body) if body else {}


def verify_all_14_error_codes_live(core_base_url: str, origin_base_url: str) -> Dict[str, int]:
    """
    Exercises and verifies all 14 normative G2A V0.0.1 error codes from Section 6.3
    against a live Intelligent Hub instance.
    """
    core_ep = f"{core_base_url.rstrip('/')}/g2a/v1/core"
    origin_ep = f"{origin_base_url.rstrip('/')}/g2a/v1/origin"
    observed: Dict[str, int] = {}

    def _record(status: int, env: Dict[str, Any]) -> str:
        code = env["error"]["code"]
        observed[code] = status
        return code

    # 1. invalid_request (400): duplicate JSON key rejected by strict RFC 8259 parser
    st, env = _raw_post(core_ep, b'{"g2a_version":"0.0.1","request_id":"r1","request_id":"r2","operation":"session.authorize","payload":{}}')
    assert _record(st, env) == "invalid_request" and st == 400

    # 2. unauthenticated (401): missing Bearer token on Core Interface
    st, env = _raw_post(core_ep, b'{"g2a_version":"0.0.1","request_id":"r2","operation":"session.authorize","payload":{"slot_id":"s"}}', token="")
    assert _record(st, env) == "unauthenticated" and st == 401

    # 3. unsupported_version (400): wrong wire version
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": "9.9.9", "request_id": "r3", "timestamp_ms": 1,
        "operation": "session.authorize", "payload": {"slot_id": "s"}
    }).encode("utf-8"))
    assert _record(st, env) == "unsupported_version" and st == 400

    # 4. unsupported_capability (400): unknown required extension
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r4", "timestamp_ms": 1,
        "operation": "session.authorize", "payload": {"slot_id": "s"},
        "extensions": {"com.unknown.required_feature.v9": {"required": True}}
    }).encode("utf-8"))
    assert _record(st, env) == "unsupported_capability" and st == 400

    # 5. not_found (404): authorizing against unknown slot_id
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r5", "timestamp_ms": 1,
        "operation": "session.authorize", "payload": {"slot_id": "nonexistent-slot"}
    }).encode("utf-8"))
    assert _record(st, env) == "not_found" and st == 404

    # 6. invalid_arguments (400): mismatched capability_table_digest on Origin Interface
    st, env = _raw_post(origin_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r6", "timestamp_ms": 1,
        "operation": "origin.capabilities",
        "payload": {
            "slot_id": "slot-conf", "role_kind": "companion", "billing_mode": "player_metered",
            "capability_table_digest": "sha256:0000", "capabilities": []
        }
    }).encode("utf-8"), token="")
    assert _record(st, env) == "invalid_arguments" and st == 400

    # Register valid slot-conf (using disconnected pull stream initially so we can also test unavailable/outcome_unknown!)
    from .jcs import jcs_sha256

    caps = [
        {
            "method_id": "#01", "name": "act_allow", "description": "Allow verb",
            "permitted_actors": ["companion", "player"],
            "params_schema": {"type": "object", "required": ["x"]},
            "sustained": False, "risk_flags": [], "default_policy": "allow",
        },
        {
            "method_id": "#02", "name": "act_prohibit", "description": "Prohibit verb",
            "permitted_actors": ["companion"],
            "params_schema": {"type": "object"},
            "sustained": False, "risk_flags": [], "default_policy": "prohibit",
        },
    ]
    cap_digest = jcs_sha256("g2a.cap.v1", caps)
    _raw_post(origin_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r7", "timestamp_ms": 1,
        "operation": "origin.capabilities",
        "payload": {
            "slot_id": "slot-conf", "role_kind": "companion", "billing_mode": "player_metered",
            "origin_execute_url": "pull://origin.execute",
            "capability_table_digest": cap_digest, "capabilities": caps,
        }
    }).encode("utf-8"), token="")

    # 7. unavailable (503): context/view not initialized yet OR chat.send when outbound stream is disconnected
    _, auth_env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r8", "timestamp_ms": 1,
        "operation": "session.authorize",
        "payload": {"slot_id": "slot-conf", "requested_scope": {"allow_chat": True}},
    }).encode("utf-8"))
    sid = auth_env["result"]["session_id"]

    # Sync full view at rev=10
    _raw_post(origin_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r9", "timestamp_ms": 1,
        "operation": "origin.sync",
        "payload": {
            "slot_id": "slot-conf", "epoch": 1, "rev": 10, "mode": "full", "significant_event": True,
            "state_brief": {},
            "upserted_methods": [
                {"method_id": "#01", "actor": "companion", "readiness": "ready", "targets": []},
                {"method_id": "#01", "actor": "player", "readiness": "ready", "targets": []},
                {"method_id": "#02", "actor": "companion", "readiness": "ready", "targets": []},
            ],
            "removed_methods": [], "settled_executions": [],
        }
    }).encode("utf-8"), token="")

    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r9b", "session_id": sid, "timestamp_ms": 1,
        "operation": "chat.send",
        "payload": {"message_id": "m1", "audience": "player_only", "text": "hi", "provenance": "ai_companion"}
    }).encode("utf-8"))
    assert _record(st, env) == "unavailable" and st == 503

    # 8. stale_context (409): based_on_rev older than last_significant_rev (5 < 10)
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r10", "session_id": sid, "timestamp_ms": 1,
        "operation": "queue.submit",
        "payload": {
            "queue_id": "Q-stale", "epoch": 1, "based_on_rev": 5, "trigger": "player_command",
            "supersedes": False, "intent_summary": "stale",
            "steps": [{"step_index": 0, "method_id": "#01", "actor": "companion", "args": {"x": 1}}],
        }
    }).encode("utf-8"))
    assert _record(st, env) == "stale_context" and st == 409

    # 9. permission_denied (403): calling prohibited method #02
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r11", "session_id": sid, "timestamp_ms": 1,
        "operation": "queue.submit",
        "payload": {
            "queue_id": "Q-proh", "epoch": 1, "based_on_rev": 10, "trigger": "player_command",
            "supersedes": False, "intent_summary": "proh",
            "steps": [{"step_index": 0, "method_id": "#02", "actor": "companion", "args": {}}],
        }
    }).encode("utf-8"))
    assert _record(st, env) == "permission_denied" and st == 403

    # 10. confirmation_required (409): proactive + actor=="player"
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r12", "session_id": sid, "timestamp_ms": 1,
        "operation": "queue.submit",
        "payload": {
            "queue_id": "Q-conf-hold", "epoch": 1, "based_on_rev": 10, "trigger": "proactive",
            "supersedes": False, "intent_summary": "hold",
            "steps": [{"step_index": 0, "method_id": "#01", "actor": "player", "args": {"x": 1}}],
        }
    }).encode("utf-8"))
    assert _record(st, env) == "confirmation_required" and st == 409

    # 11. conflict (409): reusing Q-conf-hold with altered arguments
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r13", "session_id": sid, "timestamp_ms": 1,
        "operation": "queue.submit",
        "payload": {
            "queue_id": "Q-conf-hold", "epoch": 1, "based_on_rev": 10, "trigger": "proactive",
            "supersedes": False, "intent_summary": "hold",
            "steps": [{"step_index": 0, "method_id": "#01", "actor": "player", "args": {"x": 999}}],
        }
    }).encode("utf-8"))
    assert _record(st, env) == "conflict" and st == 409

    # 12. resource_limit (429): rapid double context.pull within adaptive idle backoff window
    _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r14a", "session_id": sid, "timestamp_ms": 1,
        "operation": "context.pull", "payload": {}
    }).encode("utf-8"))
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r14b", "session_id": sid, "timestamp_ms": 1,
        "operation": "context.pull", "payload": {}
    }).encode("utf-8"))
    assert _record(st, env) == "resource_limit" and st == 429

    # 13. outcome_unknown (504): Origin HTTP endpoint accepts TCP/HTTP and stalls past Hub client timeout or returns 504ReplyEnvelope!
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class UnknownOriginHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(n)
            rep = json.dumps({
                "g2a_version": G2A_WIRE_VERSION, "request_id": "x", "timestamp_ms": 1,
                "acceptance": "unknown", "result": None,
                "error": {"code": "outcome_unknown", "http_status": 504, "message": "Upstream gateway timeout"}
            }).encode("utf-8")
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(rep)))
            self.end_headers()
            self.wfile.write(rep)

    u_srv = ThreadingHTTPServer(("127.0.0.1", 0), UnknownOriginHandler)
    u_port = u_srv.server_address[1]
    threading.Thread(target=u_srv.serve_forever, daemon=True).start()
    try:
        _raw_post(origin_ep, json.dumps({
            "g2a_version": G2A_WIRE_VERSION, "request_id": "r15", "timestamp_ms": 1,
            "operation": "origin.capabilities",
            "payload": {
                "slot_id": "slot-conf", "role_kind": "companion", "billing_mode": "player_metered",
                "origin_execute_url": f"http://127.0.0.1:{u_port}/exec",
                "capability_table_digest": cap_digest, "capabilities": caps,
            }
        }).encode("utf-8"), token="")
        st, env = _raw_post(core_ep, json.dumps({
            "g2a_version": G2A_WIRE_VERSION, "request_id": "r16", "session_id": sid, "timestamp_ms": 1,
            "operation": "chat.send",
            "payload": {"message_id": "m-504", "audience": "player_only", "text": "test", "provenance": "ai_companion"}
        }).encode("utf-8"))
        assert _record(st, env) == "outcome_unknown" and st == 504
    finally:
        u_srv.shutdown()
        u_srv.server_close()

    # 14. revoked (403): close session -> heartbeat returns revoked!
    _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r17", "session_id": sid, "timestamp_ms": 1,
        "operation": "session.close", "payload": {}
    }).encode("utf-8"))
    st, env = _raw_post(core_ep, json.dumps({
        "g2a_version": G2A_WIRE_VERSION, "request_id": "r18", "session_id": sid, "timestamp_ms": 1,
        "operation": "session.heartbeat", "payload": {}
    }).encode("utf-8"))
    assert _record(st, env) == "revoked" and st == 403

    missing: Set[str] = set(VALID_ERROR_CODES) - set(observed.keys())
    if missing:
        raise AssertionError(f"Missing conformance error codes: {missing}")
    return observed
