"""Zero-dependency native asyncio Interaction Origin SDK (G2A V0.0.1)."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .jcs import StrictJSONError, parse_strict_json
from .origin_sdk import (
    G2A_WIRE_VERSION,
    ActiveMethodGrant,
    CapabilityEntry,
    EffectInvocationResult,
    G2AOriginError,
    ImmediateFailed,
    ImmediateSucceeded,
    LoopbackHTTPTransportClient,
    OriginExecuteReply,
    OriginExecuteRequest,
    OriginMetrics,
    OriginSDK,
    SpatialAdapterPort,
    StartedExecuting,
    StepExecutionToken,
    _validate_args_schema,
    discover_local_hub_origin_url,
    infer_params_schema_from_callable,
)


class AsyncOriginSDK:
    """
    Native asyncio Tier 1 Interaction Origin SDK for async Python game loops and simulations.
    Supports both `async def` coroutine handlers and synchronous handlers via `@sdk.capability(...)`,
    persistent HTTP/1.1 Keep-Alive loopback `origin.execute` server via `asyncio.start_server`,
    and deterministic `async with await AsyncOriginSDK.connect_quick(...) as sdk:` lifecycle.
    """

    def __init__(
        self,
        slot_id: str,
        hub_origin_url: str = "http://127.0.0.1:19210",
        slot_role_kind: str = "companion",
        billing_mode: str = "player_metered",
        player_id: str = "player-local",
        game_instance_id: str = "game-local",
        room_id: str = "",
        faction_id: str = "",
        spatial: Optional[SpatialAdapterPort] = None,
        perception_radius: float = 15.0,
        chat_ui_handler: Optional[Callable[[str, str, bool], Any]] = None,
        shared_token: str = "",
    ) -> None:
        self._tx = LoopbackHTTPTransportClient(hub_origin_url, shared_token=shared_token)
        self._core = OriginSDK(
            slot_id=slot_id,
            hub_transport=self._tx,
            slot_role_kind=slot_role_kind,
            billing_mode=billing_mode,
            player_id=player_id,
            game_instance_id=game_instance_id,
            room_id=room_id,
            faction_id=faction_id,
            spatial=spatial,
            perception_radius=perception_radius,
            chat_ui_handler=chat_ui_handler,
            shared_token=shared_token,
        )
        self._async_handlers: Dict[str, Callable[..., Awaitable[EffectInvocationResult]]] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._closed = False

    @property
    def slot_id(self) -> str:
        return self._core.slot_id

    @property
    def metrics(self) -> OriginMetrics:
        return self._core.metrics

    @property
    def origin_execute_url(self) -> str:
        return self._core.origin_execute_url

    @classmethod
    async def connect_quick(
        cls,
        slot_id: str,
        hub_origin_url: str = "auto",
        fallback_url: str = "http://127.0.0.1:19210",
        actors: Sequence[str] = ("companion", "player"),
        runtime_file: Optional[str] = None,
        shared_token: str = "",
        setup_fn: Optional[Callable[["AsyncOriginSDK"], Union[None, Awaitable[None]]]] = None,
        publish: bool = True,
        **kwargs: Any,
    ) -> "AsyncOriginSDK":
        resolved = hub_origin_url
        if hub_origin_url == "auto":
            try:
                resolved = await asyncio.to_thread(discover_local_hub_origin_url, runtime_file)
            except Exception:
                resolved = fallback_url
        sdk = cls(slot_id=slot_id, hub_origin_url=resolved, shared_token=shared_token, **kwargs)
        for act in actors:
            if sdk._core.slot_role_kind == "independent_npc" and act == "player":
                continue
            sdk.bind_actor_subject(act, {})
        await sdk.start_loopback_execute_listener()
        if setup_fn is not None:
            maybe_coro = setup_fn(sdk)
            if inspect.isawaitable(maybe_coro):
                await maybe_coro
        if publish:
            await sdk.publish_capabilities_and_baseline()
        return sdk

    async def __aenter__(self) -> "AsyncOriginSDK":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close(notify_offline=True)

    async def close(self, notify_offline: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._core._cancel_all_in_flight("sdk_closed")
        if notify_offline:
            try:
                await self.report_player_presence(False, 0)
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await asyncio.to_thread(self._tx.close)

    def bind_actor_subject(self, actor: str, instance: Any) -> None:
        self._core.bind_actor_subject(actor, instance)

    def bind_state_module(
        self,
        module_name: str,
        snapshot_fn: Callable[[], Dict[str, Any]],
        active_methods_fn: Callable[[], List[ActiveMethodGrant]],
    ) -> None:
        self._core.bind_state_module(module_name, snapshot_fn, active_methods_fn)

    def attach_visual_frame(self, visual_ref: Dict[str, Any]) -> None:
        self._core.attach_visual_frame(visual_ref, flush=False)

    def attach_audio_segment(self, audio_ref: Dict[str, Any]) -> None:
        self._core.attach_audio_segment(audio_ref, flush=False)

    def set_allowed_timeline_tags(self, tags: List[str]) -> None:
        self._core.set_allowed_timeline_tags(tags, flush=False)

    def attach_extension_data(self, extension_name: str, data: Dict[str, Any]) -> None:
        self._core.attach_extension_data(extension_name, data, flush=False)

    def capability(
        self,
        method_id: str,
        name: Optional[str] = None,
        description: str = "",
        permitted_actors: Sequence[str] = ("companion",),
        sustained: bool = False,
        risk_flags: Sequence[str] = (),
        default_policy: str = "allow",
        params_schema: Optional[Dict[str, Any]] = None,
        readiness_check: Optional[Callable[[str, Any], Optional[str]]] = None,
        spatial_target_verb: bool = False,
        navigation_verb: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Registers either an `async def` coroutine or a regular `def` function as a G2A capability."""

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            sig = inspect.signature(fn)
            param_names = list(sig.parameters.keys())
            is_raw_four_arg = param_names == ["actor", "subject", "args", "token"]
            schema = (
                dict(params_schema)
                if params_schema is not None
                else ({"type": "object"} if is_raw_four_arg else infer_params_schema_from_callable(fn))
            )
            cap_def = CapabilityEntry(
                method_id=method_id,
                name=name or fn.__name__,
                description=description or (inspect.getdoc(fn) or fn.__name__),
                permitted_actors=list(permitted_actors),
                params_schema=schema,
                sustained=sustained,
                risk_flags=list(risk_flags),
                default_policy=default_policy,
            )

            async def _async_invoke(
                actor: str,
                subject: Any,
                step_args: Dict[str, Any],
                token: StepExecutionToken,
            ) -> EffectInvocationResult:
                if is_raw_four_arg:
                    res = fn(actor, subject, step_args, token)
                else:
                    call_kwargs: Dict[str, Any] = {}
                    accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                    for pname in param_names:
                        if pname == "actor":
                            call_kwargs["actor"] = actor
                        elif pname == "subject":
                            call_kwargs["subject"] = subject
                        elif pname == "token":
                            call_kwargs["token"] = token
                        elif pname in step_args:
                            call_kwargs[pname] = step_args[pname]
                    if accepts_varkw:
                        for k, v in step_args.items():
                            call_kwargs.setdefault(k, v)
                    res = fn(**call_kwargs)
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, (ImmediateSucceeded, ImmediateFailed, StartedExecuting)):
                    return res
                if res is None:
                    return ImmediateSucceeded({})
                if isinstance(res, dict):
                    return ImmediateSucceeded(res)
                return ImmediateSucceeded({"value": res})

            self._async_handlers[method_id] = _async_invoke
            self._core.register_capability(
                cap_def,
                effect_handler=lambda a, s, args, tok: ImmediateFailed("use_async_handle_origin_execute"),
                readiness_check=readiness_check,
                spatial_target_verb=spatial_target_verb,
                navigation_verb=navigation_verb,
            )
            return fn

        return _decorator

    async def handle_origin_execute(self, req: OriginExecuteRequest) -> OriginExecuteReply:
        if req.kind == "cancel":
            return await asyncio.to_thread(self._core.cancel_in_flight_step, req.execution_id)
        if req.kind == "deliver_chat":
            aud = str(req.args.get("audience", "player_only"))
            txt = str(req.args.get("text", ""))
            cb_res = self._core.chat_ui_handler(aud, txt, True)
            if inspect.isawaitable(cb_res):
                await cb_res
            return OriginExecuteReply(req.execution_id, "succeeded")

        if req.execution_id in self._core.execution_ledger:
            return self._core.execution_ledger[req.execution_id]
        if req.epoch != self._core.current_epoch:
            raise G2AOriginError("stale_context", 409, f"Epoch mismatch: {req.epoch} != {self._core.current_epoch}")
        if not req.method_id or req.method_id not in self._core.capabilities:
            raise G2AOriginError("invalid_arguments", 400, f"Unknown method_id {req.method_id!r}")
        reg = self._core.capabilities[req.method_id]
        if not req.actor or req.actor not in reg.definition.permitted_actors:
            raise G2AOriginError("permission_denied", 403, f"Actor {req.actor!r} not permitted")
        if req.actor not in self._core.actors:
            raise G2AOriginError("not_found", 404, f"Actor {req.actor!r} not bound")

        _validate_args_schema(req.args, reg.definition.params_schema)
        token = StepExecutionToken(execution_id=req.execution_id, _sdk=self._core)
        self._core.metrics.steps_executed += 1

        async_fn = self._async_handlers.get(req.method_id)
        if async_fn is not None:
            outcome = await async_fn(req.actor, self._core.actors[req.actor], req.args, token)
        else:
            outcome = reg.effect_handler(req.actor, self._core.actors[req.actor], req.args, token)

        if isinstance(outcome, ImmediateSucceeded):
            rep = OriginExecuteReply(req.execution_id, "succeeded", effect_summary=outcome.effect_summary)
            self._core._store_ledger_reply(rep)
            await self.flush_dirty_sync(False)
            return rep
        if isinstance(outcome, ImmediateFailed):
            rep = OriginExecuteReply(req.execution_id, "failed", failure_reason=outcome.reason)
            self._core._store_ledger_reply(rep)
            await self.flush_dirty_sync(False)
            return rep
        if isinstance(outcome, StartedExecuting):
            self._core.in_flight_steps[req.execution_id] = outcome.cancel_fn
            self._core._in_flight_tokens[req.execution_id] = token
            rep = OriginExecuteReply(req.execution_id, "executing")
            self._core._store_ledger_reply(rep)
            return rep
        raise G2AOriginError("invalid_request", 400, "Invalid EffectInvocationResult")

    async def start_loopback_execute_listener(self, host: str = "127.0.0.1", port: int = 0) -> str:
        async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while not self._closed:
                    req_line = await reader.readline()
                    if not req_line:
                        break
                    headers: Dict[str, str] = {}
                    while True:
                        hline = await reader.readline()
                        if hline in (b"\r\n", b"\n", b""):
                            break
                        decoded = hline.decode("latin1").rstrip("\r\n")
                        if ":" in decoded:
                            k, v = decoded.split(":", 1)
                            headers[k.strip().lower()] = v.strip()
                    want_close = headers.get("connection", "").lower() == "close"
                    length = int(headers.get("content-length", "0"))
                    raw_body = await reader.readexactly(length) if length > 0 else b""

                    if self._core.shared_token:
                        auth = headers.get("authorization", "")
                        cand = auth[7:].strip() if auth.startswith("Bearer ") else ""
                        if not cand or not hmac.compare_digest(cand.encode("utf-8"), self._core.shared_token.encode("utf-8")):
                            await _send_json(writer, 401, "", "not_accepted", None, {"code": "unauthenticated", "http_status": 401, "message": "Invalid Origin token"}, want_close)
                            if want_close:
                                break
                            continue

                    try:
                        env = parse_strict_json(raw_body)
                        req_id = str(env.get("request_id", ""))
                        if env.get("operation") != "origin.execute":
                            raise G2AOriginError("invalid_request", 400, "Only origin.execute is accepted")
                        exec_req = OriginExecuteRequest.from_dict(env["payload"])
                        reply_obj = await self.handle_origin_execute(exec_req)
                        await _send_json(writer, 200, req_id, "accepted", reply_obj.to_dict(), None, want_close)
                    except StrictJSONError as jerr:
                        await _send_json(writer, 400, "", "not_accepted", None, {"code": "invalid_request", "http_status": 400, "message": str(jerr)}, want_close)
                    except G2AOriginError as gerr:
                        await _send_json(writer, gerr.http_status, "", "not_accepted", None, {"code": gerr.code, "http_status": gerr.http_status, "message": gerr.message}, want_close)
                    if want_close:
                        break
            except Exception:
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        async def _send_json(
            writer: asyncio.StreamWriter,
            status: int,
            req_id: str,
            acceptance: str,
            result: Any,
            err_obj: Any,
            close_conn: bool,
        ) -> None:
            body = json.dumps(
                {
                    "g2a_version": G2A_WIRE_VERSION,
                    "request_id": req_id,
                    "timestamp_ms": int(time.time() * 1000),
                    "acceptance": acceptance,
                    "result": result,
                    "error": err_obj,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            conn_hdr = "close" if close_conn else "keep-alive"
            head = (
                f"HTTP/1.1 {status} OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: {conn_hdr}\r\n\r\n"
            ).encode("ascii")
            writer.write(head + body)
            await writer.drain()

        self._server = await asyncio.start_server(_handle_client, host=host, port=port)
        sock = self._server.sockets[0]
        bound_host, bound_port = sock.getsockname()[:2]
        self._core.origin_execute_url = f"http://{bound_host}:{bound_port}/g2a/v1/origin/execute"
        return self._core.origin_execute_url

    async def publish_capabilities_and_baseline(self, **kwargs: Any) -> None:
        await asyncio.to_thread(self._core.publish_capabilities_and_baseline, **kwargs)

    async def flush_dirty_sync(self, significant_event: bool = False) -> bool:
        return await asyncio.to_thread(self._core.flush_dirty_sync, significant_event)

    async def flush_full_sync(self) -> None:
        await asyncio.to_thread(self._core.flush_full_sync)

    async def complete_step(
        self,
        execution_id: str,
        succeeded: bool = True,
        summary: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        await asyncio.to_thread(self._core.complete_step, execution_id, succeeded, summary, reason)

    async def complete_steps_batch(
        self,
        settlements: Sequence[Tuple[str, bool, Optional[Dict[str, Any]], Optional[str]]],
    ) -> int:
        return await asyncio.to_thread(self._core.complete_steps_batch, settlements)

    async def report_player_command(self, command_text: str) -> None:
        await asyncio.to_thread(self._core.report_player_command, command_text)

    async def report_player_presence(self, online: bool, remaining_online_humans: int = 0) -> None:
        await asyncio.to_thread(self._core.report_player_presence, online, remaining_online_humans)
