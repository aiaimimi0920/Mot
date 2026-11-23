from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Dict, List

from .core_sdk import CoreSDK, discover_local_hub_manifest
from .sqlite_memory import SQLiteCompanionStore


def _wait(pred, timeout_sec: float = 5.0) -> bool:
    end = time.time() + timeout_sec
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.04)
    return pred()


def run_three_tier_demo_workbench(genre: str = "all") -> Dict[str, str]:
    """
    Launches Go Intelligent Hub + Python Interaction Origin + Python Computation Core
    using zero-config `hub.runtime.json` auto-discovery and runs end-to-end genre scenarios.
    """
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    origin_py_src = os.path.join(root_dir, "interaction-origin", "python", "src")
    if origin_py_src not in sys.path:
        sys.path.insert(0, origin_py_src)

    from g2a_origin.demo_host import (
        build_deduction_demo_sdk,
        build_spatial_demo_sdk,
        build_strategy_card_demo_sdk,
        build_tactical_arena_outbound_sdk,
        build_workshop_demo_sdk,
    )
    from g2a_origin.origin_sdk import discover_local_hub_origin_url
    from .cli import HeuristicReferencePlanner

    hub_bin = os.path.join(root_dir, "intelligent-hub", "bin", "intelligent-hub.exe")
    if not os.path.exists(hub_bin):
        raise FileNotFoundError(f"Intelligent Hub binary not found at {hub_bin}")

    genres: List[str] = (
        ["spatial", "strategy_card", "workshop", "deduction", "tactical_arena"]
        if genre == "all"
        else [genre]
    )
    results: Dict[str, str] = {}

    with tempfile.TemporaryDirectory() as tmp:
        runtime_path = os.path.join(tmp, "hub.runtime.json")
        ledger_path = os.path.join(tmp, "hub.ledger.json")
        db_path = os.path.join(tmp, "companion.db")

        hub_proc = subprocess.Popen(
            [
                hub_bin,
                "-origin-addr", "127.0.0.1:0",
                "-core-addr", "127.0.0.1:0",
                "-consent-addr", "127.0.0.1:0",
                "-ledger", ledger_path,
                "-runtime-file", runtime_path,
                "-auto-approve-scope",
                "-print-ports-json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _ = json.loads(hub_proc.stdout.readline().strip())
            # Both Origin and Core discover Hub purely via runtime_path!
            origin_url = discover_local_hub_origin_url(runtime_file=runtime_path)
            manifest = discover_local_hub_manifest(runtime_file=runtime_path)
            consent_url = manifest["dashboard_url"].rstrip("/")
            no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            for g in genres:
                store = SQLiteCompanionStore(db_path)
                core = CoreSDK(planner=HeuristicReferencePlanner(), memory_store=store, allow_loopback=True, heartbeat_interval_sec=1.0)
                if g == "spatial":
                    orig, world, _ = build_spatial_demo_sdk(origin_url, slot_id="demo-spatial")
                    try:
                        core.connect_local_hub("tok-spatial", "demo-spatial", runtime_file=runtime_path)
                        time.sleep(0.1)
                        orig.report_player_command("打开右侧的宝箱")
                        assert _wait(lambda: "chest_R" in world.opened_chests)
                        results[g] = "opened chest_R via sustained move_to + direct interact"
                    finally:
                        core.shutdown_session_cleanly()
                        orig.stop_loopback_execute_listener()
                        store.close()

                elif g == "strategy_card":
                    orig, sim = build_strategy_card_demo_sdk(origin_url, slot_id="demo-card")
                    try:
                        core.connect_local_hub("tok-card", "demo-card", runtime_file=runtime_path)
                        time.sleep(0.1)
                        orig.report_player_command("出牌 combo")
                        assert _wait(lambda: sim.boss_hp == 12)
                        results[g] = "mana_crystal unlocked staged fireball (boss_hp 20 -> 12)"
                    finally:
                        core.shutdown_session_cleanly()
                        orig.stop_loopback_execute_listener()
                        store.close()

                elif g == "workshop":
                    orig, sim = build_workshop_demo_sdk(origin_url, slot_id="demo-workshop")
                    try:
                        core.connect_local_hub("tok-workshop", "demo-workshop", runtime_file=runtime_path)
                        time.sleep(0.1)
                        sim.bench_running = True
                        orig.notify_module_changed("workshop", significant_event=False)
                        sim.bench_running = False
                        sim.timber = 0
                        orig.notify_module_changed("workshop", significant_event=True)
                        assert _wait(lambda: len(core.pending_confirm_queues) == 1)
                        with no_proxy.open(f"{consent_url}/trusted/pending", timeout=3.0) as r:
                            qp = json.loads(r.read().decode("utf-8"))["queue_prompts"][0]
                        req = urllib.request.Request(
                            f"{consent_url}/trusted/decide-queue",
                            data=json.dumps({
                                "session_id": qp["session_id"],
                                "queue_id": qp["queue_id"],
                                "queue_digest": qp["queue_digest"],
                                "fingerprint": qp["fingerprint"],
                                "approved": True,
                            }).encode("utf-8"),
                            method="POST",
                            headers={"Content-Type": "application/json"},
                        )
                        with no_proxy.open(req, timeout=3.0):
                            pass
                        assert _wait(lambda: sim.bench_running)
                        results[g] = f"single-prompt confirmed {qp['queue_id']} & resumed Bench_1"
                    finally:
                        core.shutdown_session_cleanly()
                        orig.stop_loopback_execute_listener()
                        store.close()

                elif g == "deduction":
                    orig, board = build_deduction_demo_sdk(origin_url, slot_id="demo-deduction")
                    try:
                        core.connect_local_hub("tok-deduction", "demo-deduction", runtime_file=runtime_path)
                        time.sleep(0.1)
                        orig.report_player_command("整理线索")
                        assert _wait(lambda: "clue_watch" in board.companion_pins)
                        results[g] = "marked clue_watch on companion overlay (attempts_left=3, culprit redacted)"
                    finally:
                        core.shutdown_session_cleanly()
                        orig.stop_loopback_execute_listener()
                        store.close()

                elif g == "tactical_arena":
                    orig, sim = build_tactical_arena_outbound_sdk(origin_url, slot_id="demo-arena")
                    try:
                        core.connect_local_hub("tok-arena", "demo-arena", runtime_file=runtime_path)
                        time.sleep(0.1)
                        orig.report_player_command("掩护 cover")
                        assert _wait(lambda: sim.shield_active)
                        results[g] = "executed ping_map + squad barrier over zero-port outbound stream"
                    finally:
                        core.shutdown_session_cleanly()
                        orig.stop_loopback_execute_listener()
                        store.close()
        finally:
            hub_proc.terminate()
            try:
                hub_proc.wait(timeout=3)
            except Exception:
                hub_proc.kill()
            if hub_proc.stdout:
                hub_proc.stdout.close()
            if hub_proc.stderr:
                hub_proc.stderr.close()

    return results
