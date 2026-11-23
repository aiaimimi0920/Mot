from __future__ import annotations

import argparse
import threading
import time
from typing import Any, Dict, List

from .origin_sdk import (
    ActiveMethodGrant,
    CapabilityEntry,
    ImmediateFailed,
    ImmediateSucceeded,
    InteractableRef,
    LoopbackHTTPTransportClient,
    OriginSDK,
    PlayerFocusInfo,
    Pose3D,
    StartedExecuting,
    StepExecutionToken,
    TargetCandidate,
)


class SpatialDemoWorld:
    def __init__(self) -> None:
        self.poses: Dict[str, Pose3D] = {
            "player": Pose3D(0.0, 0.0, 0.0),
            "companion": Pose3D(0.0, 0.0, 0.5),
        }
        self.player_e_key_busy = True  # Simulates player holding E key drinking potion!
        self.opened_chests: List[str] = []
        self.objects = [
            InteractableRef("chest_R", "Right Wooden Chest", Pose3D(4.0, 0.0, 0.0), interact_range=1.5),
            InteractableRef("secret_crypt", "Hidden Crypt", Pose3D(2.0, 0.0, 0.0), hidden_secret_unrevealed=True),
        ]

    def get_actor_pose(self, actor: str) -> Pose3D | None:
        return self.poses.get(actor)

    def get_player_focus(self) -> PlayerFocusInfo:
        return PlayerFocusInfo(pose=self.poses["player"], pointed_target_id=None)

    def query_nearby_interactables(self, origin: Pose3D, perception_radius: float) -> List[InteractableRef]:
        return [o for o in self.objects if o.id not in self.opened_chests]


def build_spatial_demo_sdk(hub_origin_url: str, slot_id: str = "slot-spatial") -> tuple[OriginSDK, SpatialDemoWorld, List[str]]:
    world = SpatialDemoWorld()
    chat_log: List[str] = []
    tx = LoopbackHTTPTransportClient(hub_origin_url)
    sdk = OriginSDK(
        slot_id=slot_id,
        hub_transport=tx,
        spatial=world,
        chat_ui_handler=lambda aud, txt, ai: chat_log.append(f"[{aud}|AI={ai}] {txt}"),
    )
    sdk.start_loopback_execute_listener()
    sdk.bind_actor_subject("player", world)
    sdk.bind_actor_subject("companion", world)

    def _move_effect(actor: str, subj: SpatialDemoWorld, args: Dict[str, Any], tok: StepExecutionToken):
        tid = args["target_id"]
        target = next((o for o in subj.objects if o.id == tid), None)
        if target is None:
            return ImmediateFailed("target_not_found")
        cancelled = {"flag": False}

        def _walk() -> None:
            time.sleep(0.05)
            if not cancelled["flag"]:
                subj.poses[actor] = Pose3D(target.pose.x, target.pose.y, target.pose.z)
                sdk.complete_step(tok.execution_id, True, {"arrived": tid})

        threading.Thread(target=_walk, daemon=True).start()
        return StartedExecuting(cancel_fn=lambda: cancelled.update(flag=True))

    def _interact_effect(actor: str, subj: SpatialDemoWorld, args: Dict[str, Any], tok: StepExecutionToken):
        tid = args["target_id"]
        # Direct domain effect: succeeds even while subj.player_e_key_busy == True!
        subj.opened_chests.append(tid)
        return ImmediateSucceeded({"opened": tid, "loot": ["gold_50", "potion_1"]})

    sdk.register_capability(
        CapabilityEntry("#01", "move_to", "Move to target", ["companion"], {"type": "object", "required": ["target_id"]}, sustained=True),
        effect_handler=_move_effect,
        spatial_target_verb=True,
        navigation_verb=True,
    )
    sdk.register_capability(
        CapabilityEntry("#02", "interact", "Open or use target", ["companion"], {"type": "object", "required": ["target_id"]}, sustained=False),
        effect_handler=_interact_effect,
        spatial_target_verb=True,
    )
    sdk.publish_capabilities_and_baseline()
    return sdk, world, chat_log


