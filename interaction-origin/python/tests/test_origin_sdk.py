import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from g2a_origin import (
    ActiveMethodGrant,
    CapabilityEntry,
    ImmediateSucceeded,
    InteractableRef,
    OriginExecuteRequest,
    OriginSDK,
    PlayerFocusInfo,
    Pose3D,
    StartedExecuting,
    TargetCandidate,
)


class RecordingTransport:
    def __init__(self):
        self.posts = []

    def post_to_hub(self, op, payload):
        self.posts.append((op, payload))
        return {"ok": True}


class MockSpatialWorld:
    def __init__(self):
        self.poses = {
            "player": Pose3D(0.0, 0.0, 0.0),
            "companion": Pose3D(0.0, 0.0, 1.0),
        }
        self.objects = [
            InteractableRef("chest_R", "Wooden Chest", Pose3D(4.0, 0.0, 0.0), interact_range=1.5),
            InteractableRef("secret_door_99", "Secret Door", Pose3D(1.0, 0.0, 0.0), hidden_secret_unrevealed=True),
        ]

    def get_actor_pose(self, actor):
        return self.poses.get(actor)

    def get_player_focus(self):
        return PlayerFocusInfo(pose=self.poses["player"], pointed_target_id=None)

    def query_nearby_interactables(self, origin, perception_radius):
        return list(self.objects)


