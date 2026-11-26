"""
Dynamic Context-Scoped Tool Schema Compiler & Zero-Dependency G2A MCP Server (JSON-RPC 2.0).

Bridges G2A V0.0.1 `ProjectedContext` + `CapabilityEntry` affordances directly into:
  1. OpenAI / vLLM / Ollama Function-Calling `tools` (`format="openai"`);
  2. Anthropic Claude `tools` (`format="anthropic"`);
  3. Model Context Protocol (`MCP`, `2024-11-05`) `tools/list`, `tools/call`, `resources/list`, & `resources/read`
     over both `stdio` and loopback `HTTP` JSON-RPC 2.0!
"""

from __future__ import annotations

import copy
import http.server
import json
import re
import socketserver
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .agent_runtime import resolve_prerequisite_steps
from .core_sdk import CoreSDK
from .jcs import parse_strict_json
from .memory import MemoryRecord, MemoryService, SpoilerGuard
from .schemas import CapabilityEntry, G2AError, PlanDecision, ProjectedContext, QueueStep


@dataclass
class ToolBinding:
    tool_name: str
    method_id: str
    actor: str


def _safe_ident(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw.strip()).strip("_")
    return cleaned or "op"


def _escape_label(text: str) -> str:
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def compile_context_tools(
    capabilities: Sequence[CapabilityEntry],
    context: ProjectedContext,
    fmt: str = "openai",  # "openai" | "anthropic" | "mcp"
    include_meta_tools: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, ToolBinding]]:
    """
    Compiles ONLY currently granted `(method_id, actor)` pairs in `context.active_methods` into
    constrained Function-Calling / MCP tool schemas with live `target_id` candidate `enum`s.
    """
    cap_by_id: Dict[str, CapabilityEntry] = {c.method_id: c for c in capabilities}
    tools: List[Dict[str, Any]] = []
    bindings: Dict[str, ToolBinding] = {}

    for grant in context.active_methods:
        cap = cap_by_id.get(grant.method_id)
        if cap is None:
            continue
        t_name = f"g2a_act__{_safe_ident(cap.method_id)}_{_safe_ident(cap.name)}__{_safe_ident(grant.actor)}"[:64]
        bindings[t_name] = ToolBinding(tool_name=t_name, method_id=cap.method_id, actor=grant.actor)

        schema: Dict[str, Any] = copy.deepcopy(cap.params_schema) if cap.params_schema else {"type": "object"}
        schema.setdefault("type", "object")
        props = dict(schema.get("properties") or {})

        desc_parts = [
            f"[{cap.method_id} actor={grant.actor} readiness={grant.readiness}"
            + (" sustained=true" if cap.sustained else "")
            + f" policy={cap.default_policy}] {cap.description}"
        ]
        if grant.targets:
            cand_ids = [t.target_id for t in grant.targets]
            t_prop = dict(props.get("target_id") or {"type": "string"})
            t_prop["type"] = "string"
            t_prop["enum"] = cand_ids
            props["target_id"] = t_prop
            summaries = []
            for t in grant.targets[:12]:
                tags = [t.readiness]
                if t.player_anchor:
                    tags.append(t.player_anchor)
                elif t.actor_anchor:
                    tags.append(t.actor_anchor)
                if t.prerequisite_hint:
                    tags.append(f"hint:{t.prerequisite_hint}")
                if t.alert_tag:
                    tags.append(f"alert:{t.alert_tag}")
                summaries.append(f"{t.target_id}('{_escape_label(t.label)}', {','.join(tags)})")
            desc_parts.append("Candidates: " + "; ".join(summaries))
        schema["properties"] = props

        full_desc = " | ".join(desc_parts)
        tools.append(_wrap_tool_schema(t_name, full_desc, schema, fmt))

    if include_meta_tools:
        submit_schema: Dict[str, Any] = {
            "type": "object",
            "required": ["intent_summary", "steps"],
            "properties": {
                "intent_summary": {"type": "string"},
                "goal_status": {"type": "string", "enum": ["completed", "continue", "blocked"]},
                "reply_text": {"type": "string"},
                "audience": {"type": "string", "enum": ["player_only", "party", "public"]},
                "memory_note": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["method_id", "actor"],
                        "properties": {
                            "method_id": {"type": "string"},
                            "actor": {"type": "string"},
                            "args": {"type": "object"},
                        },
                    },
                },
            },
        }
        tools.append(
            _wrap_tool_schema(
                "g2a_submit_queue",
                "Submit an atomic multi-step G2A MethodCallQueue (step 0 must be ready; subsequent steps may be staged).",
                submit_schema,
                fmt,
            )
        )

    return tools, bindings


