from __future__ import annotations

import base64
import hashlib
import http.client
import inspect
import json
import os
import random
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
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Union

from .agent_runtime import (
    ActionOutcomeRecord,
    AgentGoalState,
    AgentGoalTracker,
    GoalExecutionReport,
    resolve_deictic_target,
    resolve_prerequisite_steps,
)
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
    QueueStep,
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


class FunctionCognitivePlanner:
    """Ergonomic adapter wrapping a plain callable `(caps, ctx, memories[, active_goal, recent_outcomes]) -> PlanDecision`."""

    def __init__(
        self,
        fn: Callable[..., PlanDecision],
    ) -> None:
        self._fn = fn
        try:
            params = inspect.signature(fn).parameters
            self._accepts_goal = "active_goal" in params or "recent_outcomes" in params
        except (TypeError, ValueError):
            self._accepts_goal = False

    def plan(
        self,
        capabilities: List[CapabilityEntry],
        context: ProjectedContext,
        recalled_memories: List[MemoryRecord],
        active_goal: Optional[AgentGoalState] = None,
        recent_outcomes: Optional[List[ActionOutcomeRecord]] = None,
    ) -> PlanDecision:
        if self._accepts_goal:
            return self._fn(
                capabilities,
                context,
                recalled_memories,
                active_goal=active_goal,
                recent_outcomes=recent_outcomes,
            )
        return self._fn(capabilities, context, recalled_memories)


class QueueBuilder:
    """
    Fluent step builder with zero-latency client-side pre-flight validation against:
      1. FrozenScope permitted_methods;
      2. ProjectedContext active_methods (readiness + target_id candidate membership);
      3. Cached CapabilityEntry JSON Schema.
    """

    def __init__(
        self,
        sdk: "CoreSDK",
        context: ProjectedContext,
        intent_summary: str = "Execute planned companion queue",
        trigger: Optional[str] = None,
        supersedes: Optional[bool] = None,
    ) -> None:
        self._sdk = sdk
        self._ctx = context
        self._intent_summary = intent_summary
        is_cmd = context.player_command is not None
        self._trigger = trigger or ("player_command" if is_cmd else "proactive")
        self._supersedes = is_cmd if supersedes is None else supersedes
        self._steps: List[QueueStep] = []
        self._grant_map = {(g.method_id, g.actor): g for g in context.active_methods}
        caps = sdk.capability_cache.get(context.capability_table_digest, [])
        self._cap_map = {c.method_id: c for c in caps}

    def add_step(
        self,
        method_id: str,
        actor: str,
        args: Optional[Dict[str, Any]] = None,
        validate: bool = True,
        allow_initial_staged: bool = False,
    ) -> "QueueBuilder":
        step_args = dict(args or {})
        idx = len(self._steps)
        if validate:
            if self._sdk.scope is not None:
                allowed_actors = self._sdk.scope.permitted_methods.get(method_id)
                if allowed_actors is not None and actor not in allowed_actors:
                    raise G2AError("permission_denied", 403, f"Scope forbids ({method_id}, {actor})")
            grant = self._grant_map.get((method_id, actor))
            if grant is None:
                raise G2AError("invalid_arguments", 400, f"Method ({method_id}, {actor}) not active in current context")
            if idx == 0 and not allow_initial_staged and grant.readiness != "ready":
                raise G2AError("stale_context", 409, f"Initial step ({method_id}, {actor}) is staged, not ready")
            if "target_id" in step_args and grant.targets:
                tid = step_args["target_id"]
                cand = next((t for t in grant.targets if t.target_id == tid), None)
                if cand is None:
                    raise G2AError("not_found", 404, f"Target {tid!r} not in active candidates for {method_id}")
                if idx == 0 and not allow_initial_staged and cand.readiness != "ready":
                    raise G2AError("stale_context", 409, f"Initial target {tid!r} is staged ({cand.prerequisite_hint})")
            cap = self._cap_map.get(method_id)
            if cap is not None and cap.params_schema:
                from .llm_planner import OpenAICompatibleLLMPlanner

                if not OpenAICompatibleLLMPlanner._validate_object_schema(step_args, cap.params_schema, 0):
                    raise G2AError("invalid_arguments", 400, f"Arguments for {method_id} failed local JSON Schema check")

        self._steps.append(QueueStep(step_index=idx, method_id=method_id, actor=actor, args=step_args))
        return self

    def add_step_auto(
        self,
        method_id: str,
        actor: str,
        args: Optional[Dict[str, Any]] = None,
        validate: bool = True,
    ) -> "QueueBuilder":
        """
        Automatically unrolls `"staged"` targets/methods into a valid `[ready_prerequisite, ..., target_step]`
        chain (e.g. prepending `#01 move_to` when `prerequisite_hint` requires proximity) before validation.
        """
        caps = list(self._cap_map.values())
        chain = resolve_prerequisite_steps(method_id, actor, args, self._ctx, caps)
        for c_mid, c_actor, c_args in chain:
            self.add_step(c_mid, c_actor, c_args, validate=validate)
        return self

    def build(self, queue_id: Optional[str] = None) -> MethodCallQueue:
        if not self._steps:
            raise G2AError("invalid_arguments", 400, "Cannot build an empty MethodCallQueue")
        return MethodCallQueue(
            queue_id=queue_id or generate_id("Q"),
            epoch=self._ctx.epoch,
            based_on_rev=self._ctx.rev,
            trigger=self._trigger,
            supersedes=self._supersedes,
            intent_summary=self._intent_summary,
            steps=list(self._steps),
        )

    def submit(self, queue_id: Optional[str] = None) -> Optional[QueueReceipt]:
        return self._sdk.submit_queue_with_standard_recovery(self.build(queue_id=queue_id))


