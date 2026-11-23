from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from .memory import MemoryBundleV1, MemoryRecord, SpoilerGuard


class SQLiteCompanionStore:
    """Durable SQLite player-owned companion memory store enforcing G2A Section 4.4 & 7.2."""

    def __init__(
        self,
        db_path: str,
        supported_persona_keys: Optional[set[str]] = None,
    ) -> None:
        self.db_path = db_path
        self.supported_persona_keys = supported_persona_keys or {"tone", "verbosity", "nickname", "language"}
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    player_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    imported_at_ms INTEGER,
                    epistemic_source TEXT NOT NULL,
                    game_origin_id TEXT,
                    timeline_tag TEXT,
                    pinned_by_player INTEGER NOT NULL DEFAULT 0,
                    max_disclosure_scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    observed_epoch INTEGER,
                    PRIMARY KEY (player_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS persona_settings (
                    player_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    PRIMARY KEY (player_id, key)
                );
                CREATE TABLE IF NOT EXISTS autonomy_preferences (
                    player_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    policy_mode TEXT NOT NULL,
                    PRIMARY KEY (player_id, key)
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def _persona(self) -> "_SQLitePersonaProxy":
        return _SQLitePersonaProxy(self)

    @property
    def _autonomy(self) -> "_SQLiteAutonomyProxy":
        return _SQLiteAutonomyProxy(self)

    def upsert_player_record(self, player_id: str, rec: MemoryRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_records (
                    player_id, memory_id, created_at_ms, imported_at_ms, epistemic_source,
                    game_origin_id, timeline_tag, pinned_by_player, max_disclosure_scope, content, observed_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id, memory_id) DO UPDATE SET
                    imported_at_ms=excluded.imported_at_ms,
                    epistemic_source=excluded.epistemic_source,
                    game_origin_id=excluded.game_origin_id,
                    timeline_tag=excluded.timeline_tag,
                    pinned_by_player=excluded.pinned_by_player,
                    max_disclosure_scope=excluded.max_disclosure_scope,
                    content=excluded.content,
                    observed_epoch=excluded.observed_epoch;
                """,
                (
                    player_id,
                    rec.memory_id,
                    rec.created_at_ms,
                    rec.imported_at_ms,
                    rec.epistemic_source,
                    rec.game_origin_id,
                    rec.timeline_tag,
                    1 if rec.pinned_by_player else 0,
                    rec.max_disclosure_scope,
                    rec.content,
                    rec.observed_epoch,
                ),
            )
            self._conn.commit()

    def edit_player_record(self, player_id: str, memory_id: str, new_content: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE memory_records
                SET content = ?, epistemic_source = 'player_edited'
                WHERE player_id = ? AND memory_id = ?;
                """,
                (new_content, player_id, memory_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_pinned(self, player_id: str, memory_id: str, pinned: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memory_records SET pinned_by_player = ? WHERE player_id = ? AND memory_id = ?;",
                (1 if pinned else 0, player_id, memory_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_player_record(self, player_id: str, memory_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memory_records WHERE player_id = ? AND memory_id = ?;",
                (player_id, memory_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_records(self, player_id: str) -> List[MemoryRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_records WHERE player_id = ? ORDER BY created_at_ms ASC;",
                (player_id,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def upsert_preserving_pins(self, player_id: str, incoming: MemoryRecord) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT pinned_by_player, epistemic_source FROM memory_records WHERE player_id = ? AND memory_id = ?;",
                (player_id, incoming.memory_id),
            ).fetchone()
            if existing and (existing["pinned_by_player"] == 1 or existing["epistemic_source"] == "player_edited"):
                return
            self.upsert_player_record(player_id, incoming)

    def autonomous_compact(self, player_id: str, summary_record: MemoryRecord) -> None:
        """Deletes only unpinned, non-player-edited records before storing the compacted summary."""
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM memory_records
                WHERE player_id = ? AND pinned_by_player = 0 AND epistemic_source != 'player_edited';
                """,
                (player_id,),
            )
            self._conn.commit()
            self.upsert_player_record(player_id, summary_record)

    def recall_safe(self, player_id: str, guard: SpoilerGuard) -> List[MemoryRecord]:
        records = self.list_records(player_id)
        out: List[MemoryRecord] = []
        for rec in records:
            if guard.game_origin_id and rec.game_origin_id and rec.game_origin_id != guard.game_origin_id:
                if rec.max_disclosure_scope != "cross_game_companion":
                    continue
            if rec.timeline_tag and guard.allowed_timeline_tags is not None:
                if rec.timeline_tag not in guard.allowed_timeline_tags:
                    continue
            out.append(rec)
        return out

    def export_all(self, player_id: str) -> MemoryBundleV1:
        with self._lock:
            p_rows = self._conn.execute(
                "SELECT key, value_json FROM persona_settings WHERE player_id = ?;", (player_id,)
            ).fetchall()
            a_rows = self._conn.execute(
                "SELECT key, policy_mode FROM autonomy_preferences WHERE player_id = ?;", (player_id,)
            ).fetchall()
            persona = {r["key"]: json.loads(r["value_json"]) for r in p_rows}
            autonomy = {r["key"]: r["policy_mode"] for r in a_rows}
            records = self.list_records(player_id)
            return MemoryBundleV1(
                exported_at_ms=int(time.time() * 1000),
                persona_settings=persona,
                autonomy_preferences=autonomy,
                records=records,
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            created_at_ms=int(row["created_at_ms"]),
            imported_at_ms=int(row["imported_at_ms"]) if row["imported_at_ms"] is not None else None,
            epistemic_source=row["epistemic_source"],
            game_origin_id=row["game_origin_id"],
            timeline_tag=row["timeline_tag"],
            pinned_by_player=bool(row["pinned_by_player"]),
            max_disclosure_scope=row["max_disclosure_scope"],
            content=row["content"],
            observed_epoch=int(row["observed_epoch"]) if row["observed_epoch"] is not None else None,
        )


class _SQLitePersonaProxy:
    def __init__(self, store: SQLiteCompanionStore) -> None:
        self._store = store

    def setdefault(self, player_id: str, default: Dict[str, Any]) -> "_SQLitePersonaPlayerBucket":
        return _SQLitePersonaPlayerBucket(self._store, player_id)


class _SQLitePersonaPlayerBucket:
    def __init__(self, store: SQLiteCompanionStore, player_id: str) -> None:
        self._store = store
        self._player_id = player_id

    def __setitem__(self, key: str, value: Any) -> None:
        with self._store._lock:
            self._store._conn.execute(
                """
                INSERT INTO persona_settings (player_id, key, value_json) VALUES (?, ?, ?)
                ON CONFLICT(player_id, key) DO UPDATE SET value_json = excluded.value_json;
                """,
                (self._player_id, key, json.dumps(value, ensure_ascii=False)),
            )
            self._store._conn.commit()


class _SQLiteAutonomyProxy:
    def __init__(self, store: SQLiteCompanionStore) -> None:
        self._store = store

    def setdefault(self, player_id: str, default: Dict[str, str]) -> "_SQLiteAutonomyPlayerBucket":
        return _SQLiteAutonomyPlayerBucket(self._store, player_id)


class _SQLiteAutonomyPlayerBucket:
    def __init__(self, store: SQLiteCompanionStore, player_id: str) -> None:
        self._store = store
        self._player_id = player_id

    def update(self, mapping: Dict[str, str]) -> None:
        with self._store._lock:
            for k, v in mapping.items():
                self._store._conn.execute(
                    """
                    INSERT INTO autonomy_preferences (player_id, key, policy_mode) VALUES (?, ?, ?)
                    ON CONFLICT(player_id, key) DO UPDATE SET policy_mode = excluded.policy_mode;
                    """,
                    (self._player_id, k, str(v)),
                )
            self._store._conn.commit()