class WorkshopLedgerSim:
    """Avatar-less simulation/management module (modeled after Beaver signal+snapshot modules)."""

    def __init__(self) -> None:
        self.gold = 500
        self.timber = 0
        self.bench_running = False

    def snapshot(self) -> Dict[str, Any]:
        return {"gold": self.gold, "timber": self.timber, "bench_running": self.bench_running}

    def active_methods(self) -> List[ActiveMethodGrant]:
        grants = [
            ActiveMethodGrant("#05", "player", "ready" if self.gold >= 40 else "staged", []),
        ]
        if not self.bench_running:
            grants.append(
                ActiveMethodGrant(
                    "#06",
                    "player",
                    "ready" if self.timber >= 20 else "staged",
                    [
                        TargetCandidate(
                            target_id="Bench_1",
                            label="Carpentry Bench 1",
                            readiness="ready" if self.timber >= 20 else "staged",
                            prerequisite_hint=None if self.timber >= 20 else "requires_resource:timber",
                            alert_tag="halted_no_material" if self.timber < 20 else None,
                        )
                    ],
                )
            )
        return grants


def build_workshop_demo_sdk(hub_origin_url: str, slot_id: str = "slot-workshop") -> tuple[OriginSDK, WorkshopLedgerSim]:
    sim = WorkshopLedgerSim()
    tx = LoopbackHTTPTransportClient(hub_origin_url)
    sdk = OriginSDK(slot_id=slot_id, hub_transport=tx, spatial=None)
    sdk.start_loopback_execute_listener()
    sdk.bind_actor_subject("companion", {"overlay_pins": []})
    sdk.bind_actor_subject("player", sim)
    sdk.bind_state_module("workshop", sim.snapshot, sim.active_methods)

    def _trade_effect(actor: str, subj: WorkshopLedgerSim, args: Dict[str, Any], tok: StepExecutionToken):
        qty = int(args["qty"])
        cost = qty * 2
        if subj.gold < cost:
            return ImmediateFailed("insufficient_gold")
        subj.gold -= cost
        subj.timber += qty
        return ImmediateSucceeded({"bought_timber": qty, "remaining_gold": subj.gold})

    def _resume_effect(actor: str, subj: WorkshopLedgerSim, args: Dict[str, Any], tok: StepExecutionToken):
        if subj.timber < 20:
            return ImmediateFailed("insufficient_timber")
        subj.timber -= 20
        subj.bench_running = True
        return ImmediateSucceeded({"resumed": args["line_id"]})

    sdk.register_capability(
        CapabilityEntry("#05", "trade_resource", "Buy material", ["player"], {"type": "object", "required": ["item", "qty"]}),
        effect_handler=_trade_effect,
    )
    sdk.register_capability(
        CapabilityEntry("#06", "resume_line", "Resume halted line", ["player"], {"type": "object", "required": ["line_id"]}),
        effect_handler=_resume_effect,
    )
    sdk.publish_capabilities_and_baseline()
    return sdk, sim


class DeductionBoardSim:
    """Entity-less mystery deduction board: redacts unrevealed culprit truth from Origin snapshots."""

    def __init__(self) -> None:
        self._secret_culprit = "butler_victor"  # MUST NEVER leave Interaction Origin!
        self.unlocked_clues = {"clue_watch": "Broken pocket watch stopped at 23:15"}
        self.companion_pins: Dict[str, str] = {}
        self.remaining_attempts = 3
        self.solved = False

    def snapshot(self) -> Dict[str, Any]:
        # Strictly export ONLY unlocked clues & public counters — never _secret_culprit!
        return {
            "unlocked_clues": dict(self.unlocked_clues),
            "companion_pins": dict(self.companion_pins),
            "remaining_attempts": self.remaining_attempts,
            "solved": self.solved,
        }

    def active_methods(self) -> List[ActiveMethodGrant]:
        targets = [
            TargetCandidate(target_id=cid, label=desc, readiness="ready")
            for cid, desc in self.unlocked_clues.items()
        ]
        return [
            ActiveMethodGrant("#07", "companion", "ready", targets),
            ActiveMethodGrant("#08", "player", "ready" if self.remaining_attempts > 0 else "staged", []),
        ]


