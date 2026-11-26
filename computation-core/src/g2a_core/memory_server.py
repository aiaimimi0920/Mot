from __future__ import annotations

import hmac
import http.client
import json
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from .jcs import parse_strict_json
from .memory import ImportReceiptV1, MemoryBundleV1, MemoryRecord, MemoryService
from .schemas import G2AError
from .sqlite_memory import SQLiteCompanionStore
from .ssrf import validate_outbound_url_against_ssrf

MAX_MEMORY_HTTP_BODY_BYTES = 4 * 1024 * 1024


class _QuietMemoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


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
            protocol_version = "HTTP/1.1"

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
                for expected_tok, pid in srv_ref.player_tokens.items():
                    if hmac.compare_digest(tok, expected_tok):
                        return pid
                return None

            def _read_strict_body(self) -> Optional[Dict[str, Any]]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "invalid_content_length"})
                    return None
                if length < 0 or length > MAX_MEMORY_HTTP_BODY_BYTES:
                    self._send_json(413, {"error": "payload_too_large"})
                    return None
                if length == 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    parsed = parse_strict_json(raw)
                except G2AError as gerr:
                    self._send_json(400, {"error": gerr.code, "message": gerr.message})
                    return None
                if not isinstance(parsed, dict):
                    self._send_json(400, {"error": "invalid_request", "message": "Expected JSON object"})
                    return None
                return parsed

            def do_GET(self) -> None:
                pid = self._auth_player()
                if not pid:
                    self._send_json(401, {"error": "unauthenticated"})
                    return
                clean_path = urlparse(self.path).path
                if clean_path == "/g2a/v1/memory/export":
                    bundle = srv_ref.service.export_bundle(pid)
                    self._send_json(200, bundle.to_dict())
                    return
                if clean_path == "/g2a/v1/memory/records":
                    recs = [r.to_dict() for r in srv_ref.store.list_records(pid)]
                    self._send_json(200, {"records": recs})
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                pid = self._auth_player()
                if not pid:
                    self._send_json(401, {"error": "unauthenticated"})
                    return
                body = self._read_strict_body()
                if body is None:
                    return
                clean_path = urlparse(self.path).path

                if clean_path == "/g2a/v1/memory/import":
                    try:
                        bundle = MemoryBundleV1.from_dict(body["bundle"])
                        selected = body.get("selected_ids") or [r.memory_id for r in bundle.records]
                        receipt = srv_ref.service.import_bundle(pid, bundle, list(selected))
                    except (KeyError, ValueError, TypeError) as exc:
                        self._send_json(400, {"error": "invalid_arguments", "message": str(exc)})
                        return
                    self._send_json(200, {
                        "record_statuses": receipt.record_statuses,
                        "applied_settings": receipt.applied_settings,
                        "unsupported_settings": receipt.unsupported_settings,
                    })
                    return

                if clean_path == "/g2a/v1/memory/records":
                    if "content" not in body:
                        self._send_json(400, {"error": "invalid_arguments", "message": "Missing content"})
                        return
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
                clean_path = urlparse(self.path).path
                if clean_path.startswith("/g2a/v1/memory/records/"):
                    mid = unquote(clean_path.rsplit("/", 1)[-1])
                    body = self._read_strict_body()
                    if body is None:
                        return
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
                clean_path = urlparse(self.path).path
                if clean_path.startswith("/g2a/v1/memory/records/"):
                    mid = unquote(clean_path.rsplit("/", 1)[-1])
                    ok = srv_ref.store.delete_player_record(pid, mid)
                    self._send_json(200 if ok else 404, {"deleted": ok})
                    return
                self._send_json(404, {"error": "not_found"})

            def _send_json(self, status: int, obj: Any) -> None:
                out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                want_close = self.headers.get("Connection", "").strip().lower() == "close"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Connection", "close" if want_close else "keep-alive")
                self.end_headers()
                self.wfile.write(out)

        self._httpd = _QuietMemoryHTTPServer((self.host, self.port), _Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


class MemoryHTTPClient:
    """
    SSRF-guarded persistent HTTP/1.1 Keep-Alive client for cross-vendor Portable Companion Memory (§4.4 & §7.2).
    Supports export, conservative import, CRUD record management, and one-step provider-to-provider transfer.
    """

    def __init__(
        self,
        base_url: str,
        player_token: str,
        allow_loopback: bool = False,
        timeout_sec: float = 8.0,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        validate_outbound_url_against_ssrf(base_url, allow_loopback=allow_loopback)
        self.base_url = base_url.rstrip("/")
        self.player_token = player_token
        self.allow_loopback = allow_loopback
        self.timeout_sec = timeout_sec
        self.ssl_context = ssl_context

        parsed = urlparse(self.base_url)
        self._is_https = parsed.scheme == "https"
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or (443 if self._is_https else 80)
        self._base_path = parsed.path.rstrip("/")
        self._lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None

    def __enter__(self) -> "MemoryHTTPClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _request_json(self, method: str, path: str, body_obj: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        full_path = f"{self._base_path}{path}"
        raw_body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8") if body_obj is not None else None
        headers = {
            "Authorization": f"Bearer {self.player_token}",
            "Connection": "keep-alive",
        }
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw_body))

        with self._lock:
            for attempt in range(2):
                if self._conn is None:
                    if self._is_https:
                        ctx = self.ssl_context or ssl.create_default_context()
                        self._conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout_sec, context=ctx)
                    else:
                        self._conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout_sec)
                try:
                    self._conn.request(method, full_path, body=raw_body, headers=headers)
                    resp = self._conn.getresponse()
                    raw_resp = resp.read()
                    parsed = parse_strict_json(raw_resp) if raw_resp else {}
                    if resp.status >= 400:
                        msg = str(parsed.get("message") or parsed.get("error") or f"HTTP {resp.status}")
                        code = "unauthenticated" if resp.status == 401 else "not_found" if resp.status == 404 else "invalid_request"
                        raise G2AError(code, resp.status, msg)
                    return parsed if isinstance(parsed, dict) else {}
                except G2AError:
                    raise
                except Exception as exc:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                    if attempt == 1:
                        raise G2AError("unavailable", 503, f"Memory HTTP request failed: {exc}") from exc
        raise G2AError("unavailable", 503, "Unreachable")

    def export_bundle(self) -> MemoryBundleV1:
        return MemoryBundleV1.from_dict(self._request_json("GET", "/g2a/v1/memory/export"))

    def import_bundle(self, bundle: MemoryBundleV1, selected_ids: Optional[List[str]] = None) -> ImportReceiptV1:
        ids = selected_ids if selected_ids is not None else [r.memory_id for r in bundle.records]
        res = self._request_json("POST", "/g2a/v1/memory/import", {"bundle": bundle.to_dict(), "selected_ids": ids})
        return ImportReceiptV1(
            record_statuses=dict(res.get("record_statuses") or {}),
            applied_settings=list(res.get("applied_settings") or []),
            unsupported_settings=list(res.get("unsupported_settings") or []),
        )

    def list_records(self) -> List[MemoryRecord]:
        res = self._request_json("GET", "/g2a/v1/memory/records")
        return [MemoryRecord.from_dict(r) for r in (res.get("records") or [])]

    def create_record(
        self,
        content: str,
        memory_id: Optional[str] = None,
        epistemic_source: str = "player_stated",
        pinned_by_player: bool = False,
        max_disclosure_scope: str = "same_game_companion",
        game_origin_id: Optional[str] = None,
        timeline_tag: Optional[str] = None,
    ) -> MemoryRecord:
        payload: Dict[str, Any] = {
            "content": content,
            "epistemic_source": epistemic_source,
            "pinned_by_player": pinned_by_player,
            "max_disclosure_scope": max_disclosure_scope,
        }
        if memory_id is not None:
            payload["memory_id"] = memory_id
        if game_origin_id is not None:
            payload["game_origin_id"] = game_origin_id
        if timeline_tag is not None:
            payload["timeline_tag"] = timeline_tag
        return MemoryRecord.from_dict(self._request_json("POST", "/g2a/v1/memory/records", payload))

    def patch_record(
        self,
        memory_id: str,
        content: Optional[str] = None,
        pinned_by_player: Optional[bool] = None,
    ) -> bool:
        patch: Dict[str, Any] = {}
        if content is not None:
            patch["content"] = content
        if pinned_by_player is not None:
            patch["pinned_by_player"] = pinned_by_player
        res = self._request_json("PATCH", f"/g2a/v1/memory/records/{quote(memory_id, safe='')}", patch)
        return bool(res.get("updated", False))

    def delete_record(self, memory_id: str) -> bool:
        res = self._request_json("DELETE", f"/g2a/v1/memory/records/{quote(memory_id, safe='')}")
        return bool(res.get("deleted", False))

    def transfer_to(
        self,
        destination: "MemoryHTTPClient",
        selected_ids: Optional[List[str]] = None,
    ) -> ImportReceiptV1:
        bundle = self.export_bundle()
        return destination.import_bundle(bundle, selected_ids=selected_ids)
