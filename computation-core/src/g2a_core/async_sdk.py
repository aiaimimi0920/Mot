"""Zero-dependency native asyncio Computation Core SDK (G2A V0.0.1)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import os
import random
import ssl
import struct
import time
import urllib.parse
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .agent_runtime import GoalExecutionReport
from .core_sdk import (
    CognitivePlannerPort,
    CoreMetrics,
    CoreSDK,
    QueueBuilder,
    discover_local_hub_manifest,
    generate_id,
)
from .jcs import jcs_sha256, parse_strict_json
from .memory import InMemoryCompanionStore, MemoryRecord, SpoilerGuard
from .schemas import (
    CapabilityEntry,
    FrozenScope,
    G2AError,
    MethodCallQueue,
    PlanDecision,
    ProjectedContext,
    QueueReceipt,
)
from .ssrf import validate_outbound_url_against_ssrf

AsyncPlannerCallable = Callable[
    [List[CapabilityEntry], ProjectedContext, List[MemoryRecord]],
    Union[PlanDecision, Awaitable[PlanDecision]],
]


class AsyncQueueBuilder:
    """Async fluent wrapper around `QueueBuilder` performing zero-latency local pre-flight checks before async submission."""

    def __init__(
        self,
        async_sdk: "AsyncCoreSDK",
        context: ProjectedContext,
        intent_summary: str = "Execute planned companion queue",
        trigger: Optional[str] = None,
        supersedes: Optional[bool] = None,
    ) -> None:
        self._async_sdk = async_sdk
        self._inner = QueueBuilder(
            async_sdk._sync_delegate,
            context,
            intent_summary=intent_summary,
            trigger=trigger,
            supersedes=supersedes,
        )

    def add_step(
        self,
        method_id: str,
        actor: str,
        args: Optional[Dict[str, Any]] = None,
        validate: bool = True,
        allow_initial_staged: bool = False,
    ) -> "AsyncQueueBuilder":
        self._inner.add_step(method_id, actor, args=args, validate=validate, allow_initial_staged=allow_initial_staged)
        return self

    def add_step_auto(
        self,
        method_id: str,
        actor: str,
        args: Optional[Dict[str, Any]] = None,
        validate: bool = True,
    ) -> "AsyncQueueBuilder":
        self._inner.add_step_auto(method_id, actor, args=args, validate=validate)
        return self

    def build(self, queue_id: Optional[str] = None) -> MethodCallQueue:
        return self._inner.build(queue_id=queue_id)

    async def submit(self, queue_id: Optional[str] = None) -> Optional[QueueReceipt]:
        return await self._async_sdk.submit_queue_with_standard_recovery(self.build(queue_id=queue_id))


class AsyncCoreSDK:
    """
    Native asyncio Tier 3 Computation Core SDK for FastAPI, LangGraph, MCP, and async AI agent runtimes.
    Uses `asyncio.open_connection` for non-blocking RFC 6455 WebSocket push streams (`ws://` & TLS 1.3 `wss://`)
    with `Last-Event-ID` cursor replay, jittered exponential reconnect backoff, async planner coroutine support,
    and automatic `504 outcome_unknown -> queue.query` idempotency reconciliation.
    """

    def __init__(
        self,
        planner: Union[CognitivePlannerPort, AsyncPlannerCallable, None] = None,
        memory_store: Optional[InMemoryCompanionStore] = None,
        allow_loopback: bool = False,
        request_timeout_sec: float = 8.0,
        heartbeat_interval_sec: float = 5.0,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self._planner = planner
        self._sync_delegate = CoreSDK(
            planner=type("_Idle", (), {"plan": staticmethod(lambda c, x, m: PlanDecision())})(),
            memory_store=memory_store,
            allow_loopback=allow_loopback,
            request_timeout_sec=request_timeout_sec,
            heartbeat_interval_sec=heartbeat_interval_sec,
            ssl_context=ssl_context,
        )
        self._hb_task: Optional[asyncio.Task[None]] = None
        self._ws_task: Optional[asyncio.Task[None]] = None
        self._stream_ready = asyncio.Event()

    @property
    def session_id(self) -> Optional[str]:
        return self._sync_delegate.session_id

    @property
    def scope(self) -> Optional[FrozenScope]:
        return self._sync_delegate.scope

    @property
    def closed(self) -> bool:
        return self._sync_delegate.closed

    @property
    def metrics(self) -> CoreMetrics:
        return self._sync_delegate.metrics

    @classmethod
    async def connect_quick(
        cls,
        slot_id: str,
        auth_token: str,
        planner: Union[CognitivePlannerPort, AsyncPlannerCallable, None] = None,
        hub_endpoint: str = "auto",
        fallback_url: Optional[str] = None,
        runtime_file: Optional[str] = None,
        allow_loopback: bool = True,
        memory_store: Optional[InMemoryCompanionStore] = None,
        **connect_kwargs: Any,
    ) -> "AsyncCoreSDK":
        resolved = hub_endpoint
        if hub_endpoint == "auto":
            try:
                manifest = await asyncio.to_thread(discover_local_hub_manifest, runtime_file)
                resolved = str(manifest["core_url"])
            except Exception:
                if not fallback_url:
                    raise
                resolved = fallback_url
        sdk = cls(planner=planner, memory_store=memory_store, allow_loopback=allow_loopback)
        await sdk.connect(resolved, auth_token, slot_id, **connect_kwargs)
        return sdk

    async def __aenter__(self) -> "AsyncCoreSDK":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close(notify_hub=True)

    async def connect(
        self,
        hub_endpoint: str,
        auth_token: str,
        slot_id: str,
        start_background_loops: bool = True,
        **kwargs: Any,
    ) -> FrozenScope:
        validate_outbound_url_against_ssrf(hub_endpoint, allow_loopback=self._sync_delegate.allow_loopback)
        scope = await asyncio.to_thread(
            self._sync_delegate.connect,
            hub_endpoint,
            auth_token,
            slot_id,
            start_background_loops=False,
            **kwargs,
        )
        if start_background_loops:
            self._stream_ready.clear()
            self._hb_task = asyncio.create_task(self._heartbeat_loop())
            self._ws_task = asyncio.create_task(self._ws_stream_loop())
            try:
                await asyncio.wait_for(self._stream_ready.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        return scope

    async def close(self, notify_hub: bool = True) -> None:
        if self._sync_delegate.closed:
            return
        self._sync_delegate.closed = True
        for t in (self._hb_task, self._ws_task):
            if t is not None and not t.done():
                t.cancel()
        for t in (self._hb_task, self._ws_task):
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if notify_hub and self._sync_delegate.session_id:
            try:
                await asyncio.to_thread(
                    self._sync_delegate._post_envelope,
                    "session.close",
                    {"reason": "client_shutdown"},
                    self._sync_delegate.session_id,
                )
            except Exception:
                pass
        await asyncio.to_thread(self._sync_delegate._close_all_keepalive_conns)

    def get_cached_capabilities(self, slot_id: Optional[str] = None) -> List[CapabilityEntry]:
        return self._sync_delegate.get_cached_capabilities(slot_id=slot_id)

    async def pull_context(self) -> ProjectedContext:
        return await asyncio.to_thread(self._sync_delegate.pull_context)

    async def pull_context_if_due(self) -> Optional[ProjectedContext]:
        return await asyncio.to_thread(self._sync_delegate.pull_context_if_due)

    async def query_queue(self, queue_id: str) -> QueueReceipt:
        return await asyncio.to_thread(self._sync_delegate.query_queue, queue_id)

    async def cancel_queue(self, queue_id: str) -> QueueReceipt:
        return await asyncio.to_thread(self._sync_delegate.cancel_queue, queue_id)

    async def send_chat(self, text: str, audience: str = "player_only") -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync_delegate.send_chat, text, audience)

    async def resume_session(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync_delegate.resume_session)

    async def send_heartbeat(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync_delegate.send_heartbeat)

    def build_queue(
        self,
        context: ProjectedContext,
        intent_summary: str = "Execute planned companion queue",
        trigger: Optional[str] = None,
        supersedes: Optional[bool] = None,
    ) -> AsyncQueueBuilder:
        return AsyncQueueBuilder(self, context, intent_summary=intent_summary, trigger=trigger, supersedes=supersedes)

    async def execute_goal(
        self,
        objective: str,
        success_when: Optional[Callable[[ProjectedContext, Optional[QueueReceipt]], bool]] = None,
        max_turns: int = 6,
        timeout_sec: float = 10.0,
    ) -> GoalExecutionReport:
        """Async closed-loop goal orchestrator with step-failure feedback, sustained-step awaiting, and cycle guard."""
        d = self._sync_delegate
        goal = d.goal_tracker.start_goal(objective, max_turns=max_turns)
        deadline = time.monotonic() + timeout_sec
        last_receipt: Optional[QueueReceipt] = None
        last_ctx: Optional[ProjectedContext] = None
        last_planned_rev = -1
        d._in_execute_goal = True

        async def _pull_fresh_async() -> Optional[ProjectedContext]:
            p = await self.pull_context_if_due()
            if p is not None:
                return p
            now_ms = int(time.time() * 1000)
            if d.next_allowed_pull_ms > now_ms and time.monotonic() < deadline:
                wait_s = min((d.next_allowed_pull_ms - now_ms) / 1000.0 + 0.015, max(0.01, deadline - time.monotonic()))
                await asyncio.sleep(wait_s)
                return await self.pull_context_if_due()
            return None

        try:
            while time.monotonic() < deadline and not d.closed and not d.actions_paused:
                pulled = await _pull_fresh_async()
                ctx = pulled or d.latest_context
                if ctx is None:
                    ctx = await self.pull_context()
                last_ctx = ctx

                if success_when is not None and success_when(ctx, last_receipt):
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

                if (
                    (last_receipt is not None and last_receipt.queue_state in ("running", "executing") and ctx.rev == last_planned_rev)
                    or bool(d.pending_confirm_queues)
                ):
                    await asyncio.sleep(0.04)
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
                caps = d.capability_cache.get(synth_ctx.capability_table_digest, [])
                allowed_tags = set(synth_ctx.allowed_timeline_tags) if synth_ctx.allowed_timeline_tags is not None else None
                memories = (
                    d.memory_store.recall_safe(
                        d.scope.player_id,
                        SpoilerGuard(
                            current_epoch=synth_ctx.epoch,
                            game_origin_id=d.scope.game_instance_id,
                            allowed_timeline_tags=allowed_tags,
                            query_hint=objective,
                        ),
                    )
                    if d.scope is not None
                    else []
                )
                decision = await self._invoke_planner(caps, synth_ctx, memories)
                if decision.reply_text and d.scope is not None and d.scope.allow_chat:
                    await self.send_chat(decision.reply_text, audience=decision.audience)

                filtered = []
                for s in decision.planned_steps:
                    if d.goal_tracker.is_failed_repeat(synth_ctx.state_brief, s.method_id, s.actor, s.args):
                        break
                    filtered.append(type(s)(len(filtered), s.method_id, s.actor, s.args))

                if not filtered:
                    st = "blocked" if decision.goal_status == "blocked" else "no_executable_steps"
                    return GoalExecutionReport(
                        goal_id=goal.goal_id,
                        objective=objective,
                        succeeded=False,
                        status=st,
                        turns_used=goal.turns_used,
                        submitted_queues=list(goal.submitted_queues),
                        outcomes=list(goal.recent_outcomes),
                        final_context=ctx,
                        failure_reason=goal.last_failure_reason or st,
                    )

                goal.turns_used += 1
                q = MethodCallQueue(
                    queue_id=generate_id("Q"),
                    epoch=synth_ctx.epoch,
                    based_on_rev=synth_ctx.rev,
                    trigger="player_command",
                    supersedes=(goal.turns_used == 1),
                    intent_summary=decision.intent_summary or objective,
                    steps=filtered,
                )
                submitted_tuples = [(s.method_id, s.actor, s.args) for s in filtered]
                last_receipt = await self.submit_queue_with_standard_recovery(q)
                if last_receipt is not None:
                    d.goal_tracker.record_receipt(last_receipt, submitted_tuples, synth_ctx)
                    if last_receipt.latest_context is not None:
                        last_ctx = last_receipt.latest_context

                fresh_after = await _pull_fresh_async()
                if fresh_after is not None:
                    last_ctx = fresh_after
                elif d.latest_context is not None and d.latest_context.rev > synth_ctx.rev:
                    last_ctx = d.latest_context

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
                await asyncio.sleep(0.03)

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
            d._in_execute_goal = False
            d.goal_tracker.clear_goal()

    async def _invoke_planner(
        self,
        caps: List[CapabilityEntry],
        ctx: ProjectedContext,
        memories: List[MemoryRecord],
    ) -> PlanDecision:
        if self._planner is None:
            return PlanDecision()
        d = self._sync_delegate
        if hasattr(self._planner, "plan_async"):
            return await self._planner.plan_async(  # type: ignore[union-attr]
                caps,
                ctx,
                memories,
                active_goal=d.goal_tracker.active_goal,
                recent_outcomes=d.goal_tracker.recent_outcomes,
            )
        target_fn = self._planner.plan if hasattr(self._planner, "plan") else self._planner
        try:
            sig = inspect.signature(target_fn)  # type: ignore[arg-type]
            if "active_goal" in sig.parameters or "recent_outcomes" in sig.parameters:
                res = target_fn(  # type: ignore[operator]
                    caps,
                    ctx,
                    memories,
                    active_goal=d.goal_tracker.active_goal,
                    recent_outcomes=d.goal_tracker.recent_outcomes,
                )
            else:
                res = target_fn(caps, ctx, memories)  # type: ignore[operator]
        except (TypeError, ValueError):
            res = target_fn(caps, ctx, memories)  # type: ignore[operator]
        if inspect.isawaitable(res):
            return await res
        return res

    async def on_context_received(self, ctx: ProjectedContext, _replan_depth: int = 0) -> Optional[QueueReceipt]:
        d = self._sync_delegate
        if d.closed or d.actions_paused or not d.session_id or not d.scope:
            return None
        d.metrics.contexts_received += 1
        d.latest_context = ctx
        caps = d.capability_cache.get(ctx.capability_table_digest, [])
        allowed_tags = set(ctx.allowed_timeline_tags) if ctx.allowed_timeline_tags is not None else None
        memories = d.memory_store.recall_safe(
            d.scope.player_id,
            SpoilerGuard(
                current_epoch=ctx.epoch,
                game_origin_id=d.scope.game_instance_id,
                allowed_timeline_tags=allowed_tags,
                query_hint=ctx.player_command,
            ),
        )
        decision = await self._invoke_planner(caps, ctx, memories)
        if decision.reply_text and d.scope.allow_chat:
            await self.send_chat(decision.reply_text, audience=decision.audience)
        if decision.planned_steps:
            is_cmd = ctx.player_command is not None
            q = MethodCallQueue(
                queue_id=generate_id("Q"),
                epoch=ctx.epoch,
                based_on_rev=ctx.rev,
                trigger="player_command" if is_cmd else "proactive",
                supersedes=is_cmd,
                intent_summary=decision.intent_summary or "Execute planned companion queue",
                steps=decision.planned_steps,
            )
            return await self.submit_queue_with_standard_recovery(q, _replan_depth=_replan_depth)
        return None

    async def submit_queue_with_standard_recovery(
        self,
        queue: MethodCallQueue,
        _replan_depth: int = 0,
    ) -> Optional[QueueReceipt]:
        d = self._sync_delegate
        if d.closed or not d.session_id:
            return None
        now_ms = int(time.time() * 1000)
        with d._state_lock:
            for k in [k for k, until in d.refusal_cooldowns.items() if now_ms >= until]:
                d.refusal_cooldowns.pop(k, None)
            if queue.trigger == "proactive":
                fp = jcs_sha256("g2a.fp.v1", [s.to_dict() for s in queue.steps])
                if now_ms < d.refusal_cooldowns.get(fp, 0):
                    d.metrics.cooldown_shield_hits += 1
                    return None
        try:
            d.metrics.queues_submitted += 1
            res = await asyncio.to_thread(d._post_envelope, "queue.submit", queue.to_dict(), d.session_id)
            receipt = QueueReceipt.from_dict(res)
            d._record_receipt(receipt)
            return receipt
        except G2AError as err:
            if err.code == "confirmation_required":
                resubmit_now = False
                with d._state_lock:
                    if queue.queue_id in d._early_confirmed_queues:
                        d._early_confirmed_queues.pop(queue.queue_id, None)
                        resubmit_now = True
                    elif queue.queue_id in d._early_declined_queues:
                        cd_ms = d._early_declined_queues.pop(queue.queue_id)
                        fp = jcs_sha256("g2a.fp.v1", [s.to_dict() for s in queue.steps])
                        d.refusal_cooldowns[fp] = int(time.time() * 1000) + cd_ms
                    else:
                        d.pending_confirm_queues[queue.queue_id] = queue
                if resubmit_now:
                    return await self.submit_queue_with_standard_recovery(queue, _replan_depth=_replan_depth)
                return None
            if err.code == "stale_context" and err.fresh_context is not None and _replan_depth < 2:
                return await self.on_context_received(err.fresh_context, _replan_depth=_replan_depth + 1)
            if err.code == "outcome_unknown":
                d.metrics.reconciled_unknowns += 1
                return await asyncio.to_thread(d.reconcile_unknown_queue, queue.queue_id)
            return None

    async def _handle_push_envelope(self, env: Dict[str, Any]) -> None:
        d = self._sync_delegate
        ev_id = env.get("event_id")
        if ev_id:
            with d._state_lock:
                if ev_id in d.seen_event_ids:
                    return
                d.seen_event_ids[ev_id] = None
                d.last_event_id = str(ev_id)
                if len(d.seen_event_ids) > 1024:
                    d.seen_event_ids.popitem(last=False)

        ev_name = env.get("event")
        payload = env.get("payload") or {}
        if ev_name == "context.push" and isinstance(payload, dict):
            d.next_allowed_pull_ms = 0
            await self.on_context_received(ProjectedContext.from_dict(payload))
        elif ev_name == "session.resumed" and isinstance(payload, dict):
            d.actions_paused = False
            await self.on_context_received(ProjectedContext.from_dict(payload))
        elif ev_name == "session.closed":
            await self.close(notify_hub=False)
        else:
            await asyncio.to_thread(d.handle_event_envelope, {"event": ev_name, "payload": payload})

    async def _heartbeat_loop(self) -> None:
        d = self._sync_delegate
        while not d.closed and d.session_id:
            await asyncio.sleep(d.heartbeat_interval_sec)
            if d.closed or not d.session_id:
                return
            try:
                await self.send_heartbeat()
            except G2AError as err:
                if err.code in ("revoked", "permission_denied"):
                    await self.close(notify_hub=False)
                    return

    async def _ws_stream_loop(self) -> None:
        d = self._sync_delegate
        first_connect = True
        attempt = 0
        while not d.closed and d.session_id:
            parsed = urllib.parse.urlparse(d.hub_endpoint)
            is_tls = parsed.scheme in ("https", "wss")
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if is_tls else 80)
            base_path = parsed.path.rstrip("/")
            replay_param = "" if first_connect else "&replay=1"
            if not first_connect and d.last_event_id:
                replay_param += f"&last_event_id={urllib.parse.quote(d.last_event_id)}"
            ws_path = f"{base_path}/g2a/v1/core/ws?session_id={urllib.parse.quote(d.session_id)}{replay_param}"

            writer: Optional[asyncio.StreamWriter] = None
            try:
                if not first_connect:
                    d.metrics.stream_reconnects += 1
                    try:
                        await self.resume_session()
                    except G2AError as gerr:
                        if gerr.code in ("revoked", "permission_denied"):
                            await self.close(notify_hub=False)
                            return
                first_connect = False

                ssl_ctx = (d.ssl_context or ssl.create_default_context()) if is_tls else None
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ssl_ctx, server_hostname=host if is_tls else None),
                    timeout=10.0,
                )
                ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
                req_head = (
                    f"GET {ws_path} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {ws_key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    f"Authorization: Bearer {d.auth_token}\r\n\r\n"
                )
                writer.write(req_head.encode("ascii"))
                await writer.drain()

                status_line = (await reader.readline()).decode("latin1").rstrip("\r\n")
                if " 101 " not in status_line:
                    if " 401 " in status_line or " 403 " in status_line:
                        await self.close(notify_hub=False)
                        return
                    raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")

                expected_accept = base64.b64encode(
                    hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                actual_accept = ""
                while True:
                    hl = (await reader.readline()).decode("latin1").rstrip("\r\n")
                    if not hl:
                        break
                    if ":" in hl:
                        hk, hv = hl.split(":", 1)
                        if hk.strip().lower() == "sec-websocket-accept":
                            actual_accept = hv.strip()
                if actual_accept != expected_accept:
                    raise ConnectionError("Invalid RFC 6455 Sec-WebSocket-Accept header")

                attempt = 0
                self._stream_ready.set()
                while not d.closed:
                    hdr = await reader.readexactly(2)
                    opcode = hdr[0] & 0x0F
                    masked = (hdr[1] & 0x80) != 0
                    length = hdr[1] & 0x7F
                    if length == 126:
                        length = struct.unpack("!H", await reader.readexactly(2))[0]
                    elif length == 127:
                        length = struct.unpack("!Q", await reader.readexactly(8))[0]
                    if length > (4 << 20):
                        raise ConnectionError("WebSocket frame exceeds 4 MiB limit")
                    mask_key = await reader.readexactly(4) if masked else b""
                    data = await reader.readexactly(length) if length > 0 else b""
                    if masked:
                        data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
                    if opcode == 0x8:
                        await _send_ws_masked_control(writer, 0x8, b"")
                        break
                    if opcode == 0x9:
                        await _send_ws_masked_control(writer, 0xA, data[:125])
                    elif opcode == 0x1:
                        await self._handle_push_envelope(parse_strict_json(data))
            except asyncio.CancelledError:
                raise
            except Exception:
                if d.closed:
                    return
                delay = min(0.15 * (2 ** min(attempt, 5)), 4.0) * (0.85 + 0.3 * random.random())
                attempt += 1
                await asyncio.sleep(delay)
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass


async def _send_ws_masked_control(writer: asyncio.StreamWriter, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    writer.write(bytes([0x80 | (opcode & 0x0F), 0x80 | len(payload)]) + mask + masked)
    await writer.drain()