def _wrap_tool_schema(name: str, description: str, parameters: Dict[str, Any], fmt: str) -> Dict[str, Any]:
    if fmt == "anthropic":
        return {"name": name, "description": description, "input_schema": parameters}
    if fmt == "mcp":
        return {"name": name, "description": description, "inputSchema": parameters}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def tool_calls_to_plan_decision(
    tool_calls: Sequence[Dict[str, Any]],
    bindings: Dict[str, ToolBinding],
    capabilities: Sequence[CapabilityEntry],
    context: ProjectedContext,
) -> PlanDecision:
    """
    Translates OpenAI `tool_calls` (or `{name, arguments}` dicts) into a firewall-verified `PlanDecision`,
    automatically unrolling `"staged"` prerequisites when single action tools are invoked!
    """
    from .llm_planner import OpenAICompatibleLLMPlanner

    cap_by_id = {c.method_id: c for c in capabilities}
    raw_steps: List[Dict[str, Any]] = []
    intent_summary = "Execute tool-planned companion actions"
    goal_status = "completed"
    reply_text: Optional[str] = None
    audience = "player_only"
    memory_note: Optional[str] = None

    for tc in tool_calls:
        fn_block = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        name = str(fn_block.get("name", ""))
        raw_args = fn_block.get("arguments", fn_block.get("input", {}))
        if isinstance(raw_args, str):
            parsed_args = parse_strict_json(raw_args) if raw_args.strip() else {}
            args_dict = parsed_args if isinstance(parsed_args, dict) else {}
        elif isinstance(raw_args, dict):
            args_dict = dict(raw_args)
        else:
            args_dict = {}

        if name == "g2a_submit_queue":
            intent_summary = str(args_dict.get("intent_summary") or intent_summary)
            goal_status = str(args_dict.get("goal_status") or goal_status)
            if isinstance(args_dict.get("reply_text"), str):
                reply_text = args_dict["reply_text"]
            if isinstance(args_dict.get("audience"), str):
                audience = args_dict["audience"]
            if isinstance(args_dict.get("memory_note"), str):
                memory_note = args_dict["memory_note"]
            for st in args_dict.get("steps") or []:
                if isinstance(st, dict):
                    raw_steps.append(
                        {
                            "method_id": st.get("method_id", ""),
                            "actor": st.get("actor", ""),
                            "args": st.get("args") if isinstance(st.get("args"), dict) else {},
                        }
                    )
        elif name in bindings:
            b = bindings[name]
            if not raw_steps:
                # First step: automatically prepend ready prerequisites if the model called a staged verb directly!
                chain = resolve_prerequisite_steps(b.method_id, b.actor, args_dict, context, capabilities)
                for c_mid, c_act, c_args in chain:
                    raw_steps.append({"method_id": c_mid, "actor": c_act, "args": c_args})
            else:
                raw_steps.append({"method_id": b.method_id, "actor": b.actor, "args": args_dict})
            intent_summary = f"Invoke {b.method_id} as {b.actor}"
        else:
            # Unknown/hallucinated tool name -> reject entire tainted plan!
            return PlanDecision()

    synthesized_raw = {
        "intent_summary": intent_summary,
        "goal_status": goal_status,
        "reply_text": reply_text,
        "audience": audience,
        "memory_note": memory_note,
        "steps": raw_steps,
    }
    return OpenAICompatibleLLMPlanner._sanitize_llm_plan(synthesized_raw, cap_by_id, context)


class _QuietMCPHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        return


