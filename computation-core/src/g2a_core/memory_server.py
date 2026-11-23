from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .memory import MemoryBundleV1, MemoryRecord, MemoryService
from .sqlite_memory import SQLiteCompanionStore


class MemoryHTTPServer:
    """
    Player-owned Companion Memory Portability HTTP Service (G2A Section 7.2 & §4.4):
      - GET  /g2a/v1/memory/export       -> Exports `g2a.memory.bundle.v1` (verifies ONLY player token; zero destination vetting!)
      - POST /g2a/v1/memory/import       -> Imports bundle conservatively under local `platform_ceiling`, preserving pins
      - GET  /g2a/v1/memory/records      -> Lists player records
      - POST /g2a/v1/memory/records      -> Adds/updates a player record
      - PATCH /g2a/v1/memory/records/<id> -> Edits content or toggles `pinned_by_player`
      - DELETE /g2a/v1/memory/records/<id> -> Deletes a record
    """

    def __init__(
        self,
        store: SQLiteCompanionStore,
        platform_ceiling: str = "same_game_companion",
        host: str = "127.0.0.1",
        port: int = 0,
        player_tokens: Optional[Dict[str, str]] = None,
    ) -> None:
        self.store = store
        self.service = MemoryService(store, platform_ceiling=platform_ceiling)
        self.host = host
        self.port = port
        self.player_tokens = player_tokens or {}  # bearer_token -> verified_player_id
        self._httpd: Optional[ThreadingHTTPServer] = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        h, p = self._httpd.server_address
        return f"http://{h}:{p}"

    def start(self) -> str:
        srv_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _auth_player(self) -> Optional[str]:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return None
                tok = auth[7:].strip()
                if not tok:
                    return None
                if not srv_ref.player_tokens:
                    return tok
                return srv_ref.player_tokens.get(tok)

            def do_GET(self) -> None:
                pid = self._auth_player()
                if not pid:
                    self._send_json(401, {"error": "unauthenticated"})
                    return
                if self.path == "/g2a/v1/memory/export":
                    bundle = srv_ref.service.export_bundle(pid)
                    self._send_json(200, bundle.to_dict())
                    return
                if self.path == "/g2a/v1/memory/records":
                    recs = [r.to_dict() for r in srv_ref.store.list_records(pid)]
                    self._send_json(200, {"records": recs})
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                pid = self._auth_player()
                if not pid:
                    self._send_json(401, {"error": "unauthenticated"})
                    return
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw.decode("utf-8")) if raw else {}

                if self.path == "/g2a/v1/memory/import":
                    bundle = MemoryBundleV1.from_dict(body["bundle"])
                    selected = body.get("selected_ids") or [r.memory_id for r in bundle.records]
                    receipt = srv_ref.service.import_bundle(pid, bundle, list(selected))
                    self._send_json(200, {
                        "record_statuses": receipt.record_statuses,
                        "applied_settings": receipt.applied_settings,
                        "unsupported_settings": receipt.unsupported_settings,
                    })
                    return

                if self.path == "/g2a/v1/memory/records":
                    mid = body.get("memory_id") or f"mem-{uuid.uuid4().hex[:8]}"
                    rec = MemoryRecord(
                        memory_id=str(mid),
                        created_at_ms=int(time.time() * 1000),
                        epistemic_source=str(body.get("epistemic_source", "player_stated")),
                        pinned_by_player=bool(body.get("pinned_by_player", False)),
                        max_disclosure_scope=str(body.get("max_disclosure_scope", "same_game_companion")),
                        content=str(body["content"]),
                        game_origin_id=body.get("game_origin_id"),
                        timeline_tag=body.get("timeline_tag"),
                    )
                    srv_ref.store.upsert_player_record(pid, rec)
                    self._send_json(200, rec.to_dict())
                    return

                self._send_json(404, {"error": "not_found"})

            def do_PATCH(self) -> None:
                pid = self._auth_player()
                if not pid:
                    self._send_json(401, {"error": "unauthenticated"})
                    return
                if self.path.startswith("/g2a/v1/memory/records/"):
                    mid = self.path.rsplit("/", 1)[-1]
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    ok = False
                    if "content" in body:
                        ok = srv_ref.store.edit_player_record(pid, mid, str(body["content"])) or ok
                    if "pinned_by_player" in body:
                        ok = srv_ref.store.set_pinned(pid, mid, bool(body["pinned_by_player"])) or ok
                    self._send_json(200 if ok else 404, {"updated": ok})
                    return
                self._send_json(404, {"error": "not_found"})

            def do_DELETE(self) -> None:
                pid = self._auth_player()
                if not pid:
                    self._send_json(401, {"error": "unauthenticated"})
                    return
                if self.path.startswith("/g2a/v1/memory/records/"):
                    mid = self.path.rsplit("/", 1)[-1]
                    ok = srv_ref.store.delete_player_record(pid, mid)
                    self._send_json(200 if ok else 404, {"deleted": ok})
                    return
                self._send_json(404, {"error": "not_found"})

            def _send_json(self, status: int, obj: Any) -> None:
                out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
