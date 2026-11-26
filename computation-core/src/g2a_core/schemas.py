from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

G2A_WIRE_VERSION = "0.0.1"

VALID_ERROR_CODES = frozenset({
    "invalid_request",
    "invalid_arguments",
    "unsupported_version",
    "unsupported_capability",
    "unauthenticated",
    "permission_denied",
    "revoked",
    "not_found",
    "stale_context",
    "conflict",
    "confirmation_required",
    "resource_limit",
    "unavailable",
    "outcome_unknown",
})


class G2AError(Exception):
    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        retry_after_ms: Optional[int] = None,
        cooldown_ms: Optional[int] = None,
        fresh_context: Optional["ProjectedContext"] = None,
    ) -> None:
        super().__init__(f"{code} ({http_status}): {message}")
        self.code = code
        self.http_status = http_status
        self.message = message
        self.retry_after_ms = retry_after_ms
        self.cooldown_ms = cooldown_ms
        self.fresh_context = fresh_context

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "G2AError":
        fresh_raw = data.get("fresh_context")
        return cls(
            code=data["code"],
            http_status=int(data.get("http_status", 400)),
            message=str(data.get("message", "")),
            retry_after_ms=data.get("retry_after_ms"),
            cooldown_ms=data.get("cooldown_ms"),
            fresh_context=ProjectedContext.from_dict(fresh_raw) if isinstance(fresh_raw, dict) else None,
        )


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

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CapabilityEntry":
        return cls(
            method_id=d["method_id"],
            name=d["name"],
            description=d.get("description", ""),
            permitted_actors=list(d.get("permitted_actors", [])),
            params_schema=dict(d.get("params_schema", {})),
            sustained=bool(d.get("sustained", False)),
            risk_flags=list(d.get("risk_flags", [])),
            default_policy=d.get("default_policy", "allow"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method_id": self.method_id,
            "name": self.name,
            "description": self.description,
            "permitted_actors": list(self.permitted_actors),
            "params_schema": self.params_schema,
            "sustained": self.sustained,
            "risk_flags": list(self.risk_flags),
            "default_policy": self.default_policy,
        }


@dataclass
class TargetCandidate:
    target_id: str
    label: str
    readiness: str
    prerequisite_hint: Optional[str] = None
    actor_anchor: Optional[str] = None
    player_anchor: Optional[str] = None
    alert_tag: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TargetCandidate":
        return cls(
            target_id=d["target_id"],
            label=d.get("label", ""),
            readiness=d["readiness"],
            prerequisite_hint=d.get("prerequisite_hint"),
            actor_anchor=d.get("actor_anchor"),
            player_anchor=d.get("player_anchor"),
            alert_tag=d.get("alert_tag"),
        )


@dataclass
class ActiveMethodGrant:
    method_id: str
    actor: str
    readiness: str
    targets: List[TargetCandidate] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ActiveMethodGrant":
        return cls(
            method_id=d["method_id"],
            actor=d["actor"],
            readiness=d["readiness"],
            targets=[TargetCandidate.from_dict(t) for t in d.get("targets", [])],
        )


@dataclass
class ProjectedContext:
    slot_id: str
    epoch: int
    rev: int
    capability_table_digest: str
    state_brief: Dict[str, Any]
    active_methods: List[ActiveMethodGrant]
    player_command: Optional[str] = None
    visual_ref: Optional[Dict[str, Any]] = None
    audio_ref: Optional[Dict[str, Any]] = None
    allowed_timeline_tags: Optional[List[str]] = None
    extensions_data: Optional[Dict[str, Dict[str, Any]]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProjectedContext":
        raw_tags = d.get("allowed_timeline_tags")
        raw_ext = d.get("extensions_data")
        ext_map = (
            {str(k): dict(v) for k, v in raw_ext.items() if isinstance(v, dict)}
            if isinstance(raw_ext, dict)
            else None
        )
        return cls(
            slot_id=d["slot_id"],
            epoch=int(d["epoch"]),
            rev=int(d["rev"]),
            capability_table_digest=d["capability_table_digest"],
            state_brief=dict(d.get("state_brief", {})),
            active_methods=[ActiveMethodGrant.from_dict(m) for m in d.get("active_methods", [])],
            player_command=d.get("player_command"),
            visual_ref=d.get("visual_ref"),
            audio_ref=d.get("audio_ref"),
            allowed_timeline_tags=list(raw_tags) if isinstance(raw_tags, list) else None,
            extensions_data=ext_map,
        )


@dataclass
class QueueStep:
    step_index: int
    method_id: str
    actor: str
    args: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "method_id": self.method_id,
            "actor": self.actor,
            "args": dict(self.args),
        }


@dataclass
class MethodCallQueue:
    queue_id: str
    epoch: int
    based_on_rev: int
    trigger: str  # "player_command" | "proactive"
    supersedes: bool
    intent_summary: str
    steps: List[QueueStep]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "epoch": self.epoch,
            "based_on_rev": self.based_on_rev,
            "trigger": self.trigger,
            "supersedes": self.supersedes,
            "intent_summary": self.intent_summary,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class StepReceipt:
    step_index: int
    method_id: str
    actor: str
    state: str
    effect_summary: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StepReceipt":
        return cls(
            step_index=int(d["step_index"]),
            method_id=d["method_id"],
            actor=d["actor"],
            state=d["state"],
            effect_summary=d.get("effect_summary"),
            failure_reason=d.get("failure_reason"),
        )


@dataclass
class QueueReceipt:
    queue_id: str
    queue_state: str
    step_receipts: List[StepReceipt]
    latest_context: Optional[ProjectedContext] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QueueReceipt":
        latest_raw = d.get("latest_context")
        return cls(
            queue_id=d["queue_id"],
            queue_state=d["queue_state"],
            step_receipts=[StepReceipt.from_dict(s) for s in d.get("step_receipts", [])],
            latest_context=ProjectedContext.from_dict(latest_raw) if isinstance(latest_raw, dict) else None,
        )


@dataclass
class FrozenScope:
    player_id: str
    core_id: str
    game_instance_id: str
    slot_id: str
    role_kind: str
    billing_mode: str
    permitted_methods: Dict[str, List[str]]
    capability_table_digest: str
    allow_visual: bool = False
    allow_audio: bool = False
    allow_chat: bool = True

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FrozenScope":
        return cls(
            player_id=d["player_id"],
            core_id=d["core_id"],
            game_instance_id=d["game_instance_id"],
            slot_id=d["slot_id"],
            role_kind=d["role_kind"],
            billing_mode=d["billing_mode"],
            permitted_methods={k: list(v) for k, v in d.get("permitted_methods", {}).items()},
            capability_table_digest=d["capability_table_digest"],
            allow_visual=bool(d.get("allow_visual", False)),
            allow_audio=bool(d.get("allow_audio", False)),
            allow_chat=bool(d.get("allow_chat", False)),
        )


@dataclass
class PlanDecision:
    intent_summary: str = ""
    planned_steps: List[QueueStep] = field(default_factory=list)
    reply_text: Optional[str] = None
    audience: str = "player_only"