@dataclass
class CoreMetrics:
    contexts_received: int = 0
    queues_submitted: int = 0
    reconciled_unknowns: int = 0
    cooldown_shield_hits: int = 0
    backoff_shield_hits: int = 0
    stream_reconnects: int = 0


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
        self._state_lock = threading.RLock()
        self.pending_confirm_queues: Dict[str, MethodCallQueue] = {}
        self._confirm_hold_ids: Set[str] = set()
        self._early_confirmed_queues: OrderedDict[str, None] = OrderedDict()
        self._early_declined_queues: OrderedDict[str, int] = OrderedDict()
        self._declined_queue_ids: OrderedDict[str, None] = OrderedDict()
        self.completed_receipts: List[QueueReceipt] = []
        self.seen_event_ids: OrderedDict[str, None] = OrderedDict()
        self.last_event_id: str = ""
        self.next_allowed_pull_ms: int = 0
        self.refusal_cooldowns: Dict[str, int] = {}
        self._http_tls = threading.local()
        self._pool_lock = threading.Lock()
        self._pooled_conns: List[http.client.HTTPConnection] = []

        self.hub_endpoint: str = ""
        self.auth_token: str = ""
        self.session_id: Optional[str] = None
        self.scope: Optional[FrozenScope] = None
        self.negotiated_extensions: List[str] = []
        self.actions_paused: bool = False
        self.closed: bool = False
        self.latest_context: Optional[ProjectedContext] = None

        self._stop_event = threading.Event()
        self._stream_ready = threading.Event()
        self._goal_wakeup = threading.Event()
        self._in_execute_goal: bool = False
        self._hb_thread: Optional[threading.Thread] = None
        self._sse_thread: Optional[threading.Thread] = None
        self.on_receipt_callback: Optional[Callable[[QueueReceipt], None]] = None
        self.metrics = CoreMetrics()
        self.goal_tracker = AgentGoalTracker()

    @property
    def is_stream_ready(self) -> bool:
        return self._stream_ready.is_set()

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
            self._stream_ready.clear()
            self._start_heartbeat_loop_async()
            if self.stream_mode == "websocket":
                self._start_websocket_stream_async()
            else:
                self._start_event_stream_async()
            if self._custom_post_fn is None:
                self._stream_ready.wait(timeout=2.0)
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

    @classmethod
    def connect_quick(
        cls,
        slot_id: str,
        auth_token: str,
        planner: Union[CognitivePlannerPort, Callable[..., PlanDecision], None] = None,
        hub_endpoint: str = "auto",
        fallback_url: Optional[str] = None,
        runtime_file: Optional[str] = None,
        stream_mode: str = "websocket",
        allow_loopback: bool = True,
        memory_store: Optional[InMemoryCompanionStore] = None,
        **connect_kwargs: Any,
    ) -> "CoreSDK":
        """One-step context-managed CoreSDK bootstrap with local Hub auto-discovery and callable planner support."""
        if planner is None:
            resolved_planner: CognitivePlannerPort = FunctionCognitivePlanner(lambda c, x, m: PlanDecision())
        elif hasattr(planner, "plan"):
            resolved_planner = planner  # type: ignore[assignment]
        else:
            resolved_planner = FunctionCognitivePlanner(planner)  # type: ignore[arg-type]

        sdk = cls(
            planner=resolved_planner,
            memory_store=memory_store,
            allow_loopback=allow_loopback,
            stream_mode=stream_mode,
        )
        resolved_ep = hub_endpoint
        if hub_endpoint == "auto":
            try:
                manifest = discover_local_hub_manifest(runtime_file=runtime_file)
                resolved_ep = str(manifest["core_url"])
            except Exception:
                if not fallback_url:
                    raise
                resolved_ep = fallback_url
        sdk.connect(resolved_ep, auth_token, slot_id, **connect_kwargs)
        return sdk

    def build_queue(
        self,
        context: Optional[ProjectedContext] = None,
        intent_summary: str = "Execute planned companion queue",
        trigger: Optional[str] = None,
        supersedes: Optional[bool] = None,
    ) -> QueueBuilder:
        ctx = context or self.latest_context
        if ctx is None:
            ctx = self.pull_context()
        return QueueBuilder(self, ctx, intent_summary=intent_summary, trigger=trigger, supersedes=supersedes)

    def __enter__(self) -> "CoreSDK":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown_session_cleanly(notify_hub=True)

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
        self._close_all_keepalive_conns()

    def __del__(self) -> None:
        try:
            self._close_all_keepalive_conns()
        except Exception:
            pass

    # --- Reconnect & Resume State Machine (§4.2) ---

    def get_cached_capabilities(self, slot_id: Optional[str] = None) -> List[CapabilityEntry]:
        target_slot = slot_id or (self.scope.slot_id if self.scope is not None else "")
        digest = self.slot_digest_index.get(target_slot, "")
        if not digest and self.scope is not None:
            digest = self.scope.capability_table_digest
        return list(self.capability_cache.get(digest, []))

    def send_heartbeat(self) -> Dict[str, Any]:
        if not self.session_id or self.closed:
            raise G2AError("revoked", 403, "Session is closed")
        return self._post_envelope("session.heartbeat", {}, session_id=self.session_id)

    def resume_session(self) -> Dict[str, Any]:
        if not self.session_id or self.closed:
            raise G2AError("revoked", 403, "Session is closed")
        res = self._post_envelope("session.resume", {}, session_id=self.session_id)
        self.actions_paused = bool(res.get("actions_paused", False))
        if isinstance(res.get("latest_context"), dict):
            self.latest_context = ProjectedContext.from_dict(res["latest_context"])
        return res

    def query_queue(self, queue_id: str) -> QueueReceipt:
        if not self.session_id or self.closed:
            raise G2AError("revoked", 403, "Session is closed")
        res = self._post_envelope("queue.query", {"queue_id": queue_id}, session_id=self.session_id)
        receipt = QueueReceipt.from_dict(res)
        self._record_receipt(receipt)
        return receipt

    def cancel_queue(self, queue_id: str) -> QueueReceipt:
        if not self.session_id or self.closed:
            raise G2AError("revoked", 403, "Session is closed")
        res = self._post_envelope("queue.cancel", {"queue_id": queue_id}, session_id=self.session_id)
        receipt = QueueReceipt.from_dict(res)
        self._record_receipt(receipt)
        return receipt

    def send_chat(self, text: str, audience: str = "player_only", message_id: Optional[str] = None) -> Dict[str, Any]:
        if not self.session_id or self.closed:
            raise G2AError("revoked", 403, "Session is closed")
        if self.scope is not None and not self.scope.allow_chat:
            raise G2AError("permission_denied", 403, "FrozenScope forbids chat.send")
        return self._post_envelope(
            "chat.send",
            {
                "message_id": message_id or generate_id("msg"),
                "audience": audience,
                "text": text,
                "provenance": "ai_companion",
            },
            session_id=self.session_id,
        )

    def on_stream_reconnected(self) -> None:
        if self.closed or not self.session_id:
            return
        self.metrics.stream_reconnects += 1
        try:
            self.resume_session()
        except G2AError as err:
            if err.code in ("revoked", "permission_denied"):
                self.shutdown_session_cleanly(notify_hub=False)
            else:
                time.sleep(0.25)

    def on_event_session_resumed(self, fresh_ctx: ProjectedContext) -> None:
        self.actions_paused = False
        self.on_context_received(fresh_ctx)

    # --- Context Consumption & Queue Submission Recovery (§4.3) ---

    def pull_context(self) -> ProjectedContext:
        if not self.session_id or self.closed:
            raise G2AError("revoked", 403, "Session is closed")
        try:
            res = self._post_envelope("context.pull", {}, session_id=self.session_id)
        except G2AError as err:
            if err.code == "resource_limit" and err.retry_after_ms is not None:
                self.next_allowed_pull_ms = int(time.time() * 1000) + int(err.retry_after_ms)
            raise
        ctx = ProjectedContext.from_dict(res)
        self.latest_context = ctx
        return ctx

    def pull_context_if_due(self) -> Optional[ProjectedContext]:
        """Client-side adaptive quiet-period polling shield (§3.4 & §6.3): avoids wasted 429 HTTP round-trips."""
        if self.closed or not self.session_id:
            return None
        if int(time.time() * 1000) < self.next_allowed_pull_ms:
            self.metrics.backoff_shield_hits += 1
            return None
        try:
            return self.pull_context()
        except G2AError as err:
            if err.code == "resource_limit":
                self.metrics.backoff_shield_hits += 1
                return None
            raise

    def _call_planner(
        self,
        caps: List[CapabilityEntry],
        ctx: ProjectedContext,
        memories: List[MemoryRecord],
    ) -> PlanDecision:
        plan_fn = self.planner.plan
        try:
            sig = inspect.signature(plan_fn)
            if "active_goal" in sig.parameters or "recent_outcomes" in sig.parameters:
                return plan_fn(  # type: ignore[call-arg]
                    caps,
                    ctx,
                    memories,
                    active_goal=self.goal_tracker.active_goal,
                    recent_outcomes=self.goal_tracker.recent_outcomes,
                )
        except (TypeError, ValueError):
            pass
        return plan_fn(caps, ctx, memories)

    def on_context_received(self, ctx: ProjectedContext, _replan_depth: int = 0) -> Optional[QueueReceipt]:
        if self.closed or self.actions_paused or not self.session_id or not self.scope:
            return None
        self.metrics.contexts_received += 1
        self.latest_context = ctx
        if self._in_execute_goal:
            self._goal_wakeup.set()
            return None

        # If a new player_command arrives while an older autonomous goal was running, supersede it cleanly;
        # conversely, if an autonomous multi-turn goal is in_progress and player_command is None, synthesize the objective!
        active_goal = self.goal_tracker.active_goal
        effective_ctx = ctx
        if ctx.player_command:
            if active_goal is not None and active_goal.objective != ctx.player_command:
                self.goal_tracker.clear_goal(status="superseded")
                active_goal = None
        elif active_goal is not None and active_goal.status == "in_progress":
            if active_goal.turns_used >= active_goal.max_turns:
                self.goal_tracker.clear_goal(status="exhausted")
            else:
                effective_ctx = ProjectedContext(
                    slot_id=ctx.slot_id,
                    epoch=ctx.epoch,
                    rev=ctx.rev,
                    capability_table_digest=ctx.capability_table_digest,
                    state_brief=ctx.state_brief,
                    active_methods=ctx.active_methods,
                    player_command=active_goal.objective,
                    visual_ref=ctx.visual_ref,
                    audio_ref=ctx.audio_ref,
                    allowed_timeline_tags=ctx.allowed_timeline_tags,
                    extensions_data=ctx.extensions_data,
                )

        caps = self.capability_cache.get(effective_ctx.capability_table_digest, [])
        allowed_tags = set(effective_ctx.allowed_timeline_tags) if effective_ctx.allowed_timeline_tags is not None else None
        memories = self.memory_store.recall_safe(
            self.scope.player_id,
            SpoilerGuard(
                current_epoch=effective_ctx.epoch,
                game_origin_id=self.scope.game_instance_id,
                allowed_timeline_tags=allowed_tags,
                query_hint=effective_ctx.player_command,
            ),
        )
        decision = self._call_planner(caps, effective_ctx, memories)

        if decision.memory_note:
            AgentGoalTracker.record_episodic_observation(
                self.memory_store,
                self.scope.player_id,
                self.scope.game_instance_id,
                effective_ctx,
                decision.memory_note,
            )

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

        # Anti-infinite-loop guard: strip any step that already failed on the exact same state_brief!
        filtered_steps: List[QueueStep] = []
        for s in decision.planned_steps:
            if self.goal_tracker.is_failed_repeat(effective_ctx.state_brief, s.method_id, s.actor, s.args):
                break
            filtered_steps.append(QueueStep(len(filtered_steps), s.method_id, s.actor, s.args))

        if filtered_steps:
            if decision.goal_status == "continue" and self.goal_tracker.active_goal is None and effective_ctx.player_command:
                self.goal_tracker.start_goal(effective_ctx.player_command)
            if self.goal_tracker.active_goal is not None:
                self.goal_tracker.active_goal.turns_used += 1

            is_cmd = effective_ctx.player_command is not None
            queue = MethodCallQueue(
                queue_id=generate_id("Q"),
                epoch=effective_ctx.epoch,
                based_on_rev=effective_ctx.rev,
                trigger="player_command" if is_cmd else "proactive",
                supersedes=is_cmd,
                intent_summary=decision.intent_summary or "Execute planned companion queue",
                steps=filtered_steps,
            )
            rec = self.submit_queue_with_standard_recovery(queue, _replan_depth=_replan_depth, _submit_ctx=effective_ctx)
            if rec is not None and rec.queue_state == "completed" and decision.goal_status == "completed":
                self.goal_tracker.clear_goal(status="completed")
            return rec

        if decision.goal_status in ("completed", "blocked"):
            self.goal_tracker.clear_goal(status=decision.goal_status)
        return None

    def wait_for_queue_terminal(
        self,
        queue_id: str,
        initial_receipt: Optional[QueueReceipt] = None,
        submitted_steps: Optional[List[Tuple[str, str, Dict[str, Any]]]] = None,
        submit_ctx: Optional[ProjectedContext] = None,
        timeout_sec: float = 5.0,
    ) -> Optional[QueueReceipt]:
        """
        Awaits terminal settlement of a submitted `MethodCallQueue` across:
          1. Sustained in-flight execution (`queue_state` in `("running", "executing")`) via `queue.query`;
          2. Player confirmation hold (`pending_confirm_queues`) waking reactively on `queue.confirmed` / `queue.declined`.
        """
        rec = initial_receipt
        if rec is not None and rec.queue_state not in ("running", "executing", "awaiting_confirmation"):
            return rec

        end_t = time.monotonic() + timeout_sec
        while time.monotonic() < end_t and not self.closed:
            with self._state_lock:
                ever_held = queue_id in self._confirm_hold_ids
                was_declined = queue_id in self._declined_queue_ids
                matched_done = next((r for r in reversed(self.completed_receipts) if r.queue_id == queue_id), None)

            if was_declined:
                return QueueReceipt(queue_id=queue_id, queue_state="cancelled", step_receipts=[])

            if matched_done is not None and matched_done.queue_state not in ("running", "executing", "awaiting_confirmation"):
                return matched_done

            if initial_receipt is None and not ever_held:
                return None

            if ever_held and matched_done is None:
                self._goal_wakeup.clear()
                self._goal_wakeup.wait(timeout=min(0.06, max(0.01, end_t - time.monotonic())))
                continue

            try:
                rec = self.query_queue(queue_id)
            except G2AError:
                break

            if rec is not None and rec.queue_state not in ("running", "executing", "awaiting_confirmation"):
                if submitted_steps is not None:
                    self.goal_tracker.record_receipt(rec, submitted_steps, submit_ctx)
                return rec

            self._goal_wakeup.clear()
            self._goal_wakeup.wait(timeout=min(0.05, max(0.01, end_t - time.monotonic())))
        return rec

    def execute_goal(
        self,
        objective: str,
        success_when: Optional[Callable[[ProjectedContext, Optional[QueueReceipt]], bool]] = None,
        max_turns: int = 6,
        timeout_sec: float = 10.0,
    ) -> GoalExecutionReport:
        """
        Closed-loop autonomous goal executor: plans, executes, waits for sustained steps or confirmations,
        feeds step failures back into the planner for self-healing replanning, and verifies completion.
        """
        goal = self.goal_tracker.start_goal(objective, max_turns=max_turns)
        deadline = time.monotonic() + timeout_sec
        last_receipt: Optional[QueueReceipt] = None
        last_ctx: Optional[ProjectedContext] = None
        last_planned_rev: int = -1
        self._in_execute_goal = True

        def _pull_fresh_with_backoff() -> Optional[ProjectedContext]:
            p = self.pull_context_if_due()
            if p is not None:
                return p
            now_ms = int(time.time() * 1000)
            if self.next_allowed_pull_ms > now_ms and time.monotonic() < deadline:
                wait_s = min((self.next_allowed_pull_ms - now_ms) / 1000.0 + 0.015, max(0.01, deadline - time.monotonic()))
                self._goal_wakeup.clear()
                self._goal_wakeup.wait(timeout=wait_s)
                return self.pull_context_if_due()
            return None

        try:
            while time.monotonic() < deadline and not self.closed and not self.actions_paused:
                pulled = _pull_fresh_with_backoff()
                ctx = pulled or self.latest_context
                if ctx is None:
                    ctx = self.pull_context()
                last_ctx = ctx

                if success_when is not None and success_when(ctx, last_receipt):
                    goal.status = "completed"
                    return GoalExecutionReport(
                        goal_id=goal.goal_id,
                        objective=objective,
                        succeeded=True,
                        status="completed",
                        turns_used=goal.turns_used,
                        submitted_queues=list(goal.submitted_queues),
                        outcomes=list(goal.recent_outcomes),
                        final_context=ctx,
                    )

                if goal.turns_used >= max_turns:
                    goal.status = "exhausted"
                    return GoalExecutionReport(
                        goal_id=goal.goal_id,
                        objective=objective,
                        succeeded=False,
                        status="exhausted",
                        turns_used=goal.turns_used,
                        submitted_queues=list(goal.submitted_queues),
                        outcomes=list(goal.recent_outcomes),
                        final_context=ctx,
                        failure_reason=goal.last_failure_reason or "max_turns_exceeded",
                    )

                # Wait if a sustained step is still running/executing or awaiting player confirmation on the same rev
                if (
                    (last_receipt is not None and last_receipt.queue_state in ("running", "executing") and ctx.rev == last_planned_rev)
                    or bool(self.pending_confirm_queues)
                ):
                    self._goal_wakeup.clear()
                    self._goal_wakeup.wait(timeout=min(0.15, max(0.01, deadline - time.monotonic())))
                    continue

                last_planned_rev = ctx.rev
                synth_ctx = ProjectedContext(
                    slot_id=ctx.slot_id,
                    epoch=ctx.epoch,
                    rev=ctx.rev,
                    capability_table_digest=ctx.capability_table_digest,
                    state_brief=ctx.state_brief,
                    active_methods=ctx.active_methods,
                    player_command=objective,
                    visual_ref=ctx.visual_ref,
                    audio_ref=ctx.audio_ref,
                    allowed_timeline_tags=ctx.allowed_timeline_tags,
                    extensions_data=ctx.extensions_data,
                )
                caps = self.capability_cache.get(synth_ctx.capability_table_digest, [])
                allowed_tags = set(synth_ctx.allowed_timeline_tags) if synth_ctx.allowed_timeline_tags is not None else None
                memories = (
                    self.memory_store.recall_safe(
                        self.scope.player_id,
                        SpoilerGuard(
                            current_epoch=synth_ctx.epoch,
                            game_origin_id=self.scope.game_instance_id,
                            allowed_timeline_tags=allowed_tags,
                            query_hint=objective,
                        ),
                    )
                    if self.scope is not None
                    else []
                )
                decision = self._call_planner(caps, synth_ctx, memories)

                if decision.memory_note and self.scope is not None:
                    AgentGoalTracker.record_episodic_observation(
                        self.memory_store,
                        self.scope.player_id,
                        self.scope.game_instance_id,
                        synth_ctx,
                        decision.memory_note,
                    )
                if decision.reply_text and self.scope is not None and self.scope.allow_chat:
                    self.send_chat(decision.reply_text, audience=decision.audience)

                filtered_steps = []
                for s in decision.planned_steps:
                    if self.goal_tracker.is_failed_repeat(synth_ctx.state_brief, s.method_id, s.actor, s.args):
                        break
                    filtered_steps.append(QueueStep(len(filtered_steps), s.method_id, s.actor, s.args))

                if not filtered_steps:
                    status = "blocked" if decision.goal_status == "blocked" else "no_executable_steps"
                    return GoalExecutionReport(
                        goal_id=goal.goal_id,
                        objective=objective,
                        succeeded=False,
                        status=status,
                        turns_used=goal.turns_used,
                        submitted_queues=list(goal.submitted_queues),
                        outcomes=list(goal.recent_outcomes),
                        final_context=ctx,
                        failure_reason=goal.last_failure_reason or status,
                    )

                goal.turns_used += 1
                queue = MethodCallQueue(
                    queue_id=generate_id("Q"),
                    epoch=synth_ctx.epoch,
                    based_on_rev=synth_ctx.rev,
                    trigger="player_command",
                    supersedes=(goal.turns_used == 1),
                    intent_summary=decision.intent_summary or objective,
                    steps=filtered_steps,
                )
                submitted_tuples = [(s.method_id, s.actor, s.args) for s in filtered_steps]
                initial_rec = self.submit_queue_with_standard_recovery(queue, _submit_ctx=synth_ctx)
                last_receipt = self.wait_for_queue_terminal(
                    queue.queue_id,
                    initial_receipt=initial_rec,
                    submitted_steps=submitted_tuples if (initial_rec is None or initial_rec.queue_state in ("running", "executing")) else None,
                    submit_ctx=synth_ctx,
                    timeout_sec=max(0.1, deadline - time.monotonic()),
                )
                if last_receipt is not None and last_receipt.queue_state == "cancelled" and initial_rec is None:
                    return GoalExecutionReport(
                        goal_id=goal.goal_id,
                        objective=objective,
                        succeeded=False,
                        status="declined_by_player",
                        turns_used=goal.turns_used,
                        submitted_queues=list(goal.submitted_queues),
                        outcomes=list(goal.recent_outcomes),
                        final_context=last_ctx,
                        failure_reason="declined_by_player",
                    )
                if last_receipt is not None and last_receipt.latest_context is not None:
                    last_ctx = last_receipt.latest_context

                # Pull fresh state after step execution so success_when / next turn sees updated world!
                fresh_after = _pull_fresh_with_backoff()
                if fresh_after is not None:
                    last_ctx = fresh_after
                elif self.latest_context is not None and self.latest_context.rev > synth_ctx.rev:
                    last_ctx = self.latest_context

                if success_when is not None and last_ctx is not None and success_when(last_ctx, last_receipt):
                    return GoalExecutionReport(
                        goal_id=goal.goal_id,
                        objective=objective,
                        succeeded=True,
                        status="completed",
                        turns_used=goal.turns_used,
                        submitted_queues=list(goal.submitted_queues),
                        outcomes=list(goal.recent_outcomes),
                        final_context=last_ctx,
                    )
                if (
                    success_when is None
                    and last_receipt is not None
                    and last_receipt.queue_state == "completed"
                    and decision.goal_status == "completed"
                ):
                    return GoalExecutionReport(
                        goal_id=goal.goal_id,
                        objective=objective,
                        succeeded=True,
                        status="completed",
                        turns_used=goal.turns_used,
                        submitted_queues=list(goal.submitted_queues),
                        outcomes=list(goal.recent_outcomes),
                        final_context=last_ctx,
                    )

                if last_receipt is None or last_receipt.queue_state in ("running", "executing"):
                    self._goal_wakeup.clear()
                    self._goal_wakeup.wait(timeout=min(0.15, max(0.01, deadline - time.monotonic())))

            return GoalExecutionReport(
                goal_id=goal.goal_id,
                objective=objective,
                succeeded=False,
                status="timeout" if time.monotonic() >= deadline else "aborted",
                turns_used=goal.turns_used,
                submitted_queues=list(goal.submitted_queues),
                outcomes=list(goal.recent_outcomes),
                final_context=last_ctx,
                failure_reason=goal.last_failure_reason or "goal_timeout",
            )
        finally:
            self._in_execute_goal = False
            self.goal_tracker.clear_goal()

    def submit_queue_with_standard_recovery(
        self,
        queue: MethodCallQueue,
        _replan_depth: int = 0,
        _submit_ctx: Optional[ProjectedContext] = None,
    ) -> Optional[QueueReceipt]:
        if self.closed or not self.session_id:
            return None
        submitted_tuples = [(s.method_id, s.actor, s.args) for s in queue.steps]
        ctx_snap = _submit_ctx or self.latest_context
        # Client-side refusal cooldown pre-filter for proactive queues (avoids redundant 429 network round-trips)
        now_ms = int(time.time() * 1000)
        with self._state_lock:
            for k in [k for k, until in self.refusal_cooldowns.items() if now_ms >= until]:
                self.refusal_cooldowns.pop(k, None)
            if queue.trigger == "proactive":
                fp = jcs_sha256("g2a.fp.v1", [s.to_dict() for s in queue.steps])
                if now_ms < self.refusal_cooldowns.get(fp, 0):
                    self.metrics.cooldown_shield_hits += 1
                    return None
        try:
            self.metrics.queues_submitted += 1
            res = self._post_envelope("queue.submit", queue.to_dict(), session_id=self.session_id)
            receipt = QueueReceipt.from_dict(res)
            self.goal_tracker.record_receipt(receipt, submitted_tuples, ctx_snap)
            self._record_receipt(receipt)
            return receipt
        except G2AError as err:
            if err.code == "confirmation_required":
                # Reconcile atomically in case SSE/WS delivered queue.confirmed or queue.declined before HTTP 409 arrived!
                resubmit_now = False
                with self._state_lock:
                    self._confirm_hold_ids.add(queue.queue_id)
                    if queue.queue_id in self._early_confirmed_queues:
                        self._early_confirmed_queues.pop(queue.queue_id, None)
                        resubmit_now = True
                    elif queue.queue_id in self._early_declined_queues:
                        cd_ms = self._early_declined_queues.pop(queue.queue_id)
                        self._declined_queue_ids[queue.queue_id] = None
                        fp = jcs_sha256("g2a.fp.v1", [s.to_dict() for s in queue.steps])
                        self.refusal_cooldowns[fp] = int(time.time() * 1000) + cd_ms
                        self._goal_wakeup.set()
                    else:
                        self.pending_confirm_queues[queue.queue_id] = queue
                if resubmit_now:
                    return self.submit_queue_with_standard_recovery(queue, _replan_depth=_replan_depth)
                return None
            if err.code == "stale_context":
                if err.fresh_context is not None:
                    self.latest_context = err.fresh_context
                    if not self._in_execute_goal and _replan_depth < 2:
                        return self.on_context_received(err.fresh_context, _replan_depth=_replan_depth + 1)
                return None
            if err.code == "outcome_unknown":
                # 504 timeout rule: NEVER blindly resend queue.submit or mint a new queue_id; reconcile via queue.query!
                self.metrics.reconciled_unknowns += 1
                return self.reconcile_unknown_queue(queue.queue_id)
            if err.code == "resource_limit":
                wait_sec = min((err.retry_after_ms or 500) / 1000.0, 2.0)
                time.sleep(max(wait_sec, 0.01))
                return None
            if err.code in ("revoked", "permission_denied") and "offline" in err.message.lower():
                self.shutdown_session_cleanly(notify_hub=False)
            return None

    def on_event_queue_confirmed(self, queue_id: str) -> Optional[QueueReceipt]:
        with self._state_lock:
            original = self.pending_confirm_queues.pop(queue_id, None)
            if original is None:
                self._early_confirmed_queues[queue_id] = None
                if len(self._early_confirmed_queues) > 256:
                    self._early_confirmed_queues.popitem(last=False)
        if original is not None:
            rec = self.submit_queue_with_standard_recovery(original)
            self._goal_wakeup.set()
            return rec
        return None

    def on_event_queue_declined(self, queue_id: str, cooldown_ms: int = 60000) -> None:
        with self._state_lock:
            self._declined_queue_ids[queue_id] = None
            if len(self._declined_queue_ids) > 256:
                self._declined_queue_ids.popitem(last=False)
            declined = self.pending_confirm_queues.pop(queue_id, None)
            if declined is not None:
                fp = jcs_sha256("g2a.fp.v1", [s.to_dict() for s in declined.steps])
                self.refusal_cooldowns[fp] = int(time.time() * 1000) + cooldown_ms
            else:
                self._early_declined_queues[queue_id] = cooldown_ms
                if len(self._early_declined_queues) > 256:
                    self._early_declined_queues.popitem(last=False)
        self._goal_wakeup.set()

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
            with self._state_lock:
                if ev_id in self.seen_event_ids:
                    return
                self.seen_event_ids[ev_id] = None
                self.last_event_id = str(ev_id)
                if len(self.seen_event_ids) > 1024:
                    self.seen_event_ids.popitem(last=False)

        ev_name = env.get("event")
        payload = env.get("payload") or {}
        if ev_name == "context.push" and isinstance(payload, dict):
            self.next_allowed_pull_ms = 0
            self.on_context_received(ProjectedContext.from_dict(payload))
        elif ev_name == "capabilities.updated" and isinstance(payload, dict):
            cap_digest = payload.get("capability_table_digest")
            raw_table = payload.get("capability_table")
            slot_id = payload.get("slot_id")
            if isinstance(cap_digest, str) and isinstance(raw_table, list):
                caps = [CapabilityEntry.from_dict(item) for item in raw_table]
                caps_sorted = sorted(caps, key=lambda c: c.method_id)
                verified = jcs_sha256("g2a.cap.v1", [c.to_dict() for c in caps_sorted])
                if verified == cap_digest:
                    self.capability_cache[cap_digest] = caps_sorted
                    if isinstance(slot_id, str) and slot_id:
                        self.slot_digest_index[slot_id] = cap_digest
                    if self.scope is not None:
                        self.scope.capability_table_digest = cap_digest
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
        self._goal_wakeup.set()

    # --- Transport Internals ---

    def _record_receipt(self, receipt: QueueReceipt) -> None:
        with self._state_lock:
            self.completed_receipts.append(receipt)
            if len(self.completed_receipts) > 256:
                del self.completed_receipts[:-256]
            if receipt.latest_context is not None:
                self.latest_context = receipt.latest_context
        if self.on_receipt_callback:
            self.on_receipt_callback(receipt)

    def _get_keepalive_conn(self, host: str, port: int, is_https: bool) -> http.client.HTTPConnection:
        key = (host, port, is_https)
        cached = getattr(self._http_tls, "entry", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        if cached is not None:
            try:
                cached[1].close()
            except Exception:
                pass
        if is_https:
            ctx = self.ssl_context or ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=self.request_timeout_sec, context=ctx
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.request_timeout_sec)
        with self._pool_lock:
            self._pooled_conns.append(conn)
        self._http_tls.entry = (key, conn)
        return conn

    def _reset_keepalive_conn(self) -> None:
        cached = getattr(self._http_tls, "entry", None)
        if cached is not None:
            try:
                cached[1].close()
            except Exception:
                pass
            with self._pool_lock:
                if cached[1] in self._pooled_conns:
                    self._pooled_conns.remove(cached[1])
            self._http_tls.entry = None

    def _close_all_keepalive_conns(self) -> None:
        with self._pool_lock:
            conns = list(self._pooled_conns)
            self._pooled_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._http_tls.entry = None

    def _post_envelope(
        self,
        operation: str,
        payload: Dict[str, Any],
        session_id: Optional[str],
        extensions: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self._custom_post_fn is not None:
            return self._custom_post_fn(self.hub_endpoint, operation, payload, self.auth_token, session_id)

        parsed_url = urllib.parse.urlparse(f"{self.hub_endpoint}/g2a/v1/core")
        is_https = parsed_url.scheme == "https"
        host = parsed_url.hostname or "127.0.0.1"
        port = parsed_url.port or (443 if is_https else 80)
        path = parsed_url.path or "/g2a/v1/core"
        if parsed_url.query:
            path = f"{path}?{parsed_url.query}"

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
            "Connection": "keep-alive",
        }
        for attempt in range(2):
            conn = self._get_keepalive_conn(host, port, is_https)
            try:
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                if raw:
                    parsed = parse_strict_json(raw)
                    return self._unwrap_reply(parsed)
                if status == 504:
                    raise G2AError("outcome_unknown", 504, "Gateway timeout")
                raise G2AError("unavailable", status, f"HTTP {status}")
            except G2AError:
                raise
            except (TimeoutError, socket.timeout) as tex:
                self._reset_keepalive_conn()
                raise G2AError("outcome_unknown", 504, f"Request timed out: {tex}") from tex
            except (http.client.RemoteDisconnected, http.client.CannotSendRequest, ConnectionResetError, BrokenPipeError) as rex:
                self._reset_keepalive_conn()
                if attempt == 0:
                    continue
                raise G2AError("unavailable", 503, f"Hub connection closed: {rex}") from rex
            except Exception as ex:
                self._reset_keepalive_conn()
                raise G2AError("unavailable", 503, f"Hub connection failed: {ex}") from ex
        raise G2AError("unavailable", 503, "Unreachable")

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
            backoff_attempt = 0
            while not self._stop_event.is_set() and not self.closed and self.session_id:
                replay_param = "" if first_connect else "&replay=1"
                cursor_id = self.last_event_id if not first_connect else ""
                if cursor_id:
                    replay_param += f"&last_event_id={urllib.parse.quote(cursor_id)}"
                stream_url = f"{self.hub_endpoint}/g2a/v1/core/events?session_id={self.session_id}{replay_param}"
                sse_headers = {
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {self.auth_token}",
                }
                if cursor_id:
                    sse_headers["Last-Event-ID"] = cursor_id
                req = urllib.request.Request(
                    stream_url,
                    method="GET",
                    headers=sse_headers,
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
                        backoff_attempt = 0
                        self._stream_ready.set()
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
                    delay = min(0.15 * (2 ** min(backoff_attempt, 5)), 4.0) * (0.85 + 0.3 * random.random())
                    backoff_attempt += 1
                    self._stop_event.wait(delay)
                except Exception:
                    if self._stop_event.is_set() or self.closed:
                        return
                    delay = min(0.15 * (2 ** min(backoff_attempt, 5)), 4.0) * (0.85 + 0.3 * random.random())
                    backoff_attempt += 1
                    self._stop_event.wait(delay)

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
            backoff_attempt = 0
            while not self._stop_event.is_set() and not self.closed and self.session_id:
                parsed = urllib.parse.urlparse(self.hub_endpoint)
                is_tls = parsed.scheme in ("https", "wss")
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port or (443 if is_tls else 80)
                base_path = parsed.path.rstrip("/")
                replay_param = "" if first_connect else "&replay=1"
                if not first_connect and self.last_event_id:
                    replay_param += f"&last_event_id={urllib.parse.quote(self.last_event_id)}"
                ws_path = f"{base_path}/g2a/v1/core/ws?session_id={urllib.parse.quote(self.session_id)}{replay_param}"

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
                        if len(resp_head) > 16384:
                            raise ConnectionError("Oversized WebSocket handshake headers")
                        chunk = raw_sock.recv(1)
                        if not chunk:
                            raise EOFError("Handshake closed")
                        resp_head += chunk
                    header_lines = resp_head.decode("latin1").split("\r\n")
                    status_line = header_lines[0]
                    if " 101 " not in status_line:
                        if " 401 " in status_line or " 403 " in status_line:
                            self.shutdown_session_cleanly(notify_hub=False)
                            return
                        raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")

                    expected_accept = base64.b64encode(
                        hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                    ).decode("ascii")
                    actual_accept = ""
                    for hline in header_lines[1:]:
                        if ":" in hline:
                            hk, hv = hline.split(":", 1)
                            if hk.strip().lower() == "sec-websocket-accept":
                                actual_accept = hv.strip()
                                break
                    if actual_accept != expected_accept:
                        raise ConnectionError("Invalid RFC 6455 Sec-WebSocket-Accept header")

                    backoff_attempt = 0
                    self._stream_ready.set()
                    while not self._stop_event.is_set() and not self.closed:
                        b0, b1 = _recv_exact(raw_sock, 2)
                        opcode = b0 & 0x0F
                        masked = (b1 & 0x80) != 0
                        length = b1 & 0x7F
                        if length == 126:
                            length = struct.unpack("!H", _recv_exact(raw_sock, 2))[0]
                        elif length == 127:
                            length = struct.unpack("!Q", _recv_exact(raw_sock, 8))[0]
                        if length > (4 << 20):
                            _send_masked_frame(raw_sock, 0x8)
                            raise ConnectionError("WebSocket frame exceeds 4 MiB limit")
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
                    delay = min(0.15 * (2 ** min(backoff_attempt, 5)), 4.0) * (0.85 + 0.3 * random.random())
                    backoff_attempt += 1
                    self._stop_event.wait(delay)
                finally:
                    if raw_sock is not None:
                        try:
                            raw_sock.close()
                        except Exception:
                            pass

        self._sse_thread = threading.Thread(target=_ws_worker, name="g2a-core-ws", daemon=True)
        self._sse_thread.start()
