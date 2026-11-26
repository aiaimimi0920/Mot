from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from typing import Any, Dict, List, Optional

from .cli import HeuristicReferencePlanner
from .core_sdk import CoreSDK, discover_local_hub_manifest
from .jcs import parse_strict_json
from .llm_planner import OpenAICompatibleLLMPlanner
from .memory import MemoryRecord, MemoryService
from .schemas import CapabilityEntry, G2AError, PlanDecision, ProjectedContext
from .sqlite_memory import SQLiteCompanionStore

MAX_CONSOLE_BODY_BYTES = 4 << 20


class _QuietThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        return


class _TracingPlanner:
    """Wraps a planner to record live cognitive decisions for the Companion Console UI."""

    def __init__(self, inner: Any, trace_sink: List[Dict[str, Any]], lock: threading.Lock) -> None:
        self._inner = inner
        self._trace = trace_sink
        self._lock = lock

    def plan(
        self,
        capabilities: List[CapabilityEntry],
        context: ProjectedContext,
        recalled_memories: List[MemoryRecord],
        active_goal: Any = None,
        recent_outcomes: Any = None,
    ) -> PlanDecision:
        try:
            decision = self._inner.plan(
                capabilities, context, recalled_memories, active_goal=active_goal, recent_outcomes=recent_outcomes
            )
        except TypeError:
            decision = self._inner.plan(capabilities, context, recalled_memories)
        entry = {
            "timestamp_ms": int(time.time() * 1000),
            "epoch": context.epoch,
            "rev": context.rev,
            "player_command": context.player_command,
            "active_methods_count": len(context.active_methods),
            "recalled_memories_count": len(recalled_memories),
            "intent_summary": decision.intent_summary,
            "reply_text": decision.reply_text,
            "planned_steps": [s.to_dict() for s in (decision.planned_steps or [])],
        }
        with self._lock:
            self._trace.insert(0, entry)
            del self._trace[40:]
        return decision


def resolve_hub_binary(root_dir: str) -> str:
    exe_name = "intelligent-hub.exe" if os.name == "nt" else "intelligent-hub"
    candidate = os.path.join(root_dir, "intelligent-hub", "bin", exe_name)
    if os.path.exists(candidate):
        return candidate
    alt = os.path.join(root_dir, "intelligent-hub", "bin", "intelligent-hub.exe")
    if os.path.exists(alt):
        return alt
    raise FileNotFoundError(f"Intelligent Hub binary not found at {candidate}")