class TestInteractionOriginPythonSDK(unittest.TestCase):
    def test_zero_change_zero_packet_and_confidentiality_redaction(self):
        tx = RecordingTransport()
        world = MockSpatialWorld()
        sdk = OriginSDK(slot_id="slot-1", hub_transport=tx, spatial=world)
        sdk.bind_actor_subject("player", {"hotkey_e_locked": True})
        sdk.bind_actor_subject("companion", {"opened": []})

        sdk.register_capability(
            CapabilityEntry("#01", "move_to", "Move", ["companion"], {"required": ["target_id"]}, sustained=True),
            effect_handler=lambda act, subj, args, tok: StartedExecuting(lambda: None),
            spatial_target_verb=True,
        )
        sdk.register_capability(
            CapabilityEntry("#02", "interact", "Open", ["companion"], {"required": ["target_id"]}, sustained=False),
            effect_handler=lambda act, subj, args, tok: ImmediateSucceeded({"opened": args["target_id"]}),
            spatial_target_verb=True,
        )
        sdk.publish_capabilities_and_baseline()
        self.assertEqual(len(tx.posts), 2)  # origin.capabilities + initial full origin.sync

        # Hidden secret door MUST NEVER appear in synced methods!
        sync_payload = tx.posts[-1][1]
        all_targets = [
            t["target_id"]
            for m in sync_payload["upserted_methods"]
            for t in m["targets"]
        ]
        self.assertIn("chest_R", all_targets)
        self.assertNotIn("secret_door_99", all_targets)

        # Calling flush_dirty_sync when nothing changed MUST emit ZERO packets!
        emitted = sdk.flush_dirty_sync()
        self.assertFalse(emitted)
        self.assertEqual(len(tx.posts), 2)

    def test_sustained_step_atomic_settlement_and_cancel(self):
        tx = RecordingTransport()
        world = MockSpatialWorld()
        sdk = OriginSDK(slot_id="slot-1", hub_transport=tx, spatial=world)
        sdk.bind_actor_subject("companion", {})
        cancelled = {"called": False}

        sdk.register_capability(
            CapabilityEntry("#01", "move_to", "Move", ["companion"], {"required": ["target_id"]}, sustained=True),
            effect_handler=lambda act, subj, args, tok: StartedExecuting(lambda: cancelled.update(called=True)),
            spatial_target_verb=True,
        )
        sdk.register_capability(
            CapabilityEntry("#02", "interact", "Open", ["companion"], {"required": ["target_id"]}),
            effect_handler=lambda act, subj, args, tok: ImmediateSucceeded({"loot": ["gold_50"]}),
            spatial_target_verb=True,
        )
        sdk.publish_capabilities_and_baseline()

        # Start sustained move_to -> returns executing
        rep = sdk.handle_origin_execute(
            OriginExecuteRequest("slot-1", 1, "invoke", "exec-1", "#01", "companion", {"target_id": "chest_R"})
        )
        self.assertEqual(rep.state, "executing")

        # Companion arrives next to chest_R and calls complete_step -> single atomic delta frame carries BOTH ready chest_R AND settled exec-1!
        world.poses["companion"] = Pose3D(4.0, 0.0, 0.0)
        sdk.complete_step("exec-1", succeeded=True, summary={"arrived": "chest_R"})
        last_op, last_frame = tx.posts[-1]
        self.assertEqual(last_op, "origin.sync")
        self.assertEqual(last_frame["settled_executions"][0]["execution_id"], "exec-1")
        interact_grants = [m for m in last_frame["upserted_methods"] if m["method_id"] == "#02"]
        self.assertEqual(interact_grants[0]["targets"][0]["readiness"], "ready")

        # Test mid-flight cancellation on another sustained step
        sdk.handle_origin_execute(
            OriginExecuteRequest("slot-1", 1, "invoke", "exec-2", "#01", "companion", {"target_id": "chest_R"})
        )
        cancel_rep = sdk.handle_origin_execute(OriginExecuteRequest("slot-1", 1, "cancel", "exec-2"))
        self.assertTrue(cancelled["called"])
        self.assertEqual(cancel_rep.state, "cancelled")

    def test_hybrid_module_merge_and_independent_npc_strip_player(self):
        tx = RecordingTransport()
        sdk = OriginSDK(slot_id="npc-bartender", hub_transport=tx, slot_role_kind="independent_npc")
        sdk.register_capability(
            CapabilityEntry("#05", "serve", "Serve drink", ["companion", "player"], {}),
            effect_handler=lambda a, s, args, t: ImmediateSucceeded({}),
        )
        self.assertNotIn("player", sdk.capabilities["#05"].definition.permitted_actors)

        sdk.bind_state_module(
            "bar_a",
            lambda: {"stock": 5},
            lambda: [ActiveMethodGrant("#05", "companion", "staged", [TargetCandidate("t1", "Table 1", "staged")])],
        )
        sdk.bind_state_module(
            "bar_b",
            lambda: {"open": True},
            lambda: [ActiveMethodGrant("#05", "companion", "ready", [TargetCandidate("t2", "Table 2", "ready")])],
        )
        _, grants = sdk.collect_current_view()
        merged = grants[("#05", "companion")]
        self.assertEqual(merged.readiness, "ready")
        self.assertEqual({t.target_id for t in merged.targets}, {"t1", "t2"})

    def test_dense_crowd_priority_sorting_and_bounded_execution_ledger(self):
        tx = RecordingTransport()
        sdk = OriginSDK(slot_id="slot-crowd", hub_transport=tx)
        sdk.bind_actor_subject("companion", object())
        sdk.register_capability(
            CapabilityEntry("#02", "interact", "Interact", ["companion"], {}),
            effect_handler=lambda a, s, args, tok: (
                StartedExecuting(lambda: None)
                if args.get("hold")
                else ImmediateSucceeded({"ok": True})
            ),
        )

        # Build 60 candidate targets: distant staged chests 00..54, close ready chest_55, pointed chest_58, alerted chest_59
        crowd = [
            TargetCandidate(f"chest_{i:02d}", f"Chest {i}", "staged", actor_anchor="22.0m")
            for i in range(55)
        ]
        crowd.append(TargetCandidate("chest_55", "Nearby Ready Chest", "ready", actor_anchor="1.2m"))
        crowd.append(TargetCandidate("chest_58", "Pointed Chest", "staged", actor_anchor="9.0m", player_anchor="pointed_ray"))
        crowd.append(TargetCandidate("chest_59", "Alerted Station", "staged", actor_anchor="14.0m", alert_tag="halted_no_material"))

        sdk.bind_state_module(
            "crowd_zone",
            lambda: {"count": len(crowd)},
            lambda: [ActiveMethodGrant("#02", "companion", "ready", crowd)],
        )
        _, grants = sdk.collect_current_view()
        top_targets = grants[("#02", "companion")].targets
        self.assertEqual(len(top_targets), 24)
        self.assertEqual(top_targets[0].target_id, "chest_59")  # Alert tag #1
        self.assertEqual(top_targets[1].target_id, "chest_58")  # Player pointed #2
        self.assertEqual(top_targets[2].target_id, "chest_55")  # Ready & closest (1.2m) #3

        # Start one sustained in-flight step and execute 600 immediate steps -> bounded at 512 while preserving in-flight!
        r_hold = sdk.handle_origin_execute(
            OriginExecuteRequest("slot-crowd", 1, "invoke", "exec-hold-inflight", "#02", "companion", {"hold": True})
        )
        self.assertEqual(r_hold.state, "executing")
        for idx in range(600):
            sdk.handle_origin_execute(
                OriginExecuteRequest("slot-crowd", 1, "invoke", f"exec-fast-{idx}", "#02", "companion", {})
            )
        self.assertEqual(len(sdk.execution_ledger), 512)
        self.assertIn("exec-hold-inflight", sdk.execution_ledger)

        # Emergency stop settles all in-flight steps atomically into pending_settled_executions
        sdk.report_emergency_stop()
        self.assertEqual(sdk.execution_ledger["exec-hold-inflight"].state, "cancelled")


if __name__ == "__main__":
    unittest.main()
