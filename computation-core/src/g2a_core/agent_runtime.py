"""
AI Agent Task Completion Engine for G2A V0.0.1 Computation Core:
  1. Bilingual (EN/ZH) 360° Deictic & Alert Spatial Target Grounding (`resolve_deictic_target`);
  2. Automatic Causal Prerequisite Unrolling (`resolve_prerequisite_steps`: staged -> ready chains);
  3. Closed-Loop Multi-Turn `AgentGoalTracker` with StepReceipt feedback, sustained-step waiting,
     anti-repeat failure cycle detection, and timeline-isolated episodic memory recording.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .jcs import jcs_sha256
from .memory import InMemoryCompanionStore, MemoryRecord
from .schemas import (
    ActiveMethodGrant,
    CapabilityEntry,
    PlanDecision,
    ProjectedContext,
    QueueReceipt,
    StepReceipt,
    TargetCandidate,
)

MAX_RECENT_OUTCOMES = 16

_EXPLICIT_RAY_KEYWORDS = ("pointed", "pointing", "crosshair", "ray", "指向", "指着", "准星")
_SOFT_DEMONSTRATIVE_KEYWORDS = ("this", "that", "here", "这个", "那个", "这儿", "那儿", "眼前")
_ALERT_KEYWORDS = ("alert", "halted", "broken", "stalled", "warning", "stop", "报警", "故障", "停机", "缺料", "卡住", "异常", "损坏")
_DIR_PATTERNS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("right", ("right", "右侧", "右边", "右方", "右面"), "left"),
    ("left", ("left", "左侧", "左边", "左方", "左面"), "right"),
    ("ahead", ("ahead", "front", "forward", "前方", "前面", "正前"), "behind"),
    ("behind", ("behind", "back", "rear", "后方", "后面", "身后", "背后"), "ahead"),
)
_NAV_KEYWORDS = ("move", "walk", "goto", "navigate", "approach", "travel", "移动", "前往", "靠近", "走")
_PROXIMITY_HINTS = ("proximity", "move", "closer", "approach", "distance", "range", "靠近", "距离", "走近")


def _parse_anchor_meters(anchor: Optional[str]) -> float:
    if not anchor:
        return 999.0
    s = anchor.strip()
    if not s.endswith("m"):
        return 999.0
    num_part = s[:-1].rsplit(" ", 1)[-1]
    try:
        v = float(num_part)
        return v if v >= 0 else 999.0
    except ValueError:
        return 999.0


def _extract_query_tokens(text: str) -> List[str]:
    lowered = text.lower()
    ascii_words = [w for w in re.findall(r"[a-z0-9_]{2,}", lowered) if w not in {"the", "and", "open", "use", "to", "at", "on"}]
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    cjk_bigrams: List[str] = []
    for run in cjk_runs:
        for ch_stop in ("打开", "开启", "使用", "前往", "帮我", "去把", "那个", "这个", "右边", "左边", "前面", "后面", "右侧", "左侧", "前方", "后方"):
            run = run.replace(ch_stop, " ")
        for seg in run.split():
            if len(seg) >= 2:
                cjk_bigrams.append(seg)
    return ascii_words + cjk_bigrams


def rank_deictic_candidates(
    utterance: str,
    candidates: Sequence[TargetCandidate],
    prefer_ready: bool = False,
) -> List[TargetCandidate]:
    """Scores and ranks `TargetCandidate`s against natural-language spatial deictics, alerts, labels, and distance."""
    if not candidates:
        return []
    q = (utterance or "").lower()
    wants_explicit_ray = any(k in q for k in _EXPLICIT_RAY_KEYWORDS)
    wants_soft_demo = any(k in q for k in _SOFT_DEMONSTRATIVE_KEYWORDS)
    alert_hit_count = sum(1 for k in _ALERT_KEYWORDS if k in q)
    tokens = _extract_query_tokens(q)

    scored: List[Tuple[float, int, TargetCandidate]] = []
    for idx, cand in enumerate(candidates):
        score = 0.0
        p_anchor = (cand.player_anchor or "").lower()
        haystack = f"{cand.target_id} {cand.label} {cand.alert_tag or ''}".lower()

        if "pointed" in p_anchor:
            if wants_explicit_ray:
                score += 68.0
            elif wants_soft_demo:
                score += 26.0
            else:
                score += 8.0

        for canonical_dir, dir_words, opposite_dir in _DIR_PATTERNS:
            if any(dw in q for dw in dir_words):
                if canonical_dir in p_anchor:
                    score += 48.0
                elif opposite_dir in p_anchor:
                    score -= 25.0

        if cand.alert_tag:
            score += (60.0 + 10.0 * min(alert_hit_count, 3)) if alert_hit_count > 0 else 10.0

        for tok in tokens:
            if tok in haystack:
                score += 30.0

        if cand.readiness == "ready":
            score += 22.0 if prefer_ready else 5.0

        meters = _parse_anchor_meters(cand.actor_anchor)
        if meters < 900.0:
            score += max(0.0, 20.0 - meters) * 0.4

        scored.append((score, -idx, cand))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored]


def resolve_deictic_target(
    utterance: str,
    candidates: Sequence[TargetCandidate],
    prefer_ready: bool = False,
) -> Optional[TargetCandidate]:
    ranked = rank_deictic_candidates(utterance, candidates, prefer_ready=prefer_ready)
    return ranked[0] if ranked else None


def resolve_prerequisite_steps(
    method_id: str,
    actor: str,
    args: Optional[Dict[str, Any]],
    context: ProjectedContext,
    capabilities: Sequence[CapabilityEntry],
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """
    Automatically unrolls a `"staged"` target/method into a valid executable `[ready_prerequisite, ..., target_step]`
    chain using `prerequisite_hint`, navigation capability matching, or same-actor resource unlock grants.
    """
    step_args = dict(args or {})
    grant_map: Dict[Tuple[str, str], ActiveMethodGrant] = {(g.method_id, g.actor): g for g in context.active_methods}
    cap_map: Dict[str, CapabilityEntry] = {c.method_id: c for c in capabilities}

    grant = grant_map.get((method_id, actor))
    if grant is None:
        return [(method_id, actor, step_args)]

    cand: Optional[TargetCandidate] = None
    tid = step_args.get("target_id")
    if isinstance(tid, str) and grant.targets:
        cand = next((t for t in grant.targets if t.target_id == tid), None)

    effective_readiness = cand.readiness if cand is not None else grant.readiness
    if effective_readiness == "ready":
        return [(method_id, actor, step_args)]

    hint = ((cand.prerequisite_hint if cand is not None else None) or "").strip()
    hint_lower = hint.lower()

    # 1. Explicit method reference in prerequisite_hint (e.g. "requires_method:#01" or "#01" or capability name)
    explicit_m = re.search(r"(#[0-9a-zA-Z_-]+)", hint)
    if explicit_m:
        pre_mid = explicit_m.group(1)
        pre_grant = grant_map.get((pre_mid, actor))
        if pre_grant is not None and pre_grant.readiness == "ready":
            pre_args: Dict[str, Any] = {}
            pre_cap = cap_map.get(pre_mid)
            if isinstance(tid, str) and (
                any(t.target_id == tid and t.readiness == "ready" for t in pre_grant.targets)
                or (pre_cap and "target_id" in (pre_cap.params_schema.get("properties") or {}))
            ):
                pre_args["target_id"] = tid
            return [(pre_mid, actor, pre_args), (method_id, actor, step_args)]

    # 2. Spatial proximity prerequisite -> prepend a ready navigation verb for the same actor & target_id
    is_proximity = (not hint_lower) or any(pk in hint_lower for pk in _PROXIMITY_HINTS)
    if is_proximity and isinstance(tid, str):
        for (cand_mid, cand_actor), cand_grant in grant_map.items():
            if cand_actor != actor or cand_mid == method_id or cand_grant.readiness != "ready":
                continue
            c_def = cap_map.get(cand_mid)
            name_desc = f"{c_def.name} {c_def.description}".lower() if c_def else ""
            accepts_target = bool(
                any(t.target_id == tid and t.readiness == "ready" for t in cand_grant.targets)
                or (
                    not cand_grant.targets
                    and c_def is not None
                    and ("target_id" in (c_def.params_schema.get("properties") or {}) or "target_id" in (c_def.params_schema.get("required") or []))
                )
            )
            if accepts_target and (any(nk in name_desc for nk in _NAV_KEYWORDS) or any(t.target_id == tid for t in cand_grant.targets)):
                return [(cand_mid, actor, {"target_id": tid}), (method_id, actor, step_args)]

    # 3. Same-method ready resource unlock target (e.g. Deckbuilder: play ready `mana_crystal` before staged `fireball`)
    if isinstance(tid, str) and grant.targets:
        ready_sibling = next(
            (
                t
                for t in grant.targets
                if t.readiness == "ready"
                and t.target_id != tid
                and (not hint_lower or t.target_id.lower() in hint_lower or t.label.lower() in hint_lower)
            ),
            None,
        )
        if ready_sibling is not None:
            return [(method_id, actor, {"target_id": ready_sibling.target_id}), (method_id, actor, step_args)]

    return [(method_id, actor, step_args)]


@dataclass
class ActionOutcomeRecord:
    turn: int
    queue_id: str
    step_index: int
    method_id: str
    actor: str
    args: Dict[str, Any]
    state: str
    effect_summary: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None
    context_rev: int = 0

    def to_prompt_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "turn": self.turn,
            "method_id": self.method_id,
            "actor": self.actor,
            "args": self.args,
            "state": self.state,
        }
        if self.effect_summary:
            out["effect_summary"] = self.effect_summary
        if self.failure_reason:
            out["failure_reason"] = self.failure_reason
        return out


@dataclass
class AgentGoalState:
    goal_id: str
    objective: str
    status: str = "in_progress"  # "in_progress" | "awaiting_execution" | "completed" | "blocked" | "exhausted"
    max_turns: int = 6
    turns_used: int = 0
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    submitted_queues: List[str] = field(default_factory=list)
    recent_outcomes: List[ActionOutcomeRecord] = field(default_factory=list)
    failed_action_fingerprints: Set[str] = field(default_factory=set)
    last_failure_reason: Optional[str] = None


@dataclass
class GoalExecutionReport:
    goal_id: str
    objective: str
    succeeded: bool
    status: str
    turns_used: int
    submitted_queues: List[str]
    outcomes: List[ActionOutcomeRecord]
    final_context: Optional[ProjectedContext] = None
    failure_reason: Optional[str] = None


class AgentGoalTracker:
    """
    Thread-safe episodic execution tracker maintaining active AI Agent objectives,
    step-outcome feedback history, anti-loop failure fingerprints, and spoiler-safe observations.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.active_goal: Optional[AgentGoalState] = None
        self.recent_outcomes: List[ActionOutcomeRecord] = []
        self._failed_fingerprints: Set[str] = set()

    @staticmethod
    def compute_action_state_fingerprint(
        state_brief: Dict[str, Any],
        method_id: str,
        actor: str,
        args: Dict[str, Any],
    ) -> str:
        return jcs_sha256(
            "g2a.agent.loop.v1",
            {"brief": state_brief, "method_id": method_id, "actor": actor, "args": args},
        )

    def start_goal(self, objective: str, max_turns: int = 6) -> AgentGoalState:
        with self._lock:
            goal = AgentGoalState(
                goal_id=f"goal-{uuid.uuid4().hex[:10]}",
                objective=objective,
                status="in_progress",
                max_turns=max(1, max_turns),
            )
            self.active_goal = goal
            return goal

    def clear_goal(self, status: str = "completed") -> None:
        with self._lock:
            if self.active_goal is not None:
                self.active_goal.status = status
                self.active_goal = None

    def is_failed_repeat(
        self,
        state_brief: Dict[str, Any],
        method_id: str,
        actor: str,
        args: Dict[str, Any],
    ) -> bool:
        fp = self.compute_action_state_fingerprint(state_brief, method_id, actor, args)
        with self._lock:
            if self.active_goal is not None and fp in self.active_goal.failed_action_fingerprints:
                return True
            return fp in self._failed_fingerprints

    def record_receipt(
        self,
        receipt: QueueReceipt,
        submitted_steps: Sequence[Tuple[str, str, Dict[str, Any]]],
        context_at_submit: Optional[ProjectedContext],
    ) -> None:
        with self._lock:
            turn_idx = self.active_goal.turns_used if self.active_goal is not None else 0
            if self.active_goal is not None and receipt.queue_id not in self.active_goal.submitted_queues:
                self.active_goal.submitted_queues.append(receipt.queue_id)

            for sr in receipt.step_receipts:
                step_args: Dict[str, Any] = {}
                if 0 <= sr.step_index < len(submitted_steps):
                    step_args = dict(submitted_steps[sr.step_index][2])
                rec = ActionOutcomeRecord(
                    turn=turn_idx,
                    queue_id=receipt.queue_id,
                    step_index=sr.step_index,
                    method_id=sr.method_id,
                    actor=sr.actor,
                    args=step_args,
                    state=sr.state,
                    effect_summary=sr.effect_summary,
                    failure_reason=sr.failure_reason,
                    context_rev=context_at_submit.rev if context_at_submit is not None else 0,
                )
                self.recent_outcomes.append(rec)
                if len(self.recent_outcomes) > MAX_RECENT_OUTCOMES:
                    del self.recent_outcomes[:-MAX_RECENT_OUTCOMES]
                if self.active_goal is not None:
                    self.active_goal.recent_outcomes.append(rec)
                    if sr.state == "failed":
                        self.active_goal.last_failure_reason = sr.failure_reason or "step_failed"
                        if context_at_submit is not None:
                            fp = self.compute_action_state_fingerprint(
                                context_at_submit.state_brief, sr.method_id, sr.actor, step_args
                            )
                            self.active_goal.failed_action_fingerprints.add(fp)
                            self._failed_fingerprints.add(fp)

    @staticmethod
    def record_episodic_observation(
        memory_store: Optional[InMemoryCompanionStore],
        player_id: str,
        game_instance_id: str,
        context: ProjectedContext,
        note: str,
    ) -> Optional[MemoryRecord]:
        cleaned = (note or "").strip()
        if memory_store is None or not player_id or not cleaned:
            return None
        timeline = context.allowed_timeline_tags[0] if context.allowed_timeline_tags else None
        rec = MemoryRecord(
            memory_id=f"obs-{uuid.uuid4().hex[:10]}",
            created_at_ms=int(time.time() * 1000),
            epistemic_source="game_observed",
            pinned_by_player=False,
            max_disclosure_scope="same_game_companion",
            content=cleaned[:500],
            game_origin_id=game_instance_id or None,
            timeline_tag=timeline,
            observed_epoch=context.epoch,
        )
        memory_store.upsert_preserving_pins(player_id, rec)
        return rec
