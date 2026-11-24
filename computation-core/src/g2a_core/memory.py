from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DISCLOSURE_ORDER = {
    "private_player_only": 0,
    "same_game_companion": 1,
    "cross_game_companion": 2,
}


def min_disclosure(scope_a: str, scope_b: str) -> str:
    rank_a = DISCLOSURE_ORDER.get(scope_a, 0)
    rank_b = DISCLOSURE_ORDER.get(scope_b, 0)
    target = min(rank_a, rank_b)
    for k, v in DISCLOSURE_ORDER.items():
        if v == target:
            return k
    return "private_player_only"


@dataclass
class SpoilerGuard:
    current_epoch: int
    game_origin_id: Optional[str] = None
    allowed_timeline_tags: Optional[set[str]] = None
    query_hint: Optional[str] = None
    max_results: int = 48


def _keyword_score(query_hint: Optional[str], content: str) -> int:
    if not query_hint:
        return 0
    q_low = query_hint.lower()
    c_low = content.lower()
    tokens = [tok for tok in q_low.replace(",", " ").split() if len(tok) >= 2]
    return sum(1 for tok in tokens if tok in c_low)


def filter_and_rank_safe_memories(records: List[MemoryRecord], guard: SpoilerGuard) -> List[MemoryRecord]:
    safe: List[MemoryRecord] = []
    for rec in records:
        if guard.game_origin_id and rec.game_origin_id and rec.game_origin_id != guard.game_origin_id:
            if rec.max_disclosure_scope != "cross_game_companion":
                continue
        if rec.timeline_tag and guard.allowed_timeline_tags is not None:
            if rec.timeline_tag not in guard.allowed_timeline_tags:
                continue
        # Save-reload anti-spoiler rule (Section 7.2): suppress game_observed facts from future epochs
        if (
            rec.epistemic_source == "game_observed"
            and rec.observed_epoch is not None
            and guard.current_epoch < rec.observed_epoch
        ):
            continue
        safe.append(rec)

    safe.sort(
        key=lambda r: (
            1 if (r.pinned_by_player or r.epistemic_source == "player_edited") else 0,
            _keyword_score(guard.query_hint, r.content),
            r.created_at_ms,
        ),
        reverse=True,
    )
    if guard.max_results > 0:
        return safe[: guard.max_results]
    return safe


@dataclass
class MemoryRecord:
    memory_id: str
    created_at_ms: int
    epistemic_source: str  # "game_observed" | "player_stated" | "agent_summary" | "player_edited"
    pinned_by_player: bool
    max_disclosure_scope: str  # "private_player_only" | "same_game_companion" | "cross_game_companion"
    content: str
    imported_at_ms: Optional[int] = None
    game_origin_id: Optional[str] = None
    timeline_tag: Optional[str] = None
    observed_epoch: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "memory_id": self.memory_id,
            "created_at_ms": self.created_at_ms,
            "imported_at_ms": self.imported_at_ms,
            "epistemic_source": self.epistemic_source,
            "game_origin_id": self.game_origin_id,
            "timeline_tag": self.timeline_tag,
            "pinned_by_player": self.pinned_by_player,
            "max_disclosure_scope": self.max_disclosure_scope,
            "content": self.content,
        }
        if self.observed_epoch is not None:
            out["observed_epoch"] = self.observed_epoch
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryRecord":
        return cls(
            memory_id=d["memory_id"],
            created_at_ms=int(d["created_at_ms"]),
            imported_at_ms=d.get("imported_at_ms"),
            epistemic_source=d["epistemic_source"],
            game_origin_id=d.get("game_origin_id"),
            timeline_tag=d.get("timeline_tag"),
            pinned_by_player=bool(d.get("pinned_by_player", False)),
            max_disclosure_scope=d.get("max_disclosure_scope", "private_player_only"),
            content=d["content"],
            observed_epoch=d.get("observed_epoch"),
        )


@dataclass
class MemoryBundleV1:
    exported_at_ms: int
    persona_settings: Dict[str, Any] = field(default_factory=dict)
    autonomy_preferences: Dict[str, str] = field(default_factory=dict)
    records: List[MemoryRecord] = field(default_factory=list)
    format: str = "g2a.memory.bundle.v1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format,
            "exported_at_ms": self.exported_at_ms,
            "persona_settings": dict(self.persona_settings),
            "autonomy_preferences": dict(self.autonomy_preferences),
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryBundleV1":
        if d.get("format") != "g2a.memory.bundle.v1":
            raise ValueError(f"Unsupported memory bundle format: {d.get('format')!r}")
        return cls(
            exported_at_ms=int(d["exported_at_ms"]),
            persona_settings=dict(d.get("persona_settings", {})),
            autonomy_preferences=dict(d.get("autonomy_preferences", {})),
            records=[MemoryRecord.from_dict(r) for r in d.get("records", [])],
        )


