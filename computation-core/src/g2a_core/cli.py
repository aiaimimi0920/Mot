from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from typing import List

from .core_sdk import CoreSDK
from .llm_planner import OpenAICompatibleLLMPlanner
from .memory import MemoryBundleV1, MemoryRecord, MemoryService
from .schemas import CapabilityEntry, PlanDecision, ProjectedContext, QueueStep
from .sqlite_memory import SQLiteCompanionStore


class HeuristicReferencePlanner:
    """Built-in deterministic planner covering spatial roaming, management sims, and deduction boards."""

    def plan(
        self,
        capabilities: List[CapabilityEntry],
        context: ProjectedContext,
        recalled_memories: List[MemoryRecord],
    ) -> PlanDecision:
        grants_by_id = {(g.method_id, g.actor): g for g in context.active_methods}
        cmd = (context.player_command or "").lower()

        # Genre 1: Embodied Spatial Command ("open right chest")
        if "右侧" in cmd or "right" in cmd or "chest" in cmd or "宝箱" in cmd:
            move_grant = grants_by_id.get(("#01", "companion"))
            interact_grant = grants_by_id.get(("#02", "companion"))
            if move_grant and interact_grant:
                target_id = None
                for t in interact_grant.targets:
                    if t.player_anchor and ("right" in t.player_anchor or "pointed" in t.player_anchor):
                        target_id = t.target_id
                        break
                if target_id is None and interact_grant.targets:
                    target_id = interact_grant.targets[0].target_id
                if target_id:
                    cand_map = {t.target_id: t for t in interact_grant.targets}
                    steps: List[QueueStep] = []
                    if cand_map[target_id].readiness == "staged":
                        steps.append(QueueStep(0, "#01", "companion", {"target_id": target_id}))
                        steps.append(QueueStep(1, "#02", "companion", {"target_id": target_id}))
                    else:
                        steps.append(QueueStep(0, "#02", "companion", {"target_id": target_id}))
                    return PlanDecision(
                        intent_summary=f"Move to {target_id} and open it",
                        planned_steps=steps,
                        reply_text=f"Heading over to open {target_id}!",
                        audience="player_only",
                    )

        # Genre 2: Avatar-Less Management Sim Alert ("halted_no_material")
        trade_grant = grants_by_id.get(("#05", "player"))
        resume_grant = grants_by_id.get(("#06", "player"))
        if trade_grant and resume_grant:
            for t in resume_grant.targets:
                if t.alert_tag == "halted_no_material":
                    return PlanDecision(
                        intent_summary=f"Buy 20 timber and resume production on {t.target_id}",
                        planned_steps=[
                            QueueStep(0, "#05", "player", {"item": "timber", "qty": 20}),
                            QueueStep(1, "#06", "player", {"line_id": t.target_id}),
                        ],
                    )

        # Genre 3: Entity-Less Deduction Board ("mark clue" on companion non-intrusive overlay)
        mark_grant = grants_by_id.get(("#07", "companion"))
        if mark_grant and ("线索" in cmd or "clue" in cmd or "整理" in cmd):
            if mark_grant.targets:
                tid = mark_grant.targets[0].target_id
                return PlanDecision(
                    intent_summary=f"Highlight clue {tid} on companion deduction overlay",
                    planned_steps=[QueueStep(0, "#07", "companion", {"target_id": tid, "note": "Timeline contradiction"})],
                    reply_text=f"Marked {tid} on the deduction overlay without spending answer attempts.",
                    audience="player_only",
                )

        # Genre 2: Strategy & Deckbuilder ("combo" / "fireball" -> play ready mana_crystal then staged fireball!)
        card_grant = grants_by_id.get(("#03", "player"))
        if card_grant and ("combo" in cmd or "出牌" in cmd or "fireball" in cmd):
            cands = {t.target_id: t for t in card_grant.targets}
            if "mana_crystal" in cands and "fireball" in cands:
                return PlanDecision(
                    intent_summary="Play Mana Crystal (+2 Mana) then cast Fireball",
                    planned_steps=[
                        QueueStep(0, "#03", "player", {"target_id": "mana_crystal"}),
                        QueueStep(1, "#03", "player", {"target_id": "fireball"}),
                    ],
                )

        # Genre 5: Tactical Arena Outbound Co-op ("cover" / "掩护" -> ping sector_B & trigger squad barrier)
        ping_grant = grants_by_id.get(("#10", "companion"))
        shield_grant = grants_by_id.get(("#11", "companion"))
        if ping_grant and shield_grant and ("cover" in cmd or "掩护" in cmd):
            return PlanDecision(
                intent_summary="Ping Sector B and deploy squad support barrier",
                planned_steps=[
                    QueueStep(0, "#10", "companion", {"target_id": "sector_B"}),
                    QueueStep(1, "#11", "companion", {}),
                ],
            )

        return PlanDecision()


