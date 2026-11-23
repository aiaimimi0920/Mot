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

from g2a_core import CloudRelayServer, CoreSDK, MemoryHTTPServer, SQLiteCompanionStore
from g2a_core.cli import HeuristicReferencePlanner
from g2a_origin.demo_host import build_spatial_demo_sdk


def _wait_until(pred, timeout_sec: float = 6.0) -> bool:
    end = time.time() + timeout_sec
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class TestNatOutboundRelayAndMemoryHTTP(unittest.TestCase):
    def test_nat_outbound_cloud_relay_end_to_end(self) -> None:
        hub_bin = os.path.join(ROOT_DIR, "intelligent-hub", "bin", "intelligent-hub.exe")
        if not os.path.exists(hub_bin):
            self.skipTest("intelligent-hub binary not built yet")

        relay = CloudRelayServer(host="127.0.0.1", port=0, relay_token="tun-secret-01")
        relay_url = relay.start()
        tmp = tempfile.TemporaryDirectory()
        ledger_path = os.path.join(tmp.name, "nat_hub.json")

        hub_proc = subprocess.Popen(
            [
                hub_bin,
                "-origin-addr", "127.0.0.1:0",
                "-core-addr", "127.0.0.1:0",
                "-consent-addr", "127.0.0.1:0",
                "-ledger", ledger_path,
                "-no-runtime-file",
                "-auto-approve-scope",
                "-outbound-relay", relay_url,
                "-hub-id", "home-pc-hub-7",
                "-relay-token", "tun-secret-01",
                "-print-ports-json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            info = json.loads(hub_proc.stdout.readline().strip())
            origin_url = f"http://{info['origin_addr']}"

            # Wait until home-pc-hub-7 outbound tunnel attaches to CloudRelayServer!
            self.assertTrue(_wait_until(lambda: relay.is_hub_connected("home-pc-hub-7")))

            origin_sdk, world, chat_log = build_spatial_demo_sdk(origin_url, slot_id="slot-nat-1")
            # Cloud CoreSDK connects ONLY to CloudRelayServer (/hubs/home-pc-hub-7), NEVER directly to Hub's local core_addr!
            cloud_core = CoreSDK(planner=HeuristicReferencePlanner(), allow_loopback=True, heartbeat_interval_sec=1.0)
            try:
                cloud_core.connect(f"{relay_url}/hubs/home-pc-hub-7", "cloud-core-alpha", "slot-nat-1")
                time.sleep(0.15)

                origin_sdk.report_player_command("打开右侧的宝箱")
                ok = _wait_until(lambda: "chest_R" in world.opened_chests and len(cloud_core.completed_receipts) == 1)
                self.assertTrue(ok, f"Expected chest_R opened via NAT outbound cloud relay; receipts={cloud_core.completed_receipts}")
                self.assertTrue(any("AI=True" in c for c in chat_log))
            finally:
                cloud_core.shutdown_session_cleanly()
                origin_sdk.stop_loopback_execute_listener()
        finally:
            hub_proc.terminate()
            try:
                hub_proc.wait(timeout=3)
            except Exception:
                hub_proc.kill()
            if hub_proc.stdout:
                hub_proc.stdout.close()
            if hub_proc.stderr:
                hub_proc.stderr.close()
            relay.stop()
            tmp.cleanup()

    def test_cross_vendor_memory_http_export_and_conservative_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_a = SQLiteCompanionStore(os.path.join(tmp, "vendor_a.db"))
            store_b = SQLiteCompanionStore(os.path.join(tmp, "vendor_b.db"))
            srv_a = MemoryHTTPServer(store_a, platform_ceiling="cross_game_companion", player_tokens={"tok-alice": "alice"})
            srv_b = MemoryHTTPServer(store_b, platform_ceiling="same_game_companion", player_tokens={"tok-alice": "alice"})
            url_a = srv_a.start()
            url_b = srv_b.start()
            no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                # 1. Create & pin record on Vendor A
                create_req = urllib.request.Request(
                    f"{url_a}/g2a/v1/memory/records",
                    data=json.dumps({
                        "memory_id": "m-http-1",
                        "content": "Player prefers non-lethal stealth",
                        "pinned_by_player": True,
                        "max_disclosure_scope": "cross_game_companion",
                    }).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", "Authorization": "Bearer tok-alice"},
                )
                with no_proxy.open(create_req, timeout=3.0) as r:
                    self.assertEqual(r.status, 200)

                # 2. Export open-format g2a.memory.bundle.v1 from Vendor A (zero destination vetting!)
                exp_req = urllib.request.Request(
                    f"{url_a}/g2a/v1/memory/export",
                    method="GET",
                    headers={"Authorization": "Bearer tok-alice"},
                )
                with no_proxy.open(exp_req, timeout=3.0) as r:
                    bundle = json.loads(r.read().decode("utf-8"))
                self.assertEqual(bundle["format"], "g2a.memory.bundle.v1")

                # 3. Import into Vendor B (stricter ceiling -> conservatively narrowed to same_game_companion!)
                imp_req = urllib.request.Request(
                    f"{url_b}/g2a/v1/memory/import",
                    data=json.dumps({"bundle": bundle}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", "Authorization": "Bearer tok-alice"},
                )
                with no_proxy.open(imp_req, timeout=3.0) as r:
                    receipt = json.loads(r.read().decode("utf-8"))
                self.assertEqual(receipt["record_statuses"]["m-http-1"], "transformed")

                list_req = urllib.request.Request(
                    f"{url_b}/g2a/v1/memory/records",
                    method="GET",
                    headers={"Authorization": "Bearer tok-alice"},
                )
                with no_proxy.open(list_req, timeout=3.0) as r:
                    recs = json.loads(r.read().decode("utf-8"))["records"]
                self.assertEqual(len(recs), 1)
                self.assertTrue(recs[0]["pinned_by_player"])
                self.assertEqual(recs[0]["max_disclosure_scope"], "same_game_companion")
            finally:
                srv_a.stop()
                srv_b.stop()
                store_a.close()
                store_b.close()


if __name__ == "__main__":
    unittest.main()
