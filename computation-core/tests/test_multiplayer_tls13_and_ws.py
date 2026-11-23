from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT_DIR, "computation-core", "src"))
sys.path.insert(0, os.path.join(ROOT_DIR, "interaction-origin", "python", "src"))

from g2a_core import CoreSDK, G2AError
from g2a_core.cli import HeuristicReferencePlanner
from g2a_origin import CapabilityEntry, ImmediateSucceeded, LoopbackHTTPTransportClient, OriginSDK
from g2a_origin.demo_host import build_spatial_demo_sdk


def _wait_until(pred, timeout_sec: float = 5.0) -> bool:
    end = time.time() + timeout_sec
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.04)
    return pred()


class TestMultiplayerTls13AndWebSocket(unittest.TestCase):
    def test_tls13_handshake_and_rfc6455_websocket_push_stream_end_to_end(self) -> None:
        hub_bin = os.path.join(ROOT_DIR, "intelligent-hub", "bin", "intelligent-hub.exe")
        if not os.path.exists(hub_bin):
            self.skipTest("intelligent-hub binary not built yet")

        tmp = tempfile.TemporaryDirectory()
        hub_proc = subprocess.Popen(
            [
                hub_bin,
                "-origin-addr", "127.0.0.1:0",
                "-core-addr", "127.0.0.1:0",
                "-consent-addr", "127.0.0.1:0",
                "-ledger", os.path.join(tmp.name, "tls_hub.json"),
                "-no-runtime-file",
                "-auto-approve-scope",
                "-tls-self-signed",
                "-print-ports-json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            info = json.loads(hub_proc.stdout.readline().strip())
            origin_url = f"http://{info['origin_addr']}"
            core_https_url = info["core_url"]
            host, port_str = info["core_addr"].split(":")

            # Build strict TLS 1.3 SSLContext pinned to Hub's ephemeral ECDSA P-256 certificate
            tls_ctx = ssl.create_default_context(cadata=info["ca_cert_pem"])
            tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_3

            # 1. Verify raw TLS negotiation uses TLSv1.3 (RFC 8446)
            with socket.create_connection((host, int(port_str)), timeout=5.0) as raw_sock:
                with tls_ctx.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                    self.assertEqual(tls_sock.version(), "TLSv1.3")

            # 2. Verify full spatial command walkthrough over HTTPS + WSS (RFC 6455 WebSocket over TLS 1.3)!
            origin_sdk, world, chat_log = build_spatial_demo_sdk(origin_url, slot_id="slot-wss-tls13")
            ws_core = CoreSDK(
                planner=HeuristicReferencePlanner(),
                allow_loopback=True,
                stream_mode="websocket",
                ssl_context=tls_ctx,
                heartbeat_interval_sec=1.0,
            )
            try:
                ws_core.connect(core_https_url, "core-wss-tls13", "slot-wss-tls13")
                time.sleep(0.15)
                origin_sdk.report_player_command("打开右侧的宝箱")
                ok = _wait_until(lambda: "chest_R" in world.opened_chests and len(ws_core.completed_receipts) == 1)
                self.assertTrue(ok, f"Expected WSS + TLS 1.3 completion; receipts={ws_core.completed_receipts}")
                self.assertTrue(any("AI=True" in c for c in chat_log))
            finally:
                ws_core.shutdown_session_cleanly()
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
            tmp.cleanup()

    def test_multiplayer_room_faction_isolation_and_sponsored_npc_lifecycle(self) -> None:
        hub_bin = os.path.join(ROOT_DIR, "intelligent-hub", "bin", "intelligent-hub.exe")
        if not os.path.exists(hub_bin):
            self.skipTest("intelligent-hub binary not built yet")

        tmp = tempfile.TemporaryDirectory()
        hub_proc = subprocess.Popen(
            [
                hub_bin,
                "-origin-addr", "127.0.0.1:0",
                "-core-addr", "127.0.0.1:0",
                "-consent-addr", "127.0.0.1:0",
                "-ledger", os.path.join(tmp.name, "mp_hub.json"),
                "-no-runtime-file",
                "-auto-approve-scope",
                "-print-ports-json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            info = json.loads(hub_proc.stdout.readline().strip())
            origin_url = f"http://{info['origin_addr']}"
            core_url = f"http://{info['core_addr']}"

            def _make_slot(slot_id: str, role: str, billing: str, player_id: str, faction: str) -> OriginSDK:
                tx = LoopbackHTTPTransportClient(origin_url)
                s = OriginSDK(
                    slot_id=slot_id,
                    hub_transport=tx,
                    slot_role_kind=role,
                    billing_mode=billing,
                    player_id=player_id,
                    room_id="match-room-42",
                    faction_id=faction,
                )
                s.start_loopback_execute_listener()
                s.bind_actor_subject("companion", {})
                s.register_capability(
                    CapabilityEntry("#10", "ping", "Ping", ["companion"], {}),
                    effect_handler=lambda a, subj, args, t: ImmediateSucceeded({}),
                )
                s.publish_capabilities_and_baseline()
                return s

            red_orig = _make_slot("slot-red-alice", "companion", "player_metered", "alice", "red")
            blue_orig = _make_slot("slot-blue-bob", "companion", "player_metered", "bob", "blue")
            ref_orig = _make_slot("slot-room-referee", "independent_npc", "developer_sponsored", "server", "")

            alice_core = CoreSDK(planner=HeuristicReferencePlanner(), allow_loopback=True, heartbeat_interval_sec=1.0)
            bob_core = CoreSDK(planner=HeuristicReferencePlanner(), allow_loopback=True, heartbeat_interval_sec=1.0)
            ref_core = CoreSDK(planner=HeuristicReferencePlanner(), allow_loopback=True, heartbeat_interval_sec=1.0)
            spy_core = CoreSDK(planner=HeuristicReferencePlanner(), allow_loopback=True, heartbeat_interval_sec=1.0)
            try:
                # 1. Cross-faction & cross-player authorization MUST be denied with 403 permission_denied!
                with self.assertRaises(G2AError) as ctx_fac:
                    spy_core.connect(core_url, "spy-blue", "slot-red-alice", faction_id="blue", start_background_loops=False)
                self.assertEqual(ctx_fac.exception.code, "permission_denied")

                with self.assertRaises(G2AError) as ctx_plr:
                    spy_core.connect(core_url, "spy-bob", "slot-red-alice", player_id="bob", start_background_loops=False)
                self.assertEqual(ctx_plr.exception.code, "permission_denied")

                # 2. Legitimate connections succeed
                alice_core.connect(core_url, "core-alice", "slot-red-alice", player_id="alice", faction_id="red")
                bob_core.connect(core_url, "core-bob", "slot-blue-bob", player_id="bob", faction_id="blue")
                ref_core.connect(core_url, "core-referee", "slot-room-referee")
                time.sleep(0.15)

                # 3. Alice disconnects while Bob remains in room (remaining_online_humans = 1):
                #    Alice's personal companion stops, while Bob's companion AND developer_sponsored Referee remain active!
                red_orig.report_player_presence(False, remaining_online_humans_in_room=1)
                self.assertTrue(_wait_until(lambda: alice_core.closed))
                self.assertFalse(bob_core.closed)
                self.assertFalse(ref_core.closed)

                # 4. Bob ALSO disconnects (remaining_online_humans = 0):
                #    Both Bob's companion AND the room's developer_sponsored Referee NPC stop immediately!
                blue_orig.report_player_presence(False, remaining_online_humans_in_room=0)
                self.assertTrue(_wait_until(lambda: bob_core.closed and ref_core.closed))
            finally:
                for c in (alice_core, bob_core, ref_core, spy_core):
                    c.shutdown_session_cleanly()
                for o in (red_orig, blue_orig, ref_orig):
                    o.stop_loopback_execute_listener()
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
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