def build_deduction_demo_sdk(hub_origin_url: str, slot_id: str = "slot-deduction") -> tuple[OriginSDK, DeductionBoardSim]:
    board = DeductionBoardSim()
    tx = LoopbackHTTPTransportClient(hub_origin_url)
    sdk = OriginSDK(slot_id=slot_id, hub_transport=tx, spatial=None)
    sdk.start_loopback_execute_listener()
    sdk.bind_actor_subject("companion", board)
    sdk.bind_actor_subject("player", board)
    sdk.bind_state_module("deduction_board", board.snapshot, board.active_methods)

    def _mark_effect(actor: str, subj: DeductionBoardSim, args: Dict[str, Any], tok: StepExecutionToken):
        tid = str(args["target_id"])
        note = str(args.get("note", "highlighted"))
        subj.companion_pins[tid] = note  # Does NOT consume subj.remaining_attempts!
        return ImmediateSucceeded({"marked": tid, "remaining_attempts": subj.remaining_attempts})

    def _submit_answer_effect(actor: str, subj: DeductionBoardSim, args: Dict[str, Any], tok: StepExecutionToken):
        subj.remaining_attempts -= 1
        if str(args.get("suspect_id")) == subj._secret_culprit:
            subj.solved = True
            return ImmediateSucceeded({"solved": True})
        return ImmediateFailed("wrong_suspect")

    sdk.register_capability(
        CapabilityEntry("#07", "mark_board", "Highlight clue on companion overlay", ["companion"], {"type": "object", "required": ["target_id"]}),
        effect_handler=_mark_effect,
    )
    sdk.register_capability(
        CapabilityEntry("#08", "submit_answer", "Submit final culprit accusation", ["player"], {"type": "object", "required": ["suspect_id"]}, default_policy="confirm"),
        effect_handler=_submit_answer_effect,
    )
    sdk.publish_capabilities_and_baseline()
    return sdk, board


class StrategyCardSim:
    """Genre 2: Turn-based strategy & deckbuilder — redacts fog-of-war enemies & un-drawn deck order!"""

    def __init__(self) -> None:
        self._fog_of_war_boss = "hidden_dragon_north_99"
        self._undrawn_deck = ["meteor_strike", "divine_shield"]
        self.mana = 1
        self.hand = ["mana_crystal", "fireball"]
        self.boss_hp = 20

    def snapshot(self) -> Dict[str, Any]:
        return {"mana": self.mana, "hand": list(self.hand), "visible_boss_hp": self.boss_hp}

    def active_methods(self) -> List[ActiveMethodGrant]:
        targets: List[TargetCandidate] = []
        if "mana_crystal" in self.hand:
            targets.append(TargetCandidate("mana_crystal", "Mana Crystal (+2 Mana)", "ready"))
        if "fireball" in self.hand:
            targets.append(
                TargetCandidate(
                    "fireball",
                    "Fireball (Cost 3 Mana, 8 DMG)",
                    "ready" if self.mana >= 3 else "staged",
                    prerequisite_hint=None if self.mana >= 3 else "requires_mana:3",
                )
            )
        overall = "ready" if any(t.readiness == "ready" for t in targets) else "staged"
        return [
            ActiveMethodGrant("#03", "player", overall, targets),
            ActiveMethodGrant("#04", "companion", "ready", [TargetCandidate("hex_C4", "Flank Tile C4", "ready")]),
        ]


def build_strategy_card_demo_sdk(hub_origin_url: str, slot_id: str = "slot-strategy") -> tuple[OriginSDK, StrategyCardSim]:
    sim = StrategyCardSim()
    tx = LoopbackHTTPTransportClient(hub_origin_url)
    sdk = OriginSDK(slot_id=slot_id, hub_transport=tx, spatial=None)
    sdk.start_loopback_execute_listener()
    sdk.bind_actor_subject("companion", sim)
    sdk.bind_actor_subject("player", sim)
    sdk.bind_state_module("battle_board", sim.snapshot, sim.active_methods)

    def _play_card(actor: str, subj: StrategyCardSim, args: Dict[str, Any], tok: StepExecutionToken):
        cid = str(args["target_id"])
        if cid not in subj.hand:
            return ImmediateFailed("card_not_in_hand")
        if cid == "mana_crystal":
            subj.hand.remove("mana_crystal")
            subj.mana += 2
            return ImmediateSucceeded({"mana": subj.mana})
        if cid == "fireball":
            if subj.mana < 3:
                return ImmediateFailed("insufficient_mana")
            subj.hand.remove("fireball")
            subj.mana -= 3
            subj.boss_hp -= 8
            return ImmediateSucceeded({"boss_hp": subj.boss_hp})
        return ImmediateFailed("unknown_card")

    sdk.register_capability(
        CapabilityEntry("#03", "play_card", "Play card from hand", ["player"], {"type": "object", "required": ["target_id"]}),
        effect_handler=_play_card,
    )
    sdk.register_capability(
        CapabilityEntry("#04", "tactical_ping", "Ping tactical tile", ["companion"], {"type": "object", "required": ["target_id"]}),
        effect_handler=lambda a, s, args, t: ImmediateSucceeded({"pinged": args["target_id"]}),
    )
    sdk.publish_capabilities_and_baseline()
    return sdk, sim