class G2AMCPServer:
    """
    Zero-dependency Model Context Protocol (`MCP`) JSON-RPC 2.0 server exposing a connected `CoreSDK`
    to external AI Agent hosts (Claude Desktop, Cursor, Antigravity, LangGraph, OpenAI Agents) via
    `stdio` or loopback `HTTP` (`POST /mcp`).
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, sdk: CoreSDK, server_name: str = "g2a-computation-core-mcp") -> None:
        self.sdk = sdk
        self.server_name = server_name
        self._httpd: Optional[_QuietMCPHTTPServer] = None
        self._latest_bindings: Dict[str, ToolBinding] = {}
        self._lock = threading.RLock()

    def _current_context(self) -> ProjectedContext:
        pulled = self.sdk.pull_context_if_due()
        if pulled is not None:
            return pulled
        if self.sdk.latest_context is not None:
            return self.sdk.latest_context
        return self.sdk.pull_context()

    def handle_jsonrpc(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req_id = req.get("id")
        method = str(req.get("method", ""))
        params = req.get("params") if isinstance(req.get("params"), dict) else {}

        # JSON-RPC 2.0 notifications have no "id" and expect no response
        if "id" not in req:
            return None

        try:
            if method == "initialize":
                return self._ok(
                    req_id,
                    {
                        "protocolVersion": self.PROTOCOL_VERSION,
                        "capabilities": {
                            "tools": {"listChanged": True},
                            "resources": {"subscribe": False, "listChanged": False},
                        },
                        "serverInfo": {"name": self.server_name, "version": "0.0.1"},
                    },
                )
            if method == "ping":
                return self._ok(req_id, {})
            if method == "resources/list":
                return self._ok(
                    req_id,
                    {
                        "resources": [
                            {
                                "uri": "g2a://context/live",
                                "name": "Live G2A ProjectedContext",
                                "mimeType": "application/json",
                                "description": "Current authorized game state brief, active methods, and deictic targets.",
                            },
                            {
                                "uri": "g2a://capabilities/cached",
                                "name": "Verified Slot Capability Catalog",
                                "mimeType": "application/json",
                                "description": "JCS-verified G2A capability definitions and JSON Schemas.",
                            },
                            {
                                "uri": "g2a://memories/safe",
                                "name": "Spoiler-Guarded Companion Memories",
                                "mimeType": "application/json",
                                "description": "Player-owned memories filtered by current epoch and allowed_timeline_tags.",
                            },
                        ]
                    },
                )
            if method == "resources/read":
                uri = str(params.get("uri", ""))
                return self._ok(req_id, self._read_resource(uri))
            if method == "tools/list":
                return self._ok(req_id, {"tools": self._list_mcp_tools()})
            if method == "tools/call":
                t_name = str(params.get("name", ""))
                t_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                return self._ok(req_id, self._call_mcp_tool(t_name, t_args))

            return self._err(req_id, -32601, f"Method not found: {method}")
        except G2AError as gerr:
            return self._ok(
                req_id,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"error": gerr.code, "message": gerr.message}, ensure_ascii=False)}],
                },
            )
        except Exception as exc:
            return self._err(req_id, -32603, str(exc))

    def _read_resource(self, uri: str) -> Dict[str, Any]:
        if uri == "g2a://context/live":
            ctx = self._current_context()
            payload: Any = {
                "slot_id": ctx.slot_id,
                "epoch": ctx.epoch,
                "rev": ctx.rev,
                "player_command": ctx.player_command,
                "state_brief": ctx.state_brief,
                "allowed_timeline_tags": ctx.allowed_timeline_tags,
                "active_methods": [
                    {
                        "method_id": g.method_id,
                        "actor": g.actor,
                        "readiness": g.readiness,
                        "targets": [t.__dict__ for t in g.targets],
                    }
                    for g in ctx.active_methods
                ],
            }
        elif uri == "g2a://capabilities/cached":
            payload = [c.to_dict() for c in self.sdk.get_cached_capabilities()]
        elif uri == "g2a://memories/safe":
            ctx = self._current_context()
            scope = self.sdk.scope
            recs = (
                self.sdk.memory_store.recall_safe(
                    scope.player_id,
                    SpoilerGuard(
                        current_epoch=ctx.epoch,
                        game_origin_id=scope.game_instance_id,
                        allowed_timeline_tags=set(ctx.allowed_timeline_tags) if ctx.allowed_timeline_tags else None,
                    ),
                )
                if scope is not None
                else []
            )
            payload = [r.to_dict() for r in recs]
        else:
            raise ValueError(f"Unknown resource URI: {uri}")
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }

    def _list_mcp_tools(self) -> List[Dict[str, Any]]:
        ctx = self._current_context()
        caps = self.sdk.get_cached_capabilities(ctx.slot_id)
        dyn_tools, bindings = compile_context_tools(caps, ctx, fmt="mcp", include_meta_tools=True)
        with self._lock:
            self._latest_bindings = bindings

        mgmt_tools = [
            {
                "name": "g2a_pull_context",
                "description": "Pull the latest authorized G2A ProjectedContext (state_brief, active_methods, deictic targets).",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "g2a_execute_goal",
                "description": "Run the closed-loop multi-turn G2A goal executor until completion or max_turns.",
                "inputSchema": {
                    "type": "object",
                    "required": ["objective"],
                    "properties": {
                        "objective": {"type": "string"},
                        "max_turns": {"type": "integer", "minimum": 1, "maximum": 12},
                        "timeout_sec": {"type": "number", "minimum": 0.5, "maximum": 60.0},
                    },
                },
            },
            {
                "name": "g2a_send_chat",
                "description": "Send a companion chat message to the player or party.",
                "inputSchema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string"},
                        "audience": {"type": "string", "enum": ["player_only", "party", "public"]},
                    },
                },
            },
            {
                "name": "g2a_remember",
                "description": "Record a concise spoiler-isolated episodic observation into player-owned companion memory.",
                "inputSchema": {
                    "type": "object",
                    "required": ["note"],
                    "properties": {"note": {"type": "string"}},
                },
            },
        ]
        return mgmt_tools + dyn_tools

    def _call_mcp_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "g2a_pull_context":
            return {"isError": False, "content": [{"type": "text", "text": self._read_resource("g2a://context/live")["contents"][0]["text"]}]}

        if name == "g2a_send_chat":
            res = self.sdk.send_chat(str(args.get("text", "")), audience=str(args.get("audience", "player_only")))
            return {"isError": False, "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        if name == "g2a_remember":
            from .agent_runtime import AgentGoalTracker

            ctx = self._current_context()
            scope = self.sdk.scope
            rec = AgentGoalTracker.record_episodic_observation(
                self.sdk.memory_store,
                scope.player_id if scope else "player-local",
                scope.game_instance_id if scope else "",
                ctx,
                str(args.get("note", "")),
            )
            return {"isError": False, "content": [{"type": "text", "text": json.dumps(rec.to_dict() if rec else {}, ensure_ascii=False)}]}

        if name == "g2a_execute_goal":
            rep = self.sdk.execute_goal(
                str(args.get("objective", "")),
                max_turns=int(args.get("max_turns", 5)),
                timeout_sec=float(args.get("timeout_sec", 8.0)),
            )
            return {
                "isError": not rep.succeeded,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "goal_id": rep.goal_id,
                                "succeeded": rep.succeeded,
                                "status": rep.status,
                                "turns_used": rep.turns_used,
                                "submitted_queues": rep.submitted_queues,
                                "outcomes": [o.to_prompt_dict() for o in rep.outcomes],
                                "failure_reason": rep.failure_reason,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            }

        ctx = self._current_context()
        caps = self.sdk.get_cached_capabilities(ctx.slot_id)
        _, fresh_bindings = compile_context_tools(caps, ctx, fmt="mcp", include_meta_tools=True)
        with self._lock:
            bindings = {**self._latest_bindings, **fresh_bindings}

        decision = tool_calls_to_plan_decision([{"name": name, "arguments": args}], bindings, caps, ctx)
        if not decision.planned_steps:
            return {
                "isError": True,
                "content": [{"type": "text", "text": json.dumps({"error": "invalid_or_unready_tool_call", "tool": name}, ensure_ascii=False)}],
            }

        if decision.reply_text and self.sdk.scope is not None and self.sdk.scope.allow_chat:
            self.sdk.send_chat(decision.reply_text, audience=decision.audience)

        qb = self.sdk.build_queue(ctx, intent_summary=decision.intent_summary, trigger="player_command", supersedes=True)
        for st in decision.planned_steps:
            qb.add_step(st.method_id, st.actor, st.args, validate=True)
        built_q = qb.build()
        initial_rec = self.sdk.submit_queue_with_standard_recovery(built_q, _submit_ctx=ctx)
        final_rec = self.sdk.wait_for_queue_terminal(built_q.queue_id, initial_receipt=initial_rec, timeout_sec=5.0)
        fresh_ctx = self.sdk.pull_context_if_due() or self.sdk.latest_context or ctx
        ok = final_rec is not None and final_rec.queue_state == "completed"
        return {
            "isError": not ok,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "queue_id": built_q.queue_id,
                            "queue_state": final_rec.queue_state if final_rec else "awaiting_confirmation",
                            "step_receipts": [
                                {
                                    "step_index": sr.step_index,
                                    "method_id": sr.method_id,
                                    "state": sr.state,
                                    "effect_summary": sr.effect_summary,
                                    "failure_reason": sr.failure_reason,
                                }
                                for sr in (final_rec.step_receipts if final_rec else [])
                            ],
                            "state_brief": fresh_ctx.state_brief,
                            "rev": fresh_ctx.rev,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }

    @staticmethod
    def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def serve_stdio(self) -> None:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = parse_strict_json(line)
            except Exception as exc:
                sys.stdout.write(json.dumps(self._err(None, -32700, f"Parse error: {exc}")) + "\n")
                sys.stdout.flush()
                continue
            if isinstance(req, dict):
                resp = self.handle_jsonrpc(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    sys.stdout.flush()

    def start_http(self, host: str = "127.0.0.1", port: int = 0) -> str:
        mcp_ref = self

        class _MCPHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                if self.path.split("?", 1)[0] not in ("/mcp", "/"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                cl = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(cl) if cl > 0 else b"{}"
                try:
                    req = parse_strict_json(raw)
                    if not isinstance(req, dict):
                        raise ValueError("Expected JSON-RPC object")
                    resp = mcp_ref.handle_jsonrpc(req)
                except Exception as exc:
                    resp = mcp_ref._err(None, -32700, str(exc))

                if resp is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    return
                out = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(out)

        self._httpd = _QuietMCPHTTPServer((host, port), _MCPHandler)
        h, p = self._httpd.server_address
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return f"http://{h}:{p}/mcp"

    def stop_http(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
