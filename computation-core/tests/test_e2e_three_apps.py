from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT_DIR, "computation-core", "src"))
sys.path.insert(0, os.path.join(ROOT_DIR, "interaction-origin", "python", "src"))

from g2a_core import CoreSDK, SQLiteCompanionStore
from g2a_core.cli import HeuristicReferencePlanner
from g2a_origin.demo_host import (
    build_deduction_demo_sdk,
    build_spatial_demo_sdk,
    build_workshop_demo_sdk,
)


def _wait_until(predicate, timeout_sec: float = 5.0, step_sec: float = 0.05) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step_sec)
    return predicate()


class TestThreeAppsHeterogeneousE2E(unittest.TestCase):
    """End-to-end verification across Go intelligent-hub.exe + Python interaction-origin + Python computation-core."""

    def setUp(self) -> None:
        self.hub_bin = os.path.join(ROOT_DIR, "intelligent-hub", "bin", "intelligent-hub.exe")
        if not os.path.exists(self.hub_bin):
            self.skipTest("intelligent-hub binary not built yet")
        self._tmp = tempfile.TemporaryDirectory()
        ledger_path = os.path.join(self._tmp.name, "hub_e2e_ledger.json")

        self.hub_proc = subprocess.Popen(
            [
                self.hub_bin,
                "-origin-addr", "127.0.0.1:0",
                "-core-addr", "127.0.0.1:0",
                "-consent-addr", "127.0.0.1:0",
                "-ledger", ledger_path,
                "-no-runtime-file",
                "-auto-approve-scope",
                "-print-ports-json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = self.hub_proc.stdout.readline().strip()
        info = json.loads(line)
        self.origin_url = f"http://{info['origin_addr']}"
        self.core_url = f"http://{info['core_addr']}"
        self.consent_url = f"http://{info['consent_addr']}"

    def tearDown(self) -> None:
        if getattr(self, "hub_proc", None) is not None:
            self.hub_proc.terminate()
            try:
                self.hub_proc.wait(timeout=3)
            except Exception:
                self.hub_proc.kill()
            if self.hub_proc.stdout:
                self.hub_proc.stdout.close()
            if self.hub_proc.stderr:
                self.hub_proc.stderr.close()
        self._tmp.cleanup()

    def test_walkthrough_1_embodied_spatial_command_over_live_sse(self) -> None:
        origin_sdk, world, chat_log = build_spatial_demo_sdk(self.origin_url, slot_id="slot-spatial-1")
        core_sdk = CoreSDK(
            planner=HeuristicReferencePlanner(),
            allow_loopback=True,
            heartbeat_interval_sec=1.0,
        )
        try:
            scope = core_sdk.connect(self.core_url, "core-spatial", "slot-spatial-1")
            self.assertEqual(scope.slot_id, "slot-spatial-1")
            time.sleep(0.15)  # Allow SSE stream subscription to attach

            # Player holds E key (drinking potion) while speaking command -> companion moves & opens chest_R in parallel!
            self.assertTrue(world.player_e_key_busy)
            origin_sdk.report_player_command("打开右侧的宝箱")

            ok = _wait_until(lambda: "chest_R" in world.opened_chests and len(core_sdk.completed_receipts) >= 1)
            self.assertTrue(ok, f"Expected chest_R opened; receipts={core_sdk.completed_receipts}")
            self.assertEqual(core_sdk.completed_receipts[0].queue_state, "completed")
            self.assertTrue(any("AI=True" in entry for entry in chat_log))

            # Mandatory offline stop -> Hub closes session & CoreSDK terminates cleanly!
            origin_sdk.report_player_presence(False, remaining_online_humans_in_room=0)
            self.assertTrue(_wait_until(lambda: core_sdk.closed))
        finally:
            core_sdk.shutdown_session_cleanly()
            origin_sdk.stop_loopback_execute_listener()

    def test_genre_3_deduction_board_confidentiality_and_companion_overlay(self) -> None:
        origin_sdk, board = build_deduction_demo_sdk(self.origin_url, slot_id="slot-deduction-1")
        core_sdk = CoreSDK(planner=HeuristicReferencePlanner(), allow_loopback=True, heartbeat_interval_sec=1.0)
        try:
            core_sdk.connect(self.core_url, "core-detective", "slot-deduction-1")
            time.sleep(0.15)
            ctx = core_sdk.pull_context()
            # Secret culprit truth MUST NEVER leak into ProjectedContext!
            self.assertNotIn("butler_victor", json.dumps(ctx.state_brief))

            origin_sdk.report_player_command("帮我整理线索")
            ok = _wait_until(lambda: "clue_watch" in board.companion_pins)
            self.assertTrue(ok)
            self.assertEqual(board.remaining_attempts, 3)  # Companion overlay never spends player attempts!
        finally:
            core_sdk.shutdown_session_cleanly()
            origin_sdk.stop_loopback_execute_listener()

    def test_three_languages_csharp_origin_go_hub_python_core_and_dashboard(self) -> None:
        cs_dll = os.path.join(
            ROOT_DIR,
            "interaction-origin",
            "csharp",
            "G2A.InteractionOrigin.SelfTest",
            "bin",
            "Debug",
            "net8.0",
            "G2A.InteractionOrigin.SelfTest.dll",
        )
        if not os.path.exists(cs_dll):
            self.skipTest("C# SelfTest assembly not built yet")

        # 1. Verify Go Hub Trusted Security Web Dashboard serves anti-clickjacking HTML
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with no_proxy.open(f"{self.consent_url}/", timeout=3.0) as dash_resp:
            self.assertEqual(dash_resp.headers.get("X-Frame-Options"), "DENY")
            html = dash_resp.read().decode("utf-8")
            self.assertIn("G2A 智能枢纽控制台", html)

        # 2. Spawn live C# Interaction Origin process talking to Go Hub!
        cs_proc = subprocess.Popen(
            ["dotnet", cs_dll, "--live-hub", self.origin_url, "--slot", "slot-cs-live"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        db_path = os.path.join(self._tmp.name, "companion_e2e.db")
        sqlite_store = SQLiteCompanionStore(db_path)
        core_sdk = CoreSDK(
            planner=HeuristicReferencePlanner(),
            memory_store=sqlite_store,
            allow_loopback=True,
            heartbeat_interval_sec=1.0,
        )
        try:
            ready_line = cs_proc.stdout.readline().strip()
            ready_info = json.loads(ready_line)
            self.assertEqual(ready_info["status"], "ready")

            # 3. Connect Python Computation Core (backed by SQLite) -> Go Hub -> C# Origin!
            core_sdk.connect(self.core_url, "core-polyglot", "slot-cs-live")
            time.sleep(0.15)

            cs_proc.stdin.write("CMD:open right chest\n")
            cs_proc.stdin.flush()

            ok = _wait_until(lambda: len(core_sdk.completed_receipts) >= 1)
            self.assertTrue(ok, "Expected C# -> Go -> Python polyglot queue completion")
            self.assertEqual(core_sdk.completed_receipts[0].queue_state, "completed")

            cs_proc.stdin.write("STATUS\n")
            cs_proc.stdin.flush()
            status_info = json.loads(cs_proc.stdout.readline().strip())
            self.assertTrue(status_info["chest_opened"])
            self.assertGreaterEqual(status_info["chat_count"], 1)

            cs_proc.stdin.write("OFFLINE\n")
            cs_proc.stdin.flush()
            self.assertTrue(_wait_until(lambda: core_sdk.closed))
        finally:
            core_sdk.shutdown_session_cleanly()
            sqlite_store.close()
            cs_proc.terminate()
            try:
                cs_proc.wait(timeout=3)
            except Exception:
                cs_proc.kill()
            if cs_proc.stdin:
                cs_proc.stdin.close()
            if cs_proc.stdout:
                cs_proc.stdout.close()
            if cs_proc.stderr:
                cs_proc.stderr.close()

    def test_walkthrough_2_management_alert_and_trusted_single_prompt_confirmation(self) -> None:
        origin_sdk, sim = build_workshop_demo_sdk(self.origin_url, slot_id="slot-workshop-1")
        core_sdk = CoreSDK(
            planner=HeuristicReferencePlanner(),
            allow_loopback=True,
            heartbeat_interval_sec=1.0,
        )
        try:
            core_sdk.connect(self.core_url, "core-workshop", "slot-workshop-1")
            time.sleep(0.15)

            # Bench 1 initially running at baseline, then runs out of timber -> significant signal alert!
            sim.bench_running = True
            origin_sdk.notify_module_changed("workshop", significant_event=False)
            sim.bench_running = False
            sim.timber = 0
            origin_sdk.notify_module_changed("workshop", significant_event=True)

            held = _wait_until(lambda: len(core_sdk.pending_confirm_queues) == 1)
            self.assertTrue(held, "Expected proactive player-seat queue to wait in pending_confirm_queues")
            held_qid = next(iter(core_sdk.pending_confirm_queues.keys()))
            self.assertFalse(sim.bench_running)

            # Verify pending prompt appears on isolated Trusted Player Consent port, then approve once for the whole queue!
            no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with no_proxy.open(f"{self.consent_url}/trusted/pending", timeout=3.0) as resp:
                pending_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(pending_data["queue_prompts"]), 1)
            prompt = pending_data["queue_prompts"][0]
            self.assertEqual(prompt["queue_id"], held_qid)

            approve_req = urllib.request.Request(
                f"{self.consent_url}/trusted/decide-queue",
                data=json.dumps(
                    {
                        "session_id": prompt["session_id"],
                        "queue_id": held_qid,
                        "queue_digest": prompt["queue_digest"],
                        "fingerprint": prompt["fingerprint"],
                        "approved": True,
                    }
                ).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with no_proxy.open(approve_req, timeout=3.0):
                pass

            # CoreSDK receives queue.confirmed over SSE, resubmits SAME queue_id, buys 20 timber, and resumes Bench_1!
            done = _wait_until(lambda: sim.bench_running and len(core_sdk.completed_receipts) == 1)
            self.assertTrue(done, f"Expected Bench_1 resumed; sim={sim.snapshot()}")
            self.assertEqual(core_sdk.completed_receipts[0].queue_id, held_qid)
            self.assertEqual(sim.gold, 460)
            self.assertEqual(sim.timber, 0)
        finally:
            core_sdk.shutdown_session_cleanly()
            origin_sdk.stop_loopback_execute_listener()


if __name__ == "__main__":
    unittest.main()