class TacticalArenaSim:
    """Genre 5: Competitive tactical co-op — runs in zero-inbound-port outbound stream mode with FRAND cooldown!"""

    def __init__(self) -> None:
        self.pings: List[str] = []
        self.shield_active = False

    def snapshot(self) -> Dict[str, Any]:
        return {"pings": list(self.pings), "shield_active": self.shield_active}

    def active_methods(self) -> List[ActiveMethodGrant]:
        return [
            ActiveMethodGrant("#10", "companion", "ready", [TargetCandidate("sector_B", "Sector B Ridge", "ready")]),
            ActiveMethodGrant("#11", "companion", "ready" if not self.shield_active else "staged", []),
        ]


def build_tactical_arena_outbound_sdk(hub_origin_url: str, slot_id: str = "slot-arena") -> tuple[OriginSDK, TacticalArenaSim]:
    sim = TacticalArenaSim()
    tx = LoopbackHTTPTransportClient(hub_origin_url)
    sdk = OriginSDK(slot_id=slot_id, hub_transport=tx, spatial=None)
    # Zero inbound port: uses outbound SSE command stream ("pull://origin.execute")!
    sdk.start_outbound_execute_stream(hub_origin_url)
    sdk.bind_actor_subject("companion", sim)
    sdk.bind_state_module("tactical_hud", sim.snapshot, sim.active_methods)

    sdk.register_capability(
        CapabilityEntry("#10", "ping_map", "Ping visible tactical sector", ["companion"], {"type": "object", "required": ["target_id"]}),
        effect_handler=lambda a, s, args, t: (s.pings.append(str(args["target_id"])) or ImmediateSucceeded({"pinged": args["target_id"]})),
    )
    sdk.register_capability(
        CapabilityEntry("#11", "trigger_support_skill", "Activate squad barrier", ["companion"], {"type": "object"}),
        effect_handler=lambda a, s, args, t: (setattr(s, "shield_active", True) or ImmediateSucceeded({"shield_active": True})),
    )
    sdk.publish_capabilities_and_baseline()
    return sdk, sim


def main() -> None:
    parser = argparse.ArgumentParser(description="G2A V0.0.1 Interaction Origin Demo Host (All 5 Genres)")
    parser.add_argument("--hub-origin", default="http://127.0.0.1:19210", help="Hub Origin Interface URL")
    parser.add_argument(
        "--mode",
        choices=["spatial", "strategy_card", "workshop", "deduction", "tactical_arena"],
        default="spatial",
    )
    args = parser.parse_args()

    if args.mode == "spatial":
        sdk, _, _ = build_spatial_demo_sdk(args.hub_origin)
    elif args.mode == "strategy_card":
        sdk, _ = build_strategy_card_demo_sdk(args.hub_origin)
    elif args.mode == "workshop":
        sdk, _ = build_workshop_demo_sdk(args.hub_origin)
    elif args.mode == "deduction":
        sdk, _ = build_deduction_demo_sdk(args.hub_origin)
    else:
        sdk, _ = build_tactical_arena_outbound_sdk(args.hub_origin)
    print(f"[G2A Origin V0.0.1] Running mode={args.mode} slot={sdk.slot_id} execute_url={sdk.origin_execute_url}")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        sdk.report_player_presence(False, remaining_online_humans_in_room=0)
        sdk.stop_loopback_execute_listener()


if __name__ == "__main__":
    main()
