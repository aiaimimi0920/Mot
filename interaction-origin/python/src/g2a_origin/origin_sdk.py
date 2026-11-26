from __future__ import annotations

import hmac
import http.client
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union

from .jcs import StrictJSONError, jcs_sha256, parse_strict_json

G2A_WIRE_VERSION = "0.0.1"
MAX_CANDIDATE_TARGETS_PER_METHOD = 24
MAX_EXECUTION_LEDGER_ENTRIES = 512


def _gen_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class G2AOriginError(Exception):
    def __init__(self, code: str, http_status: int, message: str) -> None:
        super().__init__(f"{code} ({http_status}): {message}")
        self.code = code
        self.http_status = http_status
        self.message = message


@dataclass
class CapabilityEntry:
    method_id: str
    name: str
    description: str
    permitted_actors: List[str]
    params_schema: Dict[str, Any]
    sustained: bool = False
    risk_flags: List[str] = field(default_factory=list)
    default_policy: str = "allow"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method_id": self.method_id,
            "name": self.name,
            "description": self.description,
            "permitted_actors": list(self.permitted_actors),
            "params_schema": dict(self.params_schema),
            "sustained": self.sustained,
            "risk_flags": list(self.risk_flags),
            "default_policy": self.default_policy,
        }


@dataclass
class TargetCandidate:
    target_id: str
    label: str
    readiness: str  # "ready" | "staged"
    prerequisite_hint: Optional[str] = None
    actor_anchor: Optional[str] = None
    player_anchor: Optional[str] = None
    alert_tag: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "target_id": self.target_id,
            "label": self.label,
            "readiness": self.readiness,
        }
        if self.prerequisite_hint is not None:
            d["prerequisite_hint"] = self.prerequisite_hint
        if self.actor_anchor is not None:
            d["actor_anchor"] = self.actor_anchor
        if self.player_anchor is not None:
            d["player_anchor"] = self.player_anchor
        if self.alert_tag is not None:
            d["alert_tag"] = self.alert_tag
        return d


@dataclass
class ActiveMethodGrant:
    method_id: str
    actor: str
    readiness: str
    targets: List[TargetCandidate] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method_id": self.method_id,
            "actor": self.actor,
            "readiness": self.readiness,
            "targets": [t.to_dict() for t in self.targets],
        }


@dataclass
class StepExecutionToken:
    execution_id: str


@dataclass
class ImmediateSucceeded:
    effect_summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ImmediateFailed:
    reason: str


@dataclass
class StartedExecuting:
    cancel_fn: Callable[[], None]


EffectInvocationResult = Union[ImmediateSucceeded, ImmediateFailed, StartedExecuting]


@dataclass
class OriginExecuteRequest:
    slot_id: str
    epoch: int
    kind: str  # "invoke" | "cancel" | "deliver_chat"
    execution_id: str
    method_id: Optional[str] = None
    actor: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OriginExecuteRequest":
        return cls(
            slot_id=d["slot_id"],
            epoch=int(d["epoch"]),
            kind=d["kind"],
            execution_id=d["execution_id"],
            method_id=d.get("method_id"),
            actor=d.get("actor"),
            args=dict(d.get("args") or {}),
        )


@dataclass
class OriginExecuteReply:
    execution_id: str
    state: str  # "executing" | "succeeded" | "failed" | "cancelled"
    effect_summary: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "execution_id": self.execution_id,
            "state": self.state,
        }
        if self.effect_summary is not None:
            out["effect_summary"] = self.effect_summary
        if self.failure_reason is not None:
            out["failure_reason"] = self.failure_reason
        return out


# --- Engine & Host Adapter Ports (§2.1) ---


class EngineDispatcherPort(Protocol):
    def run_on_game_thread(self, task: Callable[[], Any]) -> Any:
        ...


