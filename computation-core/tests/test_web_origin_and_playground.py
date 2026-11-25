from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ORIGIN_PY_SRC = os.path.join(ROOT_DIR, "interaction-origin", "python", "src")
CORE_PY_SRC = os.path.join(ROOT_DIR, "computation-core", "src")
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
for p in (ORIGIN_PY_SRC, CORE_PY_SRC, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from g2a_core.playground import LivePlaygroundStack, resolve_hub_binary
from g2a_origin.demo_host import build_spatial_demo_sdk
from split_standalone_repos import export_standalone_repos


def _wait_until(pred, timeout_sec: float = 6.0) -> bool:
    end = time.time() + timeout_sec
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class TestWebOriginCorsAndPlayground(unittest.TestCase):
    def test_cors_preflight_stream_query_token_and_dashboard_anti_clickjacking(self) -> None:
        hub_bin = resolve_hub_binary(ROOT_DIR)
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.json")
            runtime_path = os.path.join(tmp, "runtime.json")
            hub_proc = subprocess.Popen(
                [
                    hub_bin,
                    "-origin-addr", "127.0.0.1:0",
                    "-core-addr", "127.0.0.1:0",
                    "-consent-addr", "127.0.0.1:0",
                    "-origin-token", "secret-origin-tok",
                    "-core-tokens", "secret-core-tok=core.web.verified",
                    "-ledger", ledger_path,
                    "-runtime-file", runtime_path,
                    "-print-ports-json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert hub_proc.stdout is not None
                ports = json.loads(hub_proc.stdout.readline().strip())
                origin_base = f"http://{ports['origin_addr']}"
                core_base = f"http://{ports['core_addr']}"
                consent_base = f"http://{ports['consent_addr']}"

                # 1. Browser OPTIONS preflight on Origin & Core interfaces succeeds with 204 + CORS headers
                for url in (f"{origin_base}/g2a/v1/origin", f"{core_base}/g2a/v1/core"):
                    req = urllib.request.Request(
                        url,
                        method="OPTIONS",
                        headers={
                            "Origin": "http://127.0.0.1:19240",
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "Content-Type, Authorization",
                        },
                    )
                    with no_proxy.open(req, timeout=3) as resp:
                        self.assertEqual(resp.status, 204)
                        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:19240")
                        self.assertIn("Authorization", resp.headers.get("Access-Control-Allow-Headers", ""))

                # 2. Trusted Player Security Dashboard strictly sets X-Frame-Options: DENY and ZERO CORS headers
                req_dash = urllib.request.Request(
                    f"{consent_base}/",
                    method="GET",
                    headers={"Origin": "http://evil.example"},
                )
                with no_proxy.open(req_dash, timeout=3) as resp:
                    self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
                    self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

                # 3. EventSource query-token auth (?access_token=...) on /g2a/v1/origin/execute-stream
                bad_req = urllib.request.Request(
                    f"{origin_base}/g2a/v1/origin/execute-stream?slot_id=slot-web&access_token=wrong"
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    no_proxy.open(bad_req, timeout=3)
                self.assertEqual(ctx.exception.code, 401)

                good_req = urllib.request.Request(
                    f"{origin_base}/g2a/v1/origin/execute-stream?slot_id=slot-web&access_token=secret-origin-tok"
                )
                with no_proxy.open(good_req, timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    first_line = resp.readline().decode("utf-8")
                    self.assertIn("g2a-origin-execute-stream-ready", first_line)
            finally:
                hub_proc.terminate()
                hub_proc.wait(timeout=3)
                if hub_proc.stdout:
                    hub_proc.stdout.close()
                if hub_proc.stderr:
                    hub_proc.stderr.close()

    def test_live_playground_three_tier_auto_attach_trace_and_memory_api(self) -> None:
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        with LivePlaygroundStack(slot_id="slot-web-hybrid") as stack:
            # 1. Verify HTML5 Web Origin Game & JS module are served cleanly
            with no_proxy.open(stack.web_game_url, timeout=3) as resp:
                html = resp.read().decode("utf-8")
                self.assertIn("WebOriginSDK", html)
                self.assertIn("REDACTED FROM AI", html)

            js_base = stack.web_game_url.split("/index.html")[0] + "/g2a_web_origin.js"
            with no_proxy.open(js_base, timeout=3) as resp:
                js_code = resp.read().decode("utf-8")
                self.assertIn("pull://origin.execute", js_code)

            # 2. Connect spatial Origin on `slot-web-hybrid`
            sdk, world, _chat_log = build_spatial_demo_sdk(stack.hub_origin_url, slot_id="slot-web-hybrid")
            try:
                # 3. Verify Playground auto-attacher connects CoreSDK over WebSocket automatically
                def _is_attached() -> bool:
                    with no_proxy.open(f"{stack.core_console_url}api/status", timeout=3) as r:
                        data = json.loads(r.read().decode("utf-8"))
                        return bool(data.get("attached"))

                self.assertTrue(_wait_until(_is_attached, 6.0))

                # 4. Issue player command -> companion walks & opens chest_R -> cognitive trace recorded in Console UI!
                sdk.report_player_command("打开我右侧的宝箱")
                self.assertTrue(_wait_until(lambda: "chest_R" in world.opened_chests, 6.0))
                self.assertNotIn("secret_trap", world.opened_chests)

                with no_proxy.open(f"{stack.core_console_url}api/status", timeout=3) as r:
                    status = json.loads(r.read().decode("utf-8"))
                self.assertGreaterEqual(len(status["traces"]), 1)
                self.assertIn("chest_R", status["traces"][0]["intent_summary"])

                # 5. Add a player-pinned memory via Companion Console API and export g2a.memory.bundle.v1
                post_req = urllib.request.Request(
                    f"{stack.core_console_url}api/memories",
                    data=json.dumps({"content": "Always inspect right-hand chests first", "pinned": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with no_proxy.open(post_req, timeout=3) as r:
                    saved = json.loads(r.read().decode("utf-8"))
                    self.assertTrue(saved["saved"]["pinned_by_player"])

                with no_proxy.open(f"{stack.core_console_url}api/memories/export", timeout=3) as r:
                    bundle = json.loads(r.read().decode("utf-8"))
                    self.assertEqual(bundle["format"], "g2a.memory.bundle.v1")
                    self.assertGreaterEqual(len(bundle["records"]), 2)

                # 6. Verify Playground Console DNS-rebinding shield, strict JSON, 413 body cap, & security headers
                c_host, c_port_s = stack.core_console_url.replace("http://", "").rstrip("/").split(":")
                c_port = int(c_port_s)

                rebind_conn = http.client.HTTPConnection(c_host, c_port, timeout=3.0)
                try:
                    rebind_conn.putrequest("GET", "/api/status", skip_host=True)
                    rebind_conn.putheader("Host", "evil.rebind.example")
                    rebind_conn.endheaders()
                    r_rebind = rebind_conn.getresponse()
                    _ = r_rebind.read()
                    self.assertEqual(r_rebind.status, 403)
                finally:
                    rebind_conn.close()

                sec_conn = http.client.HTTPConnection(c_host, c_port, timeout=3.0)
                try:
                    sec_conn.request(
                        "POST",
                        "/api/memories",
                        body=b'{"content":"a","content":"b"}',
                        headers={"Content-Type": "application/json", "Connection": "keep-alive"},
                    )
                    r_dup = sec_conn.getresponse()
                    _ = r_dup.read()
                    self.assertEqual(r_dup.status, 400)
                    self.assertEqual(r_dup.getheader("X-Content-Type-Options"), "nosniff")
                    self.assertEqual(r_dup.getheader("X-Frame-Options"), "DENY")
                    self.assertEqual(r_dup.getheader("Connection"), "keep-alive")

                    # Reuse SAME Keep-Alive socket for oversized Content-Length check -> 413
                    sec_conn.request(
                        "POST",
                        "/api/memories",
                        body=b"",
                        headers={"Content-Type": "application/json", "Content-Length": str(5 * 1024 * 1024)},
                    )
                    r_big = sec_conn.getresponse()
                    _ = r_big.read()
                    self.assertEqual(r_big.status, 413)
                finally:
                    sec_conn.close()
            finally:
                sdk.stop_loopback_execute_listener()

    def test_standalone_three_repo_splitter_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exported = export_standalone_repos(Path(tmp), github_org="aiaimimi0920")
            self.assertEqual(len(exported), 3)
            for repo_dir in exported:
                self.assertTrue((repo_dir / ".git").is_dir())
                self.assertTrue((repo_dir / ".gitignore").is_file())
                readme_txt = (repo_dir / "README.md").read_text(encoding="utf-8")
                self.assertNotIn("../G2A/docs/standard/", readme_txt)
                st = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(repo_dir),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(st.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