def _cmd_run(args: argparse.Namespace) -> None:
    store = SQLiteCompanionStore(args.memory_db)
    heuristic = HeuristicReferencePlanner()
    if args.planner == "llm":
        planner = OpenAICompatibleLLMPlanner(
            base_url=args.llm_url,
            model=args.llm_model,
            api_key=args.llm_key or os.environ.get("G2A_LLM_API_KEY", ""),
            allow_loopback=args.allow_loopback,
            fallback_planner=heuristic,
        )
    else:
        planner = heuristic

    sdk = CoreSDK(planner=planner, memory_store=store, allow_loopback=args.allow_loopback)
    if args.hub == "auto":
        scope = sdk.connect_local_hub(args.token, args.slot)
    else:
        scope = sdk.connect(args.hub, args.token, args.slot)
    print(
        f"[G2A Core V0.0.1] Connected session={sdk.session_id} slot={scope.slot_id} "
        f"planner={args.planner} memory_db={args.memory_db}"
    )
    try:
        while not sdk.closed:
            time.sleep(0.5)
    except KeyboardInterrupt:
        sdk.shutdown_session_cleanly(notify_hub=True)
    finally:
        store.close()


def _cmd_memory(args: argparse.Namespace) -> None:
    store = SQLiteCompanionStore(args.db)
    svc = MemoryService(store, platform_ceiling=args.ceiling)
    try:
        if args.mem_action == "list":
            recs = store.list_records(args.player)
            print(json.dumps([r.to_dict() for r in recs], ensure_ascii=False, indent=2))
        elif args.mem_action == "add":
            mid = args.id or f"mem-{uuid.uuid4().hex[:8]}"
            rec = MemoryRecord(
                memory_id=mid,
                created_at_ms=int(time.time() * 1000),
                epistemic_source="player_stated",
                pinned_by_player=args.pin,
                max_disclosure_scope=args.scope,
                content=args.content,
            )
            store.upsert_player_record(args.player, rec)
            print(f"Saved memory {mid}")
        elif args.mem_action == "edit":
            ok = store.edit_player_record(args.player, args.id, args.content)
            print("Updated" if ok else "Not found")
        elif args.mem_action in ("pin", "unpin"):
            ok = store.set_pinned(args.player, args.id, args.mem_action == "pin")
            print("Updated" if ok else "Not found")
        elif args.mem_action == "delete":
            ok = store.delete_player_record(args.player, args.id)
            print("Deleted" if ok else "Not found")
        elif args.mem_action == "export":
            bundle = svc.export_bundle(args.player)
            with open(args.file, "w", encoding="utf-8") as f:
                json.dump(bundle.to_dict(), f, ensure_ascii=False, indent=2)
            print(f"Exported {len(bundle.records)} records to {args.file}")
        elif args.mem_action == "import":
            with open(args.file, "r", encoding="utf-8") as f:
                bundle = MemoryBundleV1.from_dict(json.load(f))
            ids = [r.memory_id for r in bundle.records] if not args.ids else args.ids.split(",")
            receipt = svc.import_bundle(args.player, bundle, ids)
            print(json.dumps(receipt.__dict__, ensure_ascii=False, indent=2))
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="G2A V0.0.1 Computation Core Runtime & Memory Manager")
    sub = parser.add_subparsers(dest="subcmd")

    run_p = sub.add_parser("run", help="Connect to an Intelligent Hub and run companion planning loop")
    run_p.add_argument("--hub", default="auto", help="Hub Core URL, or 'auto' to discover via hub.runtime.json")
    run_p.add_argument("--slot", required=True)
    run_p.add_argument("--token", default="core-demo")
    run_p.add_argument("--allow-loopback", action="store_true")
    run_p.add_argument("--planner", choices=["heuristic", "llm"], default="heuristic")
    run_p.add_argument("--llm-url", default="http://127.0.0.1:11434/v1")
    run_p.add_argument("--llm-model", default="qwen2.5:7b")
    run_p.add_argument("--llm-key", default="")
    run_p.add_argument("--memory-db", default="companion_memory.db")

    mem_p = sub.add_parser("memory", help="Inspect, edit, pin, export, or import player-owned companion memories")
    mem_p.add_argument("mem_action", choices=["list", "add", "edit", "pin", "unpin", "delete", "export", "import"])
    mem_p.add_argument("--db", default="companion_memory.db")
    mem_p.add_argument("--player", default="player-local")
    mem_p.add_argument("--id", default="")
    mem_p.add_argument("--content", default="")
    mem_p.add_argument("--pin", action="store_true")
    mem_p.add_argument("--scope", default="same_game_companion")
    mem_p.add_argument("--ceiling", default="same_game_companion")
    mem_p.add_argument("--file", default="memory_bundle.json")
    mem_p.add_argument("--ids", default="")

    conf_p = sub.add_parser("conformance", help="Verify all 14 G2A V0.0.1 error codes against a live Intelligent Hub")
    conf_p.add_argument("--hub-core", default="http://127.0.0.1:19220")
    conf_p.add_argument("--hub-origin", default="http://127.0.0.1:19210")

    demo_p = sub.add_parser("demo", help="Launch self-contained 3-tier G2A demo workbench via zero-config auto-discovery")
    demo_p.add_argument(
        "--genre",
        choices=["all", "spatial", "strategy_card", "workshop", "deduction", "tactical_arena"],
        default="all",
    )

    play_p = sub.add_parser("playground", help="Launch live 3-application visual playground (HTML5 Game + Go Hub + Core Console)")
    play_p.add_argument("--no-browser", action="store_true", help="Do not automatically open the system browser")
    play_p.add_argument("--planner", choices=["heuristic", "llm"], default="heuristic")

    relay_p = sub.add_parser("serve-relay", help="Run Cloud NAT Outbound Relay accepting home Intelligent Hub tunnels")
    relay_p.add_argument("--host", default="127.0.0.1")
    relay_p.add_argument("--port", type=int, default=19280)
    relay_p.add_argument("--relay-token", default="")

    memsrv_p = sub.add_parser("serve-memory", help="Run Companion Memory Portability HTTP Service")
    memsrv_p.add_argument("--db", default="companion_memory.db")
    memsrv_p.add_argument("--ceiling", default="same_game_companion")
    memsrv_p.add_argument("--host", default="127.0.0.1")
    memsrv_p.add_argument("--port", type=int, default=19290)

    mcp_p = sub.add_parser("serve-mcp", help="Expose a G2A slot as a Model Context Protocol (MCP) JSON-RPC 2.0 server")
    mcp_p.add_argument("--hub", default="auto", help="Hub Core URL or 'auto' for hub.runtime.json")
    mcp_p.add_argument("--slot", required=True)
    mcp_p.add_argument("--token", default="core-mcp-agent")
    mcp_p.add_argument("--memory-db", default="companion_memory.db")
    mcp_p.add_argument("--host", default="127.0.0.1")
    mcp_p.add_argument("--http-port", type=int, default=0, help="If >0, serve HTTP JSON-RPC on /mcp instead of stdio")

    args = parser.parse_args()
    if args.subcmd == "run":
        _cmd_run(args)
    elif args.subcmd == "memory":
        _cmd_memory(args)
    elif args.subcmd == "conformance":
        from .conformance import verify_all_14_error_codes_live

        res = verify_all_14_error_codes_live(args.hub_core, args.hub_origin)
        print(json.dumps({"verified_error_codes": res, "count": len(res)}, indent=2, sort_keys=True))
    elif args.subcmd == "demo":
        from .workbench import run_three_tier_demo_workbench

        out = run_three_tier_demo_workbench(args.genre)
        print(json.dumps({"g2a_version": "0.0.1", "demo_results": out}, ensure_ascii=False, indent=2))
    elif args.subcmd == "playground":
        from .playground import run_interactive_playground

        run_interactive_playground(open_browser=not args.no_browser, planner_kind=args.planner)
    elif args.subcmd == "serve-relay":
        from .relay_server import CloudRelayServer

        srv = CloudRelayServer(host=args.host, port=args.port, relay_token=args.relay_token)
        url = srv.start()
        print(f"[G2A Cloud Relay V0.0.1] Listening on {url}")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            srv.stop()
    elif args.subcmd == "serve-memory":
        from .memory_server import MemoryHTTPServer

        store = SQLiteCompanionStore(args.db)
        srv = MemoryHTTPServer(store=store, platform_ceiling=args.ceiling, host=args.host, port=args.port)
        url = srv.start()
        print(f"[G2A Memory HTTP Server V0.0.1] Listening on {url}")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            srv.stop()
            store.close()
    elif args.subcmd == "serve-mcp":
        from .mcp_bridge import G2AMCPServer

        store = SQLiteCompanionStore(args.memory_db)
        try:
            with CoreSDK.connect_quick(
                slot_id=args.slot,
                auth_token=args.token,
                hub_endpoint=args.hub,
                memory_store=store,
            ) as sdk:
                mcp_srv = G2AMCPServer(sdk)
                if args.http_port > 0:
                    url = mcp_srv.start_http(host=args.host, port=args.http_port)
                    print(f"[G2A MCP Server V0.0.1] Listening on {url}", flush=True)
                    try:
                        while True:
                            time.sleep(1.0)
                    except KeyboardInterrupt:
                        mcp_srv.stop_http()
                else:
                    mcp_srv.serve_stdio()
        finally:
            store.close()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
