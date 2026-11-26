"""付款对手客户端：失去应答、对账与状态推进。"""

from pathlib import Path
from threading import Thread
import unittest

from claims import PeerClient
from peer_replay import serve_recording

RECORDING = Path(__file__).resolve().parents[1] / "samples" / "peer.json"


class PeerClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve_recording(RECORDING, 0)
        cls.thread = Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.client = PeerClient(f"http://127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def test_submit_loses_response_then_reconcile(self):
        # 录制中该笔提交直接断开：客户端不判死，报告 unknown
        result = self.client.submit("P07-D1-1", "repair", "fixer-2", 60000)
        self.assertEqual(result["result"], "unknown")
        # 财务先查该笔是否已支付：pending -> settled
        first = self.client.inquire("P07-D1-1")
        self.assertEqual(first["result"], "status")
        self.assertEqual(first["body"]["status"], "pending")
        second = self.client.inquire("P07-D1-1")
        self.assertEqual(second["body"]["status"], "settled")
        self.assertEqual(second["body"]["settlement"], "Z-0019")

    def test_recovery_result_visible_by_inquiry(self):
        result = self.client.inquire("P07-R1")
        self.assertEqual(result["result"], "status")
        self.assertEqual(result["body"]["status"], "settled")
        self.assertEqual(result["body"]["kind"], "recovery")

    def test_unknown_ref_is_not_found(self):
        self.assertEqual(self.client.inquire("NO-SUCH-REF")["result"], "not_found")


if __name__ == "__main__":
    unittest.main()