@dataclass
class ImportReceiptV1:
    record_statuses: Dict[str, str]  # memory_id -> "imported" | "transformed" | "rejected"
    applied_settings: List[str]
    unsupported_settings: List[str]


class InMemoryCompanionStore:
    """Reference player-owned companion memory store enforcing Section 4.4 & 7.2 invariants."""

    def __init__(self, supported_persona_keys: Optional[set[str]] = None) -> None:
        self._records: Dict[str, Dict[str, MemoryRecord]] = {}
        self._persona: Dict[str, Dict[str, Any]] = {}
        self._autonomy: Dict[str, Dict[str, str]] = {}
        self.supported_persona_keys = supported_persona_keys or {"tone", "verbosity", "nickname"}

    def upsert_player_record(self, player_id: str, rec: MemoryRecord) -> None:
        self._records.setdefault(player_id, {})[rec.memory_id] = rec

    def delete_player_record(self, player_id: str, memory_id: str) -> bool:
        return self._records.get(player_id, {}).pop(memory_id, None) is not None

    def upsert_preserving_pins(self, player_id: str, incoming: MemoryRecord) -> None:
        bucket = self._records.setdefault(player_id, {})
        existing = bucket.get(incoming.memory_id)
        if existing and (existing.pinned_by_player or existing.epistemic_source == "player_edited"):
            return
        bucket[incoming.memory_id] = incoming

    def autonomous_compact(self, player_id: str, summary_record: MemoryRecord) -> None:
        """Autonomous compaction MUST NEVER overwrite player edits or delete pinned records."""
        bucket = self._records.setdefault(player_id, {})
        kept: Dict[str, MemoryRecord] = {}
        for mid, rec in bucket.items():
            if rec.pinned_by_player or rec.epistemic_source == "player_edited":
                kept[mid] = rec
        kept[summary_record.memory_id] = summary_record
        self._records[player_id] = kept

    def recall_safe(self, player_id: str, guard: SpoilerGuard) -> List[MemoryRecord]:
        return filter_and_rank_safe_memories(list(self._records.get(player_id, {}).values()), guard)

    def export_all(self, player_id: str) -> MemoryBundleV1:
        return MemoryBundleV1(
            exported_at_ms=int(time.time() * 1000),
            persona_settings=dict(self._persona.get(player_id, {})),
            autonomy_preferences=dict(self._autonomy.get(player_id, {})),
            records=list(self._records.get(player_id, {}).values()),
        )


class MemoryService:
    def __init__(
        self,
        store: InMemoryCompanionStore,
        platform_ceiling: str = "same_game_companion",
    ) -> None:
        self.store = store
        self.platform_ceiling = platform_ceiling

    def export_bundle(self, verified_player_id: str) -> MemoryBundleV1:
        """Verifies ONLY the player's own identity; never vets or restricts destination platforms."""
        if not verified_player_id:
            raise PermissionError("Player authentication required")
        return self.store.export_all(verified_player_id)

    def import_bundle(
        self,
        verified_player_id: str,
        bundle: MemoryBundleV1,
        selected_ids: List[str],
    ) -> ImportReceiptV1:
        if not verified_player_id:
            raise PermissionError("Player authentication required")
        selected = set(selected_ids)
        statuses: Dict[str, str] = {}
        now_ms = int(time.time() * 1000)
        for rec in bundle.records:
            if rec.memory_id not in selected:
                continue
            narrowed_scope = min_disclosure(rec.max_disclosure_scope, self.platform_ceiling)
            transformed = narrowed_scope != rec.max_disclosure_scope
            imported_copy = MemoryRecord(
                memory_id=rec.memory_id,
                created_at_ms=rec.created_at_ms,
                imported_at_ms=now_ms,
                epistemic_source=rec.epistemic_source,
                game_origin_id=rec.game_origin_id,
                timeline_tag=rec.timeline_tag,
                pinned_by_player=rec.pinned_by_player,
                max_disclosure_scope=narrowed_scope,
                content=rec.content,
                observed_epoch=rec.observed_epoch,
            )
            self.store.upsert_preserving_pins(verified_player_id, imported_copy)
            statuses[rec.memory_id] = "transformed" if transformed else "imported"

        applied: List[str] = []
        unsupported: List[str] = []
        persona_bucket = self.store._persona.setdefault(verified_player_id, {})
        for k, v in bundle.persona_settings.items():
            if k in self.store.supported_persona_keys:
                persona_bucket[k] = v
                applied.append(k)
            else:
                unsupported.append(k)
        self.store._autonomy.setdefault(verified_player_id, {}).update(bundle.autonomy_preferences)
        return ImportReceiptV1(
            record_statuses=statuses,
            applied_settings=applied,
            unsupported_settings=unsupported,
        )
