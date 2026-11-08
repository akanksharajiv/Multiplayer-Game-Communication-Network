from pathlib import Path
import hashlib
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import launch_project
import multiplayer_demo
import d2_dynamic_person2_test


class ProjectIntegrityTests(unittest.TestCase):
    def test_receiver_dependency_resolves(self):
        self.assertEqual(launch_project.RECEIVER_FILE.name, "receiver_new.py")
        self.assertTrue(launch_project.RECEIVER_FILE.is_file())

    def test_d1_controller_uses_protocol_aware_forwarding(self):
        source = (ROOT / "game_sdn_controller.py").read_text()
        self.assertIn('"eth_type": eth.ethertype', source)
        self.assertIn('fields["ip_proto"] = ip_pkt.proto', source)
        self.assertIn('fields["udp_dst"] = udp_pkt.dst_port', source)
        self.assertNotIn('match = parser.OFPMatch(\n                    in_port=in_port,\n                    eth_dst=eth.dst,\n                )', source)

    def test_approved_d2_controller_hash_is_preserved(self):
        data = (ROOT / "game_sdn_controller_person3_D2.py").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(digest, d2_dynamic_person2_test.APPROVED_CONTROLLER_SHA256)

    def test_dynamic_thresholds_are_explicit(self):
        env = d2_dynamic_person2_test.DYNAMIC_CONTROLLER_ENV
        self.assertEqual(env["CONGESTION_THRESHOLD"], "50")
        self.assertEqual(env["RECOVERY_THRESHOLD"], "30")
        self.assertEqual(env["CONGESTION_CONFIRM_SAMPLES"], "2")
        self.assertEqual(env["RECOVERY_CONFIRM_SAMPLES"], "3")

    def test_d1_demo_requires_game_flows_on_queue_1(self):
        flows = (
            " cookie=0x0, duration=2s, table=0, n_packets=3, "
            "priority=200,udp,tp_dst=5000 actions=set_queue:1,output:2"
        )
        self.assertEqual(multiplayer_demo.count_game_queue_flows(flows), 1)
        self.assertEqual(multiplayer_demo.count_game_queue_flows(""), 0)
        self.assertEqual(
            multiplayer_demo.count_game_queue_flows(
                "priority=10,udp,tp_dst=5000 actions=output:2"
            ),
            0,
        )

    def test_robustness_passes_only_with_outage_and_recovery_evidence(self):
        rows = [
            {"target": "2", "elapsed_s": "2.0", "reply_received": "1"},
            {"target": "2", "elapsed_s": "6.0", "reply_received": "0"},
            {"target": "3", "elapsed_s": "6.5", "reply_received": "1"},
            {"target": "2", "elapsed_s": "11.0", "reply_received": "1"},
            {"target": "3", "elapsed_s": "11.5", "reply_received": "1"},
        ]
        checks = multiplayer_demo.evaluate_robustness_records(
            rows, player=1, other_player=3, offline_start=5, offline_duration=5
        )
        self.assertTrue(all(checks.values()))
        incomplete = [row for row in rows if row["target"] != "2"]
        failed = multiplayer_demo.evaluate_robustness_records(
            incomplete, player=1, other_player=3, offline_start=5, offline_duration=5
        )
        self.assertFalse(failed["ack_before_outage"])
        self.assertFalse(failed["timeout_during_outage"])
        self.assertFalse(failed["ack_after_recovery"])


if __name__ == "__main__":
    unittest.main()
