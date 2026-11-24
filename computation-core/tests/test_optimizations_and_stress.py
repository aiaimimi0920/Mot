from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ORIGIN_PY_SRC = os.path.join(ROOT_DIR, "interaction-origin", "python", "src")
CORE_PY_SRC = os.path.join(ROOT_DIR, "computation-core", "src")
for p in (ORIGIN_PY_SRC, CORE_PY_SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

from g2a_core.core_sdk import CoreSDK
from g2a_core.memory import MemoryRecord, SpoilerGuard
from g2a_core.playground import resolve_hub_binary
from g2a_core.schemas import MethodCallQueue, PlanDecision, QueueStep
from g2a_core.sqlite_memory import SQLiteCompanionStore
from g2a_origin.demo_host import build_spatial_demo_sdk


class _NoOpPlanner:
    def plan(self, capabilities, context, recalled_memories):
        return PlanDecision()


class TestOptimizationsAndStress(unittest.TestCase):
    def test_save_reload_epoch_spoiler_guard_and_ranked_sqlite_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "ranked_companion.db")
            store = SQLiteCompanionStore(db_path)
            try:
                now = int(time.time() * 1000)
                # 1. Insert 60 unpinned filler memories
                for i in range(60):
                    store.upsert_player_record(
                        "player-1",
                        MemoryRecord(
                            memory_id=f"mem-bg-{i:02d}",
                            created_at_ms=now + i,
                            epistemic_source="agent_summary",
                            pinned_by_player=False,
                            max_disclosure_scope="same_game_companion",
                            game_origin_id="game-rpg",
                            content=f"Routine patrol log entry number {i}",
                        ),
                    )

                # 2. Insert a future-epoch game_observed spoiler (observed at epoch=5)
                store.upsert_player_record(
                    "player-1",
                    MemoryRecord(
                        memory_id="mem-future-spoiler",
                        created_at_ms=now + 500,
                        epistemic_source="game_observed",
                        pinned_by_player=False,
                        max_disclosure_scope="same_game_companion",
                        game_origin_id="game-rpg",
                        content="Chest mimic ambush in throne room",
                        observed_epoch=5,
                    ),
                )

                # 3. Insert a player-pinned strategic rule and a keyword-relevant chest note
                store.upsert_player_record(
                    "player-1",
                    MemoryRecord(
                        memory_id="mem-pinned-rule",
                        created_at_ms=now - 1000,
                        epistemic_source="player_stated",
                        pinned_by_player=True,
                        max_disclosure_scope="same_game_companion",
                        game_origin_id="game-rpg",
                        content="Always conserve healing potions before boss doors",
                    ),
                )
                store.upsert_player_record(
                    "player-1",
                    MemoryRecord(
                        memory_id="mem-chest-keyword",
                        created_at_ms=now + 100,
                        epistemic_source="game_observed",
                        pinned_by_player=False,
                        max_disclosure_scope="same_game_companion",
                        game_origin_id="game-rpg",
                        content="Silver key opens locked right chest in hall",
                        observed_epoch=2,
                    ),
                )

                # Reloaded earlier save at current_epoch=2 -> future epoch=5 spoiler MUST be suppressed!
                recalled = store.recall_safe(
                    "player-1",
                    SpoilerGuard(
                        current_epoch=2,
                        game_origin_id="game-rpg",
                        query_hint="open right chest",
                        max_results=48,
                    ),
                )
                ids = [r.memory_id for r in recalled]
                self.assertEqual(len(recalled), 48)
                self.assertNotIn("mem-future-spoiler", ids)
                self.assertEqual(ids[0], "mem-pinned-rule")
                self.assertEqual(ids[1], "mem-chest-keyword")

                # Once timeline advances to epoch=5, the observed_epoch=5 memory becomes safe to recall
                recalled_ep5 = store.recall_safe(
                    "player-1",
                    SpoilerGuard(current_epoch=5, game_origin_id="game-rpg", query_hint="throne mimic"),
                )
                self.assertIn("mem-future-spoiler", [r.memory_id for r in recalled_ep5])
            finally:
                store.close()

    def test_proactive_refusal_cooldown_client_shield_and_keepalive_burst(self) -> None:
        # 1. Verify CoreSDK suppresses proactive re-submissions locally during refusal cooldown without network calls
        calls = []

        def _counting_post(endpoint, op, payload, token, sid):
            calls.append(op)
            return {
                "queue_id": payload.get("queue_id", "Q-1"),
                "queue_state": "completed",
                "step_receipts": [],
            }

        sdk = CoreSDK(planner=_NoOpPlanner(), custom_post_fn=_counting_post)
        sdk.session_id = "sess-local"
        step = QueueStep(0, "#05", "player", {"item": "timber", "qty": 20})
        q_declined = MethodCallQueue("Q-dec", 1, 1, "proactive", False, "Buy timber", [step])
        sdk.pending_confirm_queues["Q-dec"] = q_declined
        sdk.on_event_queue_declined("Q-dec", cooldown_ms=60000)

        # Proactive retry with identical target fingerprint is suppressed locally (0 network calls!)
        q_retry_proactive = MethodCallQueue("Q-pro-2", 1, 2, "proactive", False, "Buy timber again", [step])
        self.assertIsNone(sdk.submit_queue_with_standard_recovery(q_retry_proactive))
        self.assertEqual(calls, [])

        # Explicit player_command with the same steps bypasses proactive cooldown as intended!
        q_explicit = MethodCallQueue("Q-cmd-3", 1, 2, "player_command", True, "Player ordered buy", [step])
        rec = sdk.submit_queue_with_standard_recovery(q_explicit)
        self.assertIsNotNone(rec)
        self.assertEqual(calls, ["queue.submit"])

        # 2. Live HTTP/1.1 Keep-Alive high-frequency 100-tick burst across Python Origin -> Go Hub <- Python Core
        hub_bin = resolve_hub_binary(ROOT_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "burst_ledger.json")
            runtime_path = os.path.join(tmp, "burst_runtime.json")
            hub_proc = subprocess.Popen(
                [
                    hub_bin,
                    "-origin-addr", "127.0.0.1:0",
                    "-core-addr", "127.0.0.1:0",
                    "-consent-addr", "127.0.0.1:0",
                    "-ledger", ledger_path,
                    "-runtime-file", runtime_path,
                    "-auto-approve-scope",
                    "-rate-limit-per-sec", "2000",
                    "-print-ports-json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert hub_proc.stdout is not None
                ports = json.loads(hub_proc.stdout.readline().strip())
                origin_url = f"http://{ports['origin_addr']}"
                core_url = ports["core_url"]

                orig_sdk, world, _ = build_spatial_demo_sdk(origin_url, slot_id="slot-burst")
                live_core = CoreSDK(planner=_NoOpPlanner(), allow_loopback=True)
                try:
                    live_core.connect(core_url, "core-burst", "slot-burst", start_background_loops=False)
                    t0 = time.perf_counter()
                    for tick in range(1, 101):
                        world.poses["player"].x = float(tick) * 0.1
                        orig_sdk.flush_dirty_sync(significant_event=(tick % 25 == 0))
                        if tick % 10 == 0:
                            live_core._post_envelope("session.heartbeat", {}, session_id=live_core.session_id)
                    elapsed = time.perf_counter() - t0
                    self.assertLess(elapsed, 2.5)
                    ctx = live_core.pull_context()
                    self.assertEqual(ctx.rev, 101)
                finally:
                    live_core.shutdown_session_cleanly(notify_hub=True)
                    orig_sdk.stop_loopback_execute_listener()
            finally:
                hub_proc.terminate()
                hub_proc.wait(timeout=3)
                if hub_proc.stdout:
                    hub_proc.stdout.close()
                if hub_proc.stderr:
                    hub_proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
