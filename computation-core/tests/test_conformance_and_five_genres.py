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

from g2a_core import CoreSDK, InMemoryCompanionStore, MemoryRecord
from g2a_core.cli import HeuristicReferencePlanner
from g2a_core.conformance import verify_all_14_error_codes_live
from g2a_origin.demo_host import (
    build_deduction_demo_sdk,
    build_strategy_card_demo_sdk,
    build_tactical_arena_outbound_sdk,
)


def _wait_until(predicate, timeout_sec: float = 5.0, step_sec: float = 0.05) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step_sec)
    return predicate()


class TestConformanceAndRemainingGenres(unittest.TestCase):
    def setUp(self) -> None:
        self.hub_bin = os.path.join(ROOT_DIR, "intelligent-hub", "bin", "intelligent-hub.exe")
        if not os.path.exists(self.hub_bin):
            self.skipTest("intelligent-hub binary not built yet")
        self._tmp = tempfile.TemporaryDirectory()
        ledger_path = os.path.join(self._tmp.name, "hub_conf_ledger.json")

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
        info = json.loads(self.hub_proc.stdout.readline().strip())
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

    def test_all_14_normative_error_codes_live_conformance(self) -> None:
        observed = verify_all_14_error_codes_live(self.core_url, self.origin_url)
        self.assertEqual(len(observed), 14)

    def test_genre_2_strategy_card_staged_mana_combo_and_fog_redaction(self) -> None:
        origin_sdk, sim = build_strategy_card_demo_sdk(self.origin_url, slot_id="slot-card-1")
        core_sdk = CoreSDK(planner=HeuristicReferencePlanner(), allow_loopback=True, heartbeat_interval_sec=1.0)
        try:
            core_sdk.connect(self.core_url, "core-card", "slot-card-1")
            time.sleep(0.15)
            ctx = core_sdk.pull_context()
            raw_ctx = json.dumps(ctx.state_brief)
            # Fog-of-war boss & undrawn deck order MUST NEVER leak!
            self.assertNotIn("hidden_dragon_north_99", raw_ctx)
            self.assertNotIn("meteor_strike", raw_ctx)

            # Player commands "combo": #03(mana_crystal) [ready] -> mana becomes 3 -> unlocks #03(fireball) [staged -> ready]!
            origin_sdk.report_player_command("执行出牌 combo")
            ok = _wait_until(lambda: sim.boss_hp == 12 and len(core_sdk.completed_receipts) == 1)
            self.assertTrue(ok, f"Expected fireball combo to deal 8 dmg; boss_hp={sim.boss_hp}")
        finally:
            core_sdk.shutdown_session_cleanly()
            origin_sdk.stop_loopback_execute_listener()

    def test_genre_5_tactical_arena_zero_inbound_port_outbound_stream_and_multimodal_toggle(self) -> None:
        origin_sdk, sim = build_tactical_arena_outbound_sdk(self.origin_url, slot_id="slot-arena-outbound")
        # Confirm ZERO inbound listener socket was opened in the game process!
        self.assertIsNone(origin_sdk._http_server)
        self.assertEqual(origin_sdk.origin_execute_url, "pull://origin.execute")

        mem_store = InMemoryCompanionStore()
        mem_store.upsert_player_record(
            "player-local",
            MemoryRecord("m-branch-b", 100, "game_observed", False, "same_game_companion", "Branch B spoiler", timeline_tag="branch_B"),
        )
        mem_store.upsert_player_record(
            "player-local",
            MemoryRecord("m-branch-a", 100, "game_observed", False, "same_game_companion", "Branch A safe", timeline_tag="branch_A"),
        )

        core_sdk = CoreSDK(
            planner=HeuristicReferencePlanner(),
            memory_store=mem_store,
            allow_loopback=True,
            heartbeat_interval_sec=1.0,
        )
        try:
            core_sdk.connect(
                self.core_url,
                "core-arena",
                "slot-arena-outbound",
                extensions={"org.g2a.narrative.timeline.v1": {"required": True}},
            )
            self.assertIn("org.g2a.narrative.timeline.v1", core_sdk.negotiated_extensions)
            time.sleep(0.15)

            # Attach visual frame & allowed timeline branch_A
            origin_sdk.set_allowed_timeline_tags(["branch_A"], flush=False)
            origin_sdk.attach_visual_frame({"media_id": "frame-1", "data_url": "data:image/png;base64,AAAA"}, flush=True)

            # Visual is OFF by default in scope -> must be stripped!
            time.sleep(0.15)
            ctx_off = core_sdk.pull_context()
            self.assertIsNone(ctx_off.visual_ref)
            self.assertEqual(ctx_off.allowed_timeline_tags, ["branch_A"])

            # Toggle allow_visual ON via Trusted Consent port -> visual_ref becomes visible!
            no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            toggle_req = urllib.request.Request(
                f"{self.consent_url}/trusted/toggle-modality",
                data=json.dumps({
                    "session_id": core_sdk.session_id,
                    "allow_visual": True,
                    "allow_audio": False,
                }).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with no_proxy.open(toggle_req, timeout=3.0):
                pass
            time.sleep(0.15)
            ctx_on = core_sdk.pull_context()
            self.assertEqual(ctx_on.visual_ref["media_id"], "frame-1")

            # Dispatch 2-step tactical queue over zero-inbound-port outbound stream!
            origin_sdk.report_player_command("掩护 cover")
            ok = _wait_until(lambda: sim.shield_active and "sector_B" in sim.pings)
            self.assertTrue(ok, f"Expected outbound stream execution; sim={sim.snapshot()}")
        finally:
            core_sdk.shutdown_session_cleanly()
            origin_sdk.stop_loopback_execute_listener()

    def test_zero_config_auto_discovery_workbench_all_five_genres(self) -> None:
        from g2a_core.workbench import run_three_tier_demo_workbench

        results = run_three_tier_demo_workbench("all")
        self.assertEqual(
            set(results.keys()),
            {"spatial", "strategy_card", "workshop", "deduction", "tactical_arena"},
        )


if __name__ == "__main__":
    unittest.main()