class LivePlaygroundStack:
    """
    Self-contained 3-application visual playground:
      1. Intelligent Hub Go daemon (Origin, Core, and Trusted Security Web Dashboard)
      2. HTML5 Spatial + Workshop Interaction Origin Web Game static server
      3. Computation Core Runtime (WebSocket CoreSDK + SQLite Memory + Companion Console UI)
    """

    def __init__(
        self,
        slot_id: str = "slot-web-hybrid",
        origin_port: int = 0,
        core_port: int = 0,
        consent_port: int = 0,
        web_game_port: int = 0,
        console_port: int = 0,
        auto_approve_scope: bool = True,
        planner_kind: str = "heuristic",
        llm_url: str = "http://127.0.0.1:11434/v1",
        llm_model: str = "qwen2.5:7b",
    ) -> None:
        self.slot_id = slot_id
        self.origin_port = origin_port
        self.core_port = core_port
        self.consent_port = consent_port
        self.web_game_port = web_game_port
        self.console_port = console_port
        self.auto_approve_scope = auto_approve_scope
        self.planner_kind = planner_kind
        self.llm_url = llm_url
        self.llm_model = llm_model

        self._root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        self._tmp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        self._hub_proc: Optional[subprocess.Popen[str]] = None
        self._game_httpd: Optional[_QuietThreadingHTTPServer] = None
        self._console_httpd: Optional[_QuietThreadingHTTPServer] = None
        self._store: Optional[SQLiteCompanionStore] = None
        self._base_planner: Optional[Any] = None
        self._sdk: Optional[CoreSDK] = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._traces: List[Dict[str, Any]] = []

        self.hub_origin_url = ""
        self.hub_core_url = ""
        self.hub_dashboard_url = ""
        self.web_game_url = ""
        self.core_console_url = ""

    def start(self) -> Dict[str, str]:
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp = self._tmp_dir.name
        runtime_path = os.path.join(tmp, "hub.runtime.json")
        ledger_path = os.path.join(tmp, "hub.ledger.json")
        db_path = os.path.join(tmp, "playground_companion.db")

        hub_bin = resolve_hub_binary(self._root_dir)
        cmd = [
            hub_bin,
            "-origin-addr", f"127.0.0.1:{self.origin_port}",
            "-core-addr", f"127.0.0.1:{self.core_port}",
            "-consent-addr", f"127.0.0.1:{self.consent_port}",
            "-ledger", ledger_path,
            "-runtime-file", runtime_path,
            "-print-ports-json",
        ]
        if self.auto_approve_scope:
            cmd.append("-auto-approve-scope")

        self._hub_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert self._hub_proc.stdout is not None
        _ = json.loads(self._hub_proc.stdout.readline().strip())
        manifest = discover_local_hub_manifest(runtime_file=runtime_path)
        self.hub_origin_url = manifest["origin_url"].rstrip("/")
        self.hub_core_url = manifest["core_url"].rstrip("/")
        self.hub_dashboard_url = manifest["dashboard_url"]

        # Start HTML5 Web Origin static server
        web_demo_dir = os.path.join(self._root_dir, "interaction-origin", "typescript", "web_demo")
        game_handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=web_demo_dir)

        class _QuietGameHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=web_demo_dir, **kwargs)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._game_httpd = _QuietThreadingHTTPServer(("127.0.0.1", self.web_game_port), _QuietGameHandler)
        _ = game_handler
        game_bound_port = self._game_httpd.server_address[1]
        threading.Thread(target=self._game_httpd.serve_forever, daemon=True).start()
        self.web_game_url = (
            f"http://127.0.0.1:{game_bound_port}/index.html"
            f"?hub={self.hub_origin_url}&dashboard={self.hub_dashboard_url}&slot={self.slot_id}"
        )

        # Initialize Companion SQLite Store & Tracing Planner
        self._store = SQLiteCompanionStore(db_path)
        self._store.upsert_player_record(
            "player-local",
            MemoryRecord(
                memory_id="mem-starter-01",
                created_at_ms=int(time.time() * 1000),
                epistemic_source="player_stated",
                pinned_by_player=True,
                max_disclosure_scope="same_game_companion",
                content="Player prefers opening chests carefully and keeping sawmill timber stocked.",
            ),
        )
        base_planner: Any = HeuristicReferencePlanner()
        if self.planner_kind == "llm":
            base_planner = OpenAICompatibleLLMPlanner(
                base_url=self.llm_url,
                model=self.llm_model,
                api_key=os.environ.get("G2A_LLM_API_KEY", ""),
                allow_loopback=True,
                fallback_planner=base_planner,
            )
        self._base_planner = base_planner
        tracing_planner = _TracingPlanner(base_planner, self._traces, self._lock)
        self._sdk = CoreSDK(
            planner=tracing_planner,
            memory_store=self._store,
            allow_loopback=True,
            stream_mode="websocket",
        )

        # Background auto-attacher once the HTML5 Origin Game registers its slot on Hub
        threading.Thread(target=self._auto_attach_loop, daemon=True).start()

        # Start Companion Console HTTP server
        stack_ref = self

        class _ConsoleHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _check_loopback_host(self) -> bool:
                raw_host = self.headers.get("Host", "").strip().lower()
                if raw_host.startswith("["):
                    host = raw_host.split("]", 1)[0].lstrip("[")
                else:
                    host = raw_host.split(":", 1)[0]
                if host and host not in ("127.0.0.1", "localhost", "::1"):
                    self._send_json(403, {"error": "forbidden_host_dns_rebinding_guard"}, force_close=True)
                    return False
                return True

            def _send_security_headers(self) -> None:
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")

            def _send_json(self, status: int, body: Any, force_close: bool = False) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                want_close = force_close or self.headers.get("Connection", "").strip().lower() == "close"
                self.send_response(status)
                self._send_security_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close" if want_close else "keep-alive")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if not self._check_loopback_host():
                    return
                if self.path == "/" or self.path.startswith("/index.html"):
                    html = stack_ref._render_console_html().encode("utf-8")
                    want_close = self.headers.get("Connection", "").strip().lower() == "close"
                    self.send_response(200)
                    self._send_security_headers()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.send_header("Connection", "close" if want_close else "keep-alive")
                    self.end_headers()
                    self.wfile.write(html)
                    return
                if self.path == "/api/status":
                    with stack_ref._lock:
                        traces = list(stack_ref._traces)
                    sdk = stack_ref._sdk
                    pending_qids: List[str] = []
                    if sdk is not None:
                        with sdk._state_lock:
                            pending_qids = list(sdk.pending_confirm_queues.keys())
                    self._send_json(
                        200,
                        {
                            "g2a_version": "0.0.1",
                            "slot_id": stack_ref.slot_id,
                            "session_id": sdk.session_id if sdk else None,
                            "attached": bool(sdk and sdk.session_id and sdk.is_stream_ready and not sdk.closed),
                            "pending_confirm_queues": pending_qids,
                            "hub_origin_url": stack_ref.hub_origin_url,
                            "hub_core_url": stack_ref.hub_core_url,
                            "hub_dashboard_url": stack_ref.hub_dashboard_url,
                            "web_game_url": stack_ref.web_game_url,
                            "traces": traces,
                        },
                    )
                    return
                if self.path == "/api/memories":
                    assert stack_ref._store is not None
                    recs = [r.to_dict() for r in stack_ref._store.list_records("player-local")]
                    self._send_json(200, {"records": recs})
                    return
                if self.path == "/api/memories/export":
                    assert stack_ref._store is not None
                    svc = MemoryService(stack_ref._store)
                    bundle = svc.export_bundle("player-local")
                    self._send_json(200, bundle.to_dict())
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if not self._check_loopback_host():
                    return
                try:
                    cl = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "invalid_content_length"})
                    return
                if cl < 0 or cl > MAX_CONSOLE_BODY_BYTES:
                    self._send_json(413, {"error": "payload_too_large"}, force_close=True)
                    return
                raw = self.rfile.read(cl) if cl > 0 else b"{}"
                try:
                    data = parse_strict_json(raw) if raw.strip() else {}
                    if not isinstance(data, dict):
                        raise G2AError("invalid_request", 400, "Expected JSON object")
                except G2AError as gerr:
                    self._send_json(400, {"error": gerr.code, "message": gerr.message})
                    return
                if self.path == "/api/attach":
                    ok = stack_ref.try_attach_core()
                    self._send_json(200, {"attached": ok, "session_id": stack_ref._sdk.session_id if stack_ref._sdk else None})
                    return
                if self.path == "/api/memories":
                    assert stack_ref._store is not None
                    mid = f"mem-{uuid.uuid4().hex[:8]}"
                    rec = MemoryRecord(
                        memory_id=mid,
                        created_at_ms=int(time.time() * 1000),
                        epistemic_source="player_stated",
                        pinned_by_player=bool(data.get("pinned", True)),
                        max_disclosure_scope="same_game_companion",
                        content=str(data.get("content", "")).strip() or "Empty note",
                    )
                    stack_ref._store.upsert_player_record("player-local", rec)
                    self._send_json(200, {"saved": rec.to_dict()})
                    return
                if self.path == "/api/memories/delete":
                    assert stack_ref._store is not None
                    ok = stack_ref._store.delete_player_record("player-local", str(data.get("memory_id", "")))
                    self._send_json(200, {"deleted": ok})
                    return
                if self.path == "/api/goal":
                    if not stack_ref._sdk or not stack_ref.try_attach_core():
                        self._send_json(503, {"error": "unavailable", "message": "CoreSDK not yet attached to slot"})
                        return
                    rep = stack_ref._sdk.execute_goal(
                        str(data.get("objective", "")),
                        max_turns=int(data.get("max_turns", 5)),
                        timeout_sec=float(data.get("timeout_sec", 8.0)),
                    )
                    self._send_json(
                        200,
                        {
                            "goal_id": rep.goal_id,
                            "succeeded": rep.succeeded,
                            "status": rep.status,
                            "turns_used": rep.turns_used,
                            "submitted_queues": rep.submitted_queues,
                            "failure_reason": rep.failure_reason,
                        },
                    )
                    return
                self._send_json(404, {"error": "not_found"})

        self._console_httpd = _QuietThreadingHTTPServer(("127.0.0.1", self.console_port), _ConsoleHandler)
        console_bound_port = self._console_httpd.server_address[1]
        threading.Thread(target=self._console_httpd.serve_forever, daemon=True).start()
        self.core_console_url = f"http://127.0.0.1:{console_bound_port}/"

        return {
            "hub_origin_url": self.hub_origin_url,
            "hub_core_url": self.hub_core_url,
            "hub_dashboard_url": self.hub_dashboard_url,
            "web_game_url": self.web_game_url,
            "core_console_url": self.core_console_url,
        }

    def try_attach_core(self) -> bool:
        if not self._sdk:
            return False
        if self._sdk.session_id and not self._sdk.closed:
            return True
        try:
            self._sdk.connect(self.hub_core_url, "core-playground", self.slot_id)
            return True
        except G2AError:
            return False
        except Exception:
            return False

    def _auto_attach_loop(self) -> None:
        while not self._stop_evt.is_set():
            if self.try_attach_core():
                return
            time.sleep(0.25)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._sdk:
            try:
                self._sdk.shutdown_session_cleanly(notify_hub=True)
            except Exception:
                pass
        close_planner = getattr(self._base_planner, "close", None)
        if callable(close_planner):
            try:
                close_planner()
            except Exception:
                pass
        if self._console_httpd:
            self._console_httpd.shutdown()
            self._console_httpd.server_close()
        if self._game_httpd:
            self._game_httpd.shutdown()
            self._game_httpd.server_close()
        if self._store:
            self._store.close()
        if self._hub_proc:
            self._hub_proc.terminate()
            try:
                self._hub_proc.wait(timeout=3)
            except Exception:
                self._hub_proc.kill()
            if self._hub_proc.stdout:
                self._hub_proc.stdout.close()
            if self._hub_proc.stderr:
                self._hub_proc.stderr.close()
        if self._tmp_dir:
            try:
                self._tmp_dir.cleanup()
            except Exception:
                pass

    def __enter__(self) -> "LivePlaygroundStack":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    def _render_console_html(self) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>G2A V0.0.1 — 三层架构可视化联调工作台 (Three-Tier Playground)</title>
  <style>
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, sans-serif;
      background: #090d16;
      color: #f8fafc;
      display: grid;
      grid-template-columns: 1.45fr 1fr;
      height: 100vh;
      overflow: hidden;
    }}
    iframe {{ width: 100%; height: 100%; border: none; border-right: 1px solid #334155; }}
    .right {{
      display: flex;
      flex-direction: column;
      padding: 14px;
      gap: 12px;
      overflow-y: auto;
      background: #0f172a;
    }}
    .box {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 10px;
      padding: 12px;
    }}
    a.btn, button {{
      display: inline-block;
      background: #0284c7;
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 7px 12px;
      text-decoration: none;
      font-weight: 600;
      font-size: 12.5px;
      cursor: pointer;
    }}
    a.btn.shield {{ background: #7c3aed; }}
    pre {{
      margin: 6px 0 0;
      font-size: 11.5px;
      background: #090d16;
      padding: 8px;
      border-radius: 6px;
      max-height: 220px;
      overflow-y: auto;
      white-space: pre-wrap;
    }}
    input {{
      background: #090d16;
      color: #f8fafc;
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 6px 9px;
      width: 68%;
    }}
  </style>
</head>
<body>
  <iframe src="{self.web_game_url}" title="Tier 1 HTML5 Interaction Origin"></iframe>
  <div class="right">
    <div class="box">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <strong>第三层：演算中枢控制台 (Computation Core)</strong>
        <a class="btn shield" href="{self.hub_dashboard_url}" target="_blank">打开第二层：智能枢纽审批中心</a>
      </div>
      <div id="statusLine" style="font-size:12.5px; color:#94a3b8; margin-top:8px;">正在等待源点注册并自动挂载会话...</div>
    </div>

    <div class="box">
      <strong>实时认知决策轨迹 (Cognitive Plan Trace over WebSocket)</strong>
      <pre id="traceBox">暂无规划记录...</pre>
    </div>

    <div class="box">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <strong>玩家自有伙伴记忆库 (SQLite + g2a.memory.bundle.v1)</strong>
        <a class="btn" href="/api/memories/export" target="_blank">导出标准记忆包</a>
      </div>
      <div style="margin-top:8px; display:flex; gap:6px;">
        <input id="memInput" placeholder="添加玩家钉选伙伴记忆..." />
        <button id="btnAddMem">钉选保存</button>
      </div>
      <pre id="memBox"></pre>
    </div>
  </div>
  <script>
    function setTextIfChanged(id, nextText) {{
      const el = document.getElementById(id);
      if (el && el.textContent !== nextText) el.textContent = nextText;
    }}
    async function refresh() {{
      const s = await (await fetch("/api/status")).json();
      setTextIfChanged(
        "statusLine",
        s.attached
          ? `已挂载 WebSocket 会话: ${{s.session_id}} | 待玩家单次确认队列: ${{s.pending_confirm_queues.join(", ") || "无"}}`
          : "等待左侧 HTML5 游戏完成源点注册..."
      );
      setTextIfChanged(
        "traceBox",
        s.traces.length ? JSON.stringify(s.traces, null, 2) : "等待触发玩家指令或工坊告警..."
      );
      const m = await (await fetch("/api/memories")).json();
      setTextIfChanged("memBox", JSON.stringify(m.records, null, 2));
    }}
    document.getElementById("btnAddMem").onclick = async () => {{
      const val = document.getElementById("memInput").value.trim();
      if (!val) return;
      await fetch("/api/memories", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ content: val, pinned: true }}),
      }});
      document.getElementById("memInput").value = "";
      refresh();
    }};
    setInterval(refresh, 900);
    refresh();
  </script>
</body>
</html>"""


def run_interactive_playground(open_browser: bool = True, planner_kind: str = "heuristic") -> None:
    with LivePlaygroundStack(
        origin_port=19210,
        core_port=19220,
        consent_port=19230,
        web_game_port=19240,
        console_port=19250,
        planner_kind=planner_kind,
    ) as stack:
        print(json.dumps({
            "g2a_version": "0.0.1",
            "tier1_web_origin_game": stack.web_game_url,
            "tier2_intelligent_hub_dashboard": stack.hub_dashboard_url,
            "tier3_computation_core_console": stack.core_console_url,
        }, ensure_ascii=False, indent=2))
        if open_browser:
            webbrowser.open(stack.core_console_url)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