class LockingGameThreadDispatcher:
    """Thread-safe default dispatcher serializing game-state reads and domain-effect mutations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def run_on_game_thread(self, task: Callable[[], Any]) -> Any:
        with self._lock:
            return task()


@dataclass
class Pose3D:
    x: float
    y: float
    z: float
    yaw_deg: float = 0.0


@dataclass
class PlayerFocusInfo:
    pose: Pose3D
    pointed_target_id: Optional[str] = None  # VR controller ray or mouse hover target


@dataclass
class InteractableRef:
    id: str
    display_name: str
    pose: Pose3D
    interact_range: float = 2.0
    hidden_secret_unrevealed: bool = False
    alert_tag: Optional[str] = None


class SpatialAdapterPort(Protocol):
    def get_actor_pose(self, actor: str) -> Optional[Pose3D]:
        ...

    def get_player_focus(self) -> PlayerFocusInfo:
        ...

    def query_nearby_interactables(self, origin: Pose3D, perception_radius: float) -> List[InteractableRef]:
        ...


class HubTransportClientPort(Protocol):
    def post_to_hub(self, operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...


def default_hub_runtime_file() -> str:
    override = os.environ.get("G2A_HUB_RUNTIME_FILE")
    if override:
        return override
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return os.path.join(local_app, "G2A", "hub.runtime.json")
    return os.path.join(os.path.expanduser("~"), ".g2a", "hub.runtime.json")


def discover_local_hub_origin_url(runtime_file: Optional[str] = None, verify_health: bool = True) -> str:
    path = runtime_file or default_hub_runtime_file()
    if not os.path.exists(path):
        raise G2AOriginError("unavailable", 503, f"Local Intelligent Hub runtime manifest not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        info = json.load(f)
    origin_url = str(info["origin_url"]).rstrip("/")
    if verify_health:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{origin_url}/g2a/v1/health", timeout=2.0) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            if health.get("status") != "ok":
                raise G2AOriginError("unavailable", 503, "Local Intelligent Hub health check failed")
    return origin_url


class LoopbackHTTPTransportClient:
    """Persistent HTTP/1.1 Keep-Alive transport client avoiding ephemeral TCP TIME_WAIT churn at 60Hz."""

    def __init__(self, hub_origin_url: str, shared_token: str = "", timeout_sec: float = 5.0) -> None:
        self.hub_origin_url = hub_origin_url.rstrip("/")
        self.shared_token = shared_token
        self.timeout_sec = timeout_sec
        parsed = urllib.parse.urlparse(self.hub_origin_url)
        self._is_https = parsed.scheme == "https"
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or (443 if self._is_https else 80)
        self._path = (parsed.path.rstrip("/") or "") + "/g2a/v1/origin"
        self._tls = threading.local()
        self._pool_lock = threading.Lock()
        self._pooled_conns: List[http.client.HTTPConnection] = []

    def _get_conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            if self._is_https:
                conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout_sec)
            else:
                conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout_sec)
            with self._pool_lock:
                self._pooled_conns.append(conn)
            self._tls.conn = conn
        return conn

    def _reset_conn(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            with self._pool_lock:
                if conn in self._pooled_conns:
                    self._pooled_conns.remove(conn)
            self._tls.conn = None

    def close(self) -> None:
        with self._pool_lock:
            conns = list(self._pooled_conns)
            self._pooled_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._tls.conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def post_to_hub(self, operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        env = {
            "g2a_version": G2A_WIRE_VERSION,
            "request_id": _gen_id("orig-req"),
            "timestamp_ms": int(time.time() * 1000),
            "operation": operation,
            "payload": payload,
            "extensions": {},
        }
        body = json.dumps(env, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        if self.shared_token:
            headers["Authorization"] = f"Bearer {self.shared_token}"

        for attempt in range(2):
            conn = self._get_conn()
            try:
                conn.request("POST", self._path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                if raw:
                    parsed = parse_strict_json(raw)
                    if parsed.get("acceptance") == "accepted":
                        return parsed.get("result") or {}
                    err = parsed.get("error") or {}
                    raise G2AOriginError(
                        err.get("code", "invalid_request"),
                        int(err.get("http_status", status)),
                        err.get("message", ""),
                    )
                raise G2AOriginError("unavailable", status, f"HTTP {status}")
            except G2AOriginError:
                raise
            except Exception as ex:
                self._reset_conn()
                if attempt == 0:
                    continue
                raise G2AOriginError("unavailable", 503, f"Hub transport error: {ex}") from ex
        raise G2AOriginError("unavailable", 503, "Unreachable")


# --- Spatial Math & Aggregation Helpers ---


def _distance(a: Pose3D, b: Pose3D) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _compute_player_deictic_label(focus: PlayerFocusInfo, obj: InteractableRef) -> str:
    if focus.pointed_target_id == obj.id:
        return "pointed_ray"
    dx = obj.pose.x - focus.pose.x
    dz = obj.pose.z - focus.pose.z
    yaw_rad = math.radians(focus.pose.yaw_deg)
    cos_y = math.cos(yaw_rad)
    sin_y = math.sin(yaw_rad)
    local_right = dx * cos_y - dz * sin_y
    local_forward = dx * sin_y + dz * cos_y
    dist = round(_distance(focus.pose, obj.pose), 1)
    if abs(local_right) >= abs(local_forward):
        side = "right" if local_right >= 0 else "left"
    else:
        side = "ahead" if local_forward >= 0 else "behind"
    return f"{side} {dist}m"


def _parse_anchor_meters(anchor: Optional[str]) -> float:
    if not anchor:
        return 1e9
    s = anchor.strip()
    if s.endswith("m"):
        token = s[:-1].rsplit(" ", 1)[-1]
        try:
            return float(token)
        except ValueError:
            return 1e9
    return 1e9


def _prioritize_alerts_and_focus(
    targets: List[TargetCandidate],
    limit: int = MAX_CANDIDATE_TARGETS_PER_METHOD,
) -> List[TargetCandidate]:
    def _rank(c: TargetCandidate) -> Tuple[int, int, int, float, str]:
        is_alert = 0 if c.alert_tag else 1
        is_pointed = 0 if (c.player_anchor and "pointed" in c.player_anchor) else 1
        is_ready = 0 if c.readiness == "ready" else 1
        dist_m = _parse_anchor_meters(c.actor_anchor)
        return (is_alert, is_pointed, is_ready, dist_m, c.target_id)

    dedup: Dict[str, TargetCandidate] = {}
    for t in targets:
        if t.target_id not in dedup or t.readiness == "ready":
            dedup[t.target_id] = t
    ordered = sorted(dedup.values(), key=_rank)
    return ordered[:limit]


def merge_method_grant(
    grants: Dict[Tuple[str, str], ActiveMethodGrant],
    incoming: ActiveMethodGrant,
) -> None:
    key = (incoming.method_id, incoming.actor)
    existing = grants.get(key)
    if existing is not None:
        merged_targets = _prioritize_alerts_and_focus(existing.targets + incoming.targets)
        overall = (
            "ready"
            if (
                existing.readiness == "ready"
                or incoming.readiness == "ready"
                or any(t.readiness == "ready" for t in merged_targets)
            )
            else "staged"
        )
        grants[key] = ActiveMethodGrant(incoming.method_id, incoming.actor, overall, merged_targets)
    else:
        grants[key] = ActiveMethodGrant(
            incoming.method_id,
            incoming.actor,
            incoming.readiness,
            _prioritize_alerts_and_focus(incoming.targets),
        )


def _validate_field_schema(name: str, val: Any, spec: Dict[str, Any], depth: int = 0) -> None:
    if depth > 16:
        raise G2AOriginError("invalid_arguments", 400, "Argument nesting depth exceeded")
    ptype = spec.get("type")
    if ptype == "string":
        if not isinstance(val, str):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be string")
        if "minLength" in spec and len(val) < int(spec["minLength"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} shorter than minLength")
        if "maxLength" in spec and len(val) > int(spec["maxLength"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} exceeds maxLength")
        enums = spec.get("enum")
        if isinstance(enums, list) and val not in enums:
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} not in enum")
    elif ptype == "boolean":
        if not isinstance(val, bool):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be boolean")
    elif ptype == "integer":
        if isinstance(val, bool) or not isinstance(val, int):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be integer")
        if "minimum" in spec and val < spec["minimum"]:
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} below minimum")
        if "maximum" in spec and val > spec["maximum"]:
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} exceeds maximum")
        if "exclusiveMinimum" in spec and val <= spec["exclusiveMinimum"]:
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must exceed exclusiveMinimum")
        if "exclusiveMaximum" in spec and val >= spec["exclusiveMaximum"]:
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be below exclusiveMaximum")
    elif ptype == "number":
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be finite number")
        fval = float(val)
        if "minimum" in spec and fval < float(spec["minimum"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} below minimum")
        if "maximum" in spec and fval > float(spec["maximum"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} exceeds maximum")
        if "exclusiveMinimum" in spec and fval <= float(spec["exclusiveMinimum"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must exceed exclusiveMinimum")
        if "exclusiveMaximum" in spec and fval >= float(spec["exclusiveMaximum"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be below exclusiveMaximum")
    elif ptype == "array":
        if not isinstance(val, list):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be array")
        if "minItems" in spec and len(val) < int(spec["minItems"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} has fewer than minItems")
        if "maxItems" in spec and len(val) > int(spec["maxItems"]):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} exceeds maxItems")
        item_spec = spec.get("items")
        if isinstance(item_spec, dict):
            for idx, elem in enumerate(val):
                _validate_field_schema(f"{name}[{idx}]", elem, item_spec, depth + 1)
    elif ptype == "object":
        if not isinstance(val, dict):
            raise G2AOriginError("invalid_arguments", 400, f"Argument {name!r} must be object")
        _validate_args_schema(val, spec, prefix=name, depth=depth + 1)


def _validate_args_schema(
    args: Dict[str, Any],
    schema: Dict[str, Any],
    prefix: str = "",
    depth: int = 0,
) -> None:
    if depth > 16:
        raise G2AOriginError("invalid_arguments", 400, "Schema nesting depth exceeded")
    if not schema:
        return
    for req_key in schema.get("required", []):
        if req_key not in args:
            qual = f"{prefix}.{req_key}" if prefix else str(req_key)
            raise G2AOriginError("invalid_arguments", 400, f"Missing required argument {qual!r}")
    props = schema.get("properties")
    if isinstance(props, dict):
        if schema.get("additionalProperties") is False:
            for k in args:
                if k not in props:
                    qual = f"{prefix}.{k}" if prefix else k
                    raise G2AOriginError("invalid_arguments", 400, f"Unexpected argument {qual!r}")
        for k, v in args.items():
            prop_spec = props.get(k)
            if isinstance(prop_spec, dict):
                qual = f"{prefix}.{k}" if prefix else k
                _validate_field_schema(qual, v, prop_spec, depth + 1)


# --- OriginSDK (§2.2 - §2.4) ---


@dataclass
class _RegisteredCap:
    definition: CapabilityEntry
    effect_handler: Callable[[str, Any, Dict[str, Any], StepExecutionToken], EffectInvocationResult]
    readiness_check: Optional[Callable[[str, Any], Optional[str]]]
    spatial_target_verb: bool
    navigation_verb: bool = False


@dataclass
class _SignalModule:
    snapshot_fn: Callable[[], Dict[str, Any]]
    active_methods_fn: Callable[[], List[ActiveMethodGrant]]
    dirty: bool = True


class OriginSDK:
    def __init__(
        self,
        slot_id: str,
        hub_transport: HubTransportClientPort,
        slot_role_kind: str = "companion",
        billing_mode: str = "player_metered",
        player_id: str = "player-local",
        game_instance_id: str = "game-local",
        room_id: str = "",
        faction_id: str = "",
        engine_dispatcher: Optional[EngineDispatcherPort] = None,
        spatial: Optional[SpatialAdapterPort] = None,
        perception_radius: float = 15.0,
        chat_ui_handler: Optional[Callable[[str, str, bool], None]] = None,
        shared_token: str = "",
    ) -> None:
        self.slot_id = slot_id
        self.slot_role_kind = slot_role_kind
        self.billing_mode = billing_mode
        self.player_id = player_id
        self.game_instance_id = game_instance_id
        self.room_id = room_id
        self.faction_id = faction_id
        self.hub_transport = hub_transport
        self.engine_dispatcher = engine_dispatcher or LockingGameThreadDispatcher()
        self.spatial = spatial
        self.perception_radius = perception_radius
        self.chat_ui_handler = chat_ui_handler or (lambda aud, txt, ai: None)
        self.shared_token = shared_token

        self.capabilities: Dict[str, _RegisteredCap] = {}
        self.actors: Dict[str, Any] = {}
        self.signal_modules: Dict[str, _SignalModule] = {}
        self.in_flight_steps: Dict[str, Callable[[], None]] = {}
        self.execution_ledger: OrderedDict[str, OriginExecuteReply] = OrderedDict()
        self.pending_settled_executions: List[OriginExecuteReply] = []

        self.current_epoch: int = 1
        self.current_rev: int = 0
        self.sync_enabled: bool = True
        self.last_sent_methods: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.last_sent_brief_digest: str = ""
        self.latest_visual_ref: Optional[Dict[str, Any]] = None
        self.latest_audio_ref: Optional[Dict[str, Any]] = None
        self.allowed_timeline_tags: Optional[List[str]] = None
        self.extensions_data: Dict[str, Dict[str, Any]] = {}

        self._http_server: Optional[ThreadingHTTPServer] = None
        self._outbound_stop = threading.Event()
        self._outbound_ready = threading.Event()
        self._outbound_reply_conn: Optional[http.client.HTTPConnection] = None
        self.origin_execute_url: str = ""

    @classmethod
    def from_local_hub(
        cls,
        slot_id: str,
        runtime_file: Optional[str] = None,
        shared_token: str = "",
        **kwargs: Any,
    ) -> "OriginSDK":
        origin_url = discover_local_hub_origin_url(runtime_file=runtime_file)
        tx = LoopbackHTTPTransportClient(origin_url, shared_token=shared_token)
        return cls(slot_id=slot_id, hub_transport=tx, shared_token=shared_token, **kwargs)

    def start_loopback_execute_listener(self, host: str = "127.0.0.1", port: int = 0) -> str:
        sdk_ref = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                if sdk_ref.shared_token:
                    auth = self.headers.get("Authorization", "")
                    cand = auth[7:].strip() if auth.startswith("Bearer ") else ""
                    if not cand or not hmac.compare_digest(
                        cand.encode("utf-8"), sdk_ref.shared_token.encode("utf-8")
                    ):
                        self._write_error("", "unauthenticated", 401, "Invalid Origin token")
                        return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._write_error("", "invalid_request", 400, "Invalid Content-Length")
                    return
                if length < 0 or length > (4 << 20):
                    self._write_error("", "invalid_request", 413, "Execute payload exceeds 4 MiB limit")
                    return
                raw = self.rfile.read(length)
                try:
                    env = parse_strict_json(raw)
                    if env.get("operation") != "origin.execute":
                        raise G2AOriginError("invalid_request", 400, "Only origin.execute is accepted")
                    req_obj = OriginExecuteRequest.from_dict(env["payload"])
                    reply_obj = sdk_ref.handle_origin_execute(req_obj)
                    resp = {
                        "g2a_version": G2A_WIRE_VERSION,
                        "request_id": env.get("request_id", ""),
                        "timestamp_ms": int(time.time() * 1000),
                        "acceptance": "accepted",
                        "result": reply_obj.to_dict(),
                        "error": None,
                    }
                    out = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(out)
                except StrictJSONError as jerr:
                    self._write_error("", "invalid_request", 400, str(jerr))
                except G2AOriginError as gerr:
                    self._write_error("", gerr.code, gerr.http_status, gerr.message)

            def _write_error(self, req_id: str, code: str, status: int, msg: str) -> None:
                resp = {
                    "g2a_version": G2A_WIRE_VERSION,
                    "request_id": req_id,
                    "timestamp_ms": int(time.time() * 1000),
                    "acceptance": "not_accepted",
                    "result": None,
                    "error": {"code": code, "http_status": status, "message": msg},
                }
                out = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(out)

        class _QuietThreadingHTTPServer(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                return

        self._http_server = _QuietThreadingHTTPServer((host, port), _Handler)
        bound_host, bound_port = self._http_server.server_address
        self.origin_execute_url = f"http://{bound_host}:{bound_port}/g2a/v1/origin/execute"
        threading.Thread(target=self._http_server.serve_forever, daemon=True).start()
        return self.origin_execute_url

    def stop_loopback_execute_listener(self) -> None:
        self._outbound_stop.set()
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._outbound_reply_conn is not None:
            try:
                self._outbound_reply_conn.close()
            except Exception:
                pass
            self._outbound_reply_conn = None
        close_fn = getattr(self.hub_transport, "close", None)
        if callable(close_fn):
            close_fn()

    def __del__(self) -> None:
        try:
            if self._outbound_reply_conn is not None:
                self._outbound_reply_conn.close()
                self._outbound_reply_conn = None
            close_fn = getattr(self.hub_transport, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception:
            pass

    def start_outbound_execute_stream(self, hub_origin_url: str) -> str:
        """Zero-inbound-port mode (NAT / console / strict sandbox): never calls socket.bind!"""
        base_url = hub_origin_url.rstrip("/")
        self.origin_execute_url = "pull://origin.execute"
        self._outbound_stop.clear()
        self._outbound_ready.clear()

        parsed_base = urllib.parse.urlparse(base_url)
        is_https = parsed_base.scheme == "https"
        r_host = parsed_base.hostname or "127.0.0.1"
        r_port = parsed_base.port or (443 if is_https else 80)
        reply_path = (parsed_base.path.rstrip("/") or "") + "/g2a/v1/origin/execute-reply"

        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if ("127.0.0.1" in base_url or "localhost" in base_url)
            else urllib.request.build_opener()
        )

        def _post_reply(req_id: str, acceptance: str, result: Any, error_obj: Any) -> None:
            reply_env = {
                "g2a_version": G2A_WIRE_VERSION,
                "request_id": req_id,
                "timestamp_ms": int(time.time() * 1000),
                "acceptance": acceptance,
                "result": result,
                "error": error_obj,
            }
            raw = json.dumps(reply_env, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json", "Connection": "keep-alive"}
            if self.shared_token:
                headers["Authorization"] = f"Bearer {self.shared_token}"
            for attempt in range(2):
                if self._outbound_reply_conn is None:
                    self._outbound_reply_conn = (
                        http.client.HTTPSConnection(r_host, r_port, timeout=5.0)
                        if is_https
                        else http.client.HTTPConnection(r_host, r_port, timeout=5.0)
                    )
                try:
                    self._outbound_reply_conn.request("POST", reply_path, body=raw, headers=headers)
                    resp = self._outbound_reply_conn.getresponse()
                    _ = resp.read()
                    return
                except Exception:
                    try:
                        self._outbound_reply_conn.close()
                    except Exception:
                        pass
                    self._outbound_reply_conn = None
                    if attempt == 1:
                        raise

        def _worker() -> None:
            stream_url = f"{base_url}/g2a/v1/origin/execute-stream?slot_id={self.slot_id}"
            headers = {"Accept": "text/event-stream"}
            if self.shared_token:
                headers["Authorization"] = f"Bearer {self.shared_token}"
            while not self._outbound_stop.is_set():
                req = urllib.request.Request(stream_url, method="GET", headers=headers)
                try:
                    with opener.open(req, timeout=60.0) as resp:
                        self._outbound_ready.set()
                        data_lines: List[str] = []
                        while not self._outbound_stop.is_set():
                            raw_line = resp.readline()
                            if not raw_line:
                                break
                            line = raw_line.decode("utf-8").rstrip("\r\n")
                            if line == "":
                                if data_lines:
                                    joined = "\n".join(data_lines)
                                    data_lines.clear()
                                    env = parse_strict_json(joined)
                                    req_id = env.get("request_id", "")
                                    try:
                                        exec_req = OriginExecuteRequest.from_dict(env["payload"])
                                        exec_rep = self.handle_origin_execute(exec_req)
                                        _post_reply(req_id, "accepted", exec_rep.to_dict(), None)
                                    except G2AOriginError as gerr:
                                        _post_reply(
                                            req_id,
                                            "not_accepted",
                                            None,
                                            {"code": gerr.code, "http_status": gerr.http_status, "message": gerr.message},
                                        )
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].lstrip(" "))
                except Exception:
                    if self._outbound_stop.is_set():
                        return
                    time.sleep(0.1)

        threading.Thread(target=_worker, name=f"g2a-origin-outbound-{self.slot_id}", daemon=True).start()
        self._outbound_ready.wait(timeout=3.0)
        return self.origin_execute_url

    def attach_visual_frame(self, visual_ref: Dict[str, Any], flush: bool = False) -> None:
        self.latest_visual_ref = dict(visual_ref)
        if flush:
            self.flush_dirty_sync(significant_event=False)

    def attach_audio_segment(self, audio_ref: Dict[str, Any], flush: bool = False) -> None:
        self.latest_audio_ref = dict(audio_ref)
        if flush:
            self.flush_dirty_sync(significant_event=False)

    def set_allowed_timeline_tags(self, tags: List[str], flush: bool = False) -> None:
        self.allowed_timeline_tags = list(tags)
        if flush:
            self.flush_dirty_sync(significant_event=True)

    def attach_extension_data(self, extension_name: str, data: Dict[str, Any], flush: bool = False) -> None:
        """Attaches reverse-DNS modular extension state (§8.1, e.g. org.g2a.sim.batch_blueprint.v1)."""
        self.extensions_data[extension_name] = dict(data)
        if flush:
            self.flush_dirty_sync(significant_event=False)

    # --- Developer-Facing Registration APIs (§2.2) ---

    def register_capability(
        self,
        definition: CapabilityEntry,
        effect_handler: Callable[[str, Any, Dict[str, Any], StepExecutionToken], EffectInvocationResult],
        readiness_check: Optional[Callable[[str, Any], Optional[str]]] = None,
        spatial_target_verb: bool = False,
        navigation_verb: bool = False,
    ) -> None:
        if self.slot_role_kind == "independent_npc":
            definition.permitted_actors = [a for a in definition.permitted_actors if a != "player"]
        self.capabilities[definition.method_id] = _RegisteredCap(
            definition=definition,
            effect_handler=effect_handler,
            readiness_check=readiness_check,
            spatial_target_verb=spatial_target_verb,
            navigation_verb=navigation_verb,
        )

    def publish_capabilities_and_baseline(
        self,
        disabled: bool = False,
        max_sessions: int = 0,
        shared_cooldown_ms: int = 0,
        allowlisted_cores: Optional[List[str]] = None,
        allowlist_criteria_url: str = "",
    ) -> None:
        defs = [reg.definition.to_dict() for _, reg in sorted(self.capabilities.items(), key=lambda kv: kv[0])]
        cap_digest = jcs_sha256("g2a.cap.v1", defs)
        cap_payload: Dict[str, Any] = {
            "slot_id": self.slot_id,
            "role_kind": self.slot_role_kind,
            "billing_mode": self.billing_mode,
            "player_id": self.player_id,
            "game_instance_id": self.game_instance_id,
            "room_id": self.room_id,
            "faction_id": self.faction_id,
            "origin_execute_url": self.origin_execute_url,
            "capability_table_digest": cap_digest,
            "capabilities": defs,
        }
        if disabled:
            cap_payload["disabled"] = True
        if max_sessions > 0:
            cap_payload["max_sessions"] = max_sessions
        if shared_cooldown_ms > 0:
            cap_payload["shared_cooldown_ms"] = shared_cooldown_ms
        if allowlisted_cores is not None:
            cap_payload["allowlisted_cores"] = list(allowlisted_cores)
        if allowlist_criteria_url:
            cap_payload["allowlist_criteria_url"] = allowlist_criteria_url
        self.hub_transport.post_to_hub("origin.capabilities", cap_payload)
        if not disabled:
            self.flush_full_sync()

    def bind_actor_subject(self, actor: str, instance: Any) -> None:
        self.actors[actor] = instance

    def bind_state_module(
        self,
        module_name: str,
        snapshot_fn: Callable[[], Dict[str, Any]],
        active_methods_fn: Callable[[], List[ActiveMethodGrant]],
    ) -> None:
        self.signal_modules[module_name] = _SignalModule(snapshot_fn=snapshot_fn, active_methods_fn=active_methods_fn)

    def notify_module_changed(self, module_name: str, significant_event: bool = False) -> None:
        if module_name in self.signal_modules:
            self.signal_modules[module_name].dirty = True
            self.flush_dirty_sync(significant_event=significant_event)

    def advance_epoch(self) -> None:
        self.current_epoch += 1
        self.current_rev = 0
        self.last_sent_methods.clear()
        self._cancel_all_in_flight("epoch_advanced")
        self.flush_full_sync()

    def report_player_command(self, command_text: str) -> None:
        self.flush_dirty_sync(significant_event=True)
        self.hub_transport.post_to_hub(
            "origin.player_event",
            {"slot_id": self.slot_id, "kind": "player_command", "text": command_text},
        )

    def report_player_presence(self, online: bool, remaining_online_humans_in_room: int = 0) -> None:
        if not online and remaining_online_humans_in_room == 0:
            self.sync_enabled = False
        else:
            self.sync_enabled = True
        self.hub_transport.post_to_hub(
            "origin.player_event",
            {
                "slot_id": self.slot_id,
                "kind": "presence_changed",
                "online": online,
                "remaining_online_humans": remaining_online_humans_in_room,
            },
        )

    def report_emergency_stop(self) -> None:
        self._cancel_all_in_flight("player_emergency_stop")
        self.hub_transport.post_to_hub(
            "origin.player_event",
            {"slot_id": self.slot_id, "kind": "emergency_stop"},
        )

    def report_queue_approval(
        self,
        session_id: str,
        queue_id: str,
        queue_digest: str,
        fingerprint: str,
        approved: bool,
    ) -> None:
        self.hub_transport.post_to_hub(
            "origin.player_event",
            {
                "slot_id": self.slot_id,
                "kind": "queue_approval",
                "session_id": session_id,
                "queue_id": queue_id,
                "queue_digest": queue_digest,
                "fingerprint": fingerprint,
                "approved": approved,
            },
        )

    def report_npc_host_handoff(self, new_player_id: str) -> None:
        """Section 4.3 privacy-isolated host handoff for player_metered independent_npc slots."""
        cleaned = new_player_id.strip()
        if not cleaned:
            raise G2AOriginError("invalid_arguments", 400, "new_player_id is required")
        if self.slot_role_kind != "independent_npc" or self.billing_mode != "player_metered":
            raise G2AOriginError(
                "permission_denied",
                403,
                "host_handoff is only permitted on player_metered independent_npc slots",
            )
        self._cancel_all_in_flight("npc_host_handoff")
        self.player_id = cleaned
        self.sync_enabled = True
        self.flush_dirty_sync(significant_event=False)
        self.hub_transport.post_to_hub(
            "origin.player_event",
            {
                "slot_id": self.slot_id,
                "kind": "host_handoff",
                "new_player_id": cleaned,
            },
        )

    # --- Dirty-Diff & Mass-Entity Aggregator (§2.3) ---

    def collect_current_view(self) -> Tuple[Dict[str, Any], Dict[Tuple[str, str], ActiveMethodGrant]]:
        brief: Dict[str, Any] = {}
        grants: Dict[Tuple[str, str], ActiveMethodGrant] = {}

        for mod_name, mod in self.signal_modules.items():
            brief[mod_name] = mod.snapshot_fn()
            for g in mod.active_methods_fn():
                merge_method_grant(grants, g)

        if self.spatial is not None:
            player_focus = self.spatial.get_player_focus()
            for method_id, reg in self.capabilities.items():
                for actor in reg.definition.permitted_actors:
                    if actor not in self.actors:
                        continue
                    subject = self.actors[actor]
                    if reg.readiness_check is not None:
                        r = reg.readiness_check(actor, subject)
                        if r is not None:
                            merge_method_grant(grants, ActiveMethodGrant(method_id, actor, r, []))

                    if reg.spatial_target_verb:
                        actor_pose = self.spatial.get_actor_pose(actor)
                        if actor_pose is not None:
                            candidates: List[TargetCandidate] = []
                            for obj in self.spatial.query_nearby_interactables(actor_pose, self.perception_radius):
                                if obj.hidden_secret_unrevealed:
                                    continue
                                dist = _distance(actor_pose, obj.pose)
                                readiness = (
                                    "ready"
                                    if (reg.navigation_verb or dist <= obj.interact_range)
                                    else "staged"
                                )
                                candidates.append(
                                    TargetCandidate(
                                        target_id=obj.id,
                                        label=obj.display_name,
                                        readiness=readiness,
                                        prerequisite_hint="requires_proximity" if readiness == "staged" else None,
                                        actor_anchor=f"{round(dist, 1)}m",
                                        player_anchor=_compute_player_deictic_label(player_focus, obj),
                                        alert_tag=obj.alert_tag,
                                    )
                                )
                            if candidates:
                                overall = "ready" if any(c.readiness == "ready" for c in candidates) else "staged"
                                merge_method_grant(grants, ActiveMethodGrant(method_id, actor, overall, candidates))
        return brief, grants

    def flush_dirty_sync(self, significant_event: bool = False) -> bool:
        if not self.sync_enabled and not self.pending_settled_executions:
            return False

        def _snap() -> Tuple[Dict[str, Any], Dict[Tuple[str, str], Dict[str, Any]], List[Dict[str, Any]]]:
            b, g_map = self.collect_current_view()
            serialized_grants = {k: v.to_dict() for k, v in g_map.items()}
            settled = [s.to_dict() for s in self.pending_settled_executions]
            self.pending_settled_executions.clear()
            return b, serialized_grants, settled

        brief, current_grants, settled_batch = self.engine_dispatcher.run_on_game_thread(_snap)
        brief_digest = jcs_sha256(
            "g2a.brief.v1",
            {
                "brief": brief,
                "visual": self.latest_visual_ref,
                "audio": self.latest_audio_ref,
                "timelines": self.allowed_timeline_tags,
                "extensions": self.extensions_data or None,
            },
        )

        upserted: List[Dict[str, Any]] = []
        removed: List[Dict[str, str]] = []
        for key, g_dict in current_grants.items():
            if key not in self.last_sent_methods or self.last_sent_methods[key] != g_dict:
                upserted.append(g_dict)
        for m_id, act in self.last_sent_methods:
            if (m_id, act) not in current_grants:
                removed.append({"method_id": m_id, "actor": act})

        # Zero-change zero-packet invariant!
        if (
            brief_digest == self.last_sent_brief_digest
            and not upserted
            and not removed
            and not settled_batch
        ):
            return False

        prev_rev = self.current_rev
        self.current_rev += 1
        frame = {
            "slot_id": self.slot_id,
            "epoch": self.current_epoch,
            "rev": self.current_rev,
            "base_rev": prev_rev,
            "mode": "delta",
            "significant_event": significant_event,
            "state_brief": brief,
            "upserted_methods": upserted,
            "removed_methods": removed,
            "settled_executions": settled_batch,
        }
        if self.latest_visual_ref is not None:
            frame["visual_ref"] = self.latest_visual_ref
        if self.latest_audio_ref is not None:
            frame["audio_ref"] = self.latest_audio_ref
        if self.allowed_timeline_tags is not None:
            frame["allowed_timeline_tags"] = self.allowed_timeline_tags
        if self.extensions_data:
            frame["extensions_data"] = dict(self.extensions_data)
        self.last_sent_brief_digest = brief_digest
        self.last_sent_methods = current_grants
        self.hub_transport.post_to_hub("origin.sync", frame)
        return True

    def flush_full_sync(self) -> None:
        def _snap() -> Tuple[Dict[str, Any], Dict[Tuple[str, str], Dict[str, Any]], List[Dict[str, Any]]]:
            b, g_map = self.collect_current_view()
            serialized_grants = {k: v.to_dict() for k, v in g_map.items()}
            settled = [s.to_dict() for s in self.pending_settled_executions]
            self.pending_settled_executions.clear()
            return b, serialized_grants, settled

        brief, current_grants, settled_batch = self.engine_dispatcher.run_on_game_thread(_snap)
        self.current_rev += 1
        frame = {
            "slot_id": self.slot_id,
            "epoch": self.current_epoch,
            "rev": self.current_rev,
            "mode": "full",
            "significant_event": True,
            "state_brief": brief,
            "upserted_methods": list(current_grants.values()),
            "removed_methods": [],
            "settled_executions": settled_batch,
        }
        if self.latest_visual_ref is not None:
            frame["visual_ref"] = self.latest_visual_ref
        if self.latest_audio_ref is not None:
            frame["audio_ref"] = self.latest_audio_ref
        if self.allowed_timeline_tags is not None:
            frame["allowed_timeline_tags"] = self.allowed_timeline_tags
        if self.extensions_data:
            frame["extensions_data"] = dict(self.extensions_data)
        self.last_sent_brief_digest = jcs_sha256(
            "g2a.brief.v1",
            {
                "brief": brief,
                "visual": self.latest_visual_ref,
                "audio": self.latest_audio_ref,
                "timelines": self.allowed_timeline_tags,
                "extensions": self.extensions_data or None,
            },
        )
        self.last_sent_methods = current_grants
        self.hub_transport.post_to_hub("origin.sync", frame)

    # --- Origin Interface Execute Handler (§2.4) ---

    def handle_origin_execute(self, req: OriginExecuteRequest) -> OriginExecuteReply:
        if req.kind == "cancel":
            return self.cancel_in_flight_step(req.execution_id)
        if req.kind == "deliver_chat":
            def _deliver() -> OriginExecuteReply:
                self.chat_ui_handler(
                    str(req.args.get("audience", "player_only")),
                    str(req.args.get("text", "")),
                    bool(req.args.get("ai_provenance", True)),
                )
                return OriginExecuteReply(req.execution_id, "succeeded")

            return self.engine_dispatcher.run_on_game_thread(_deliver)

        sync_needed = {"flag": False}

        def _invoke() -> OriginExecuteReply:
            if req.execution_id in self.execution_ledger:
                return self.execution_ledger[req.execution_id]
            if req.epoch != self.current_epoch:
                raise G2AOriginError("stale_context", 409, "Epoch mismatch")
            if not req.method_id or req.method_id not in self.capabilities:
                raise G2AOriginError("invalid_arguments", 400, f"Unknown method_id {req.method_id!r}")
            reg = self.capabilities[req.method_id]
            if not req.actor or req.actor not in reg.definition.permitted_actors:
                raise G2AOriginError("permission_denied", 403, "Actor not permitted")
            if req.actor not in self.actors:
                raise G2AOriginError("not_found", 404, "Actor subject not present")
            _validate_args_schema(req.args, reg.definition.params_schema)

            subject_instance = self.actors[req.actor]
            token = StepExecutionToken(req.execution_id)
            outcome = reg.effect_handler(req.actor, subject_instance, req.args, token)

            if isinstance(outcome, ImmediateSucceeded):
                rep = OriginExecuteReply(req.execution_id, "succeeded", effect_summary=outcome.effect_summary)
                self._store_ledger_reply(rep)
                sync_needed["flag"] = True
                return rep
            if isinstance(outcome, ImmediateFailed):
                rep = OriginExecuteReply(req.execution_id, "failed", failure_reason=outcome.reason)
                self._store_ledger_reply(rep)
                sync_needed["flag"] = True
                return rep
            if isinstance(outcome, StartedExecuting):
                self.in_flight_steps[req.execution_id] = outcome.cancel_fn
                rep = OriginExecuteReply(req.execution_id, "executing")
                self._store_ledger_reply(rep)
                return rep
            raise G2AOriginError("invalid_request", 400, "Invalid EffectInvocationResult")

        reply = self.engine_dispatcher.run_on_game_thread(_invoke)
        if sync_needed["flag"]:
            self.flush_dirty_sync(significant_event=False)
        return reply

    def _store_ledger_reply(self, rep: OriginExecuteReply) -> None:
        self.execution_ledger[rep.execution_id] = rep
        self.execution_ledger.move_to_end(rep.execution_id)
        if len(self.execution_ledger) > MAX_EXECUTION_LEDGER_ENTRIES:
            for old_id in list(self.execution_ledger.keys()):
                if len(self.execution_ledger) <= MAX_EXECUTION_LEDGER_ENTRIES:
                    break
                if old_id not in self.in_flight_steps:
                    self.execution_ledger.pop(old_id, None)

    def complete_step(
        self,
        execution_id: str,
        succeeded: bool,
        summary: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        settled_flag = {"queued": False}

        def _finish() -> None:
            if execution_id not in self.in_flight_steps:
                return
            self.in_flight_steps.pop(execution_id, None)
            state = "succeeded" if succeeded else "failed"
            rep = OriginExecuteReply(execution_id, state, effect_summary=summary, failure_reason=reason)
            self._store_ledger_reply(rep)
            self.pending_settled_executions.append(rep)
            settled_flag["queued"] = True

        self.engine_dispatcher.run_on_game_thread(_finish)
        if settled_flag["queued"]:
            self.flush_dirty_sync(significant_event=False)

    def cancel_in_flight_step(self, execution_id: str) -> OriginExecuteReply:
        cancelled_flag = {"queued": False}

        def _cancel() -> OriginExecuteReply:
            cancel_fn = self.in_flight_steps.pop(execution_id, None)
            if cancel_fn is not None:
                cancel_fn()
                rep = OriginExecuteReply(execution_id, "cancelled", failure_reason="cancelled_by_hub")
                self._store_ledger_reply(rep)
                self.pending_settled_executions.append(rep)
                cancelled_flag["queued"] = True
                return rep
            return self.execution_ledger.get(execution_id, OriginExecuteReply(execution_id, "cancelled"))

        rep = self.engine_dispatcher.run_on_game_thread(_cancel)
        if cancelled_flag["queued"]:
            self.flush_dirty_sync(significant_event=False)
        return rep

    def _cancel_all_in_flight(self, reason: str) -> None:
        def _clear() -> None:
            for exec_id, cancel_fn in list(self.in_flight_steps.items()):
                cancel_fn()
                rep = OriginExecuteReply(exec_id, "cancelled", failure_reason=reason)
                self._store_ledger_reply(rep)
                self.pending_settled_executions.append(rep)
            self.in_flight_steps.clear()

        self.engine_dispatcher.run_on_game_thread(_clear)
