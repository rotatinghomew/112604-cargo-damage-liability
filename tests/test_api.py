"""服务端接口：交接、理赔、追偿与按车/按事故查案。"""

import http.client
import json
from pathlib import Path
from threading import Thread
import unittest

from claims import ClaimService, PeerClient, create_server, seed_from_samples
from peer_replay import serve_recording

ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "samples" / "peer.json"
SURVEYOR = {"X-Role": "surveyor"}
MANAGER = {"X-Role": "manager"}
FINANCE = {"X-Role": "finance"}


def party(name):
    return {"X-Role": "party", "X-Party": name}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.peer_server = serve_recording(RECORDING, 0)
        cls.peer_thread = Thread(target=cls.peer_server.serve_forever,
                                 kwargs={"poll_interval": 0.02}, daemon=True)
        cls.peer_thread.start()
        peer = PeerClient(f"http://127.0.0.1:{cls.peer_server.server_port}")
        service = seed_from_samples(ClaimService(peer=peer), ROOT / "samples")
        cls.server = create_server(service=service, port=0)
        cls.thread = Thread(target=cls.server.serve_forever,
                            kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.peer_server.shutdown()
        cls.peer_thread.join()

    def call(self, method, path, headers, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body,
                               headers={"Content-Type": "application/json", **headers})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_full_case_flow(self):
        # 交接双方确认/异议
        status, _ = self.call("POST", "/checks/Q2/ack", party("yard"), {"decision": "confirm"})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/checks/Q2/ack", party("haul"),
                              {"decision": "object", "reason": "接车时擦痕已存在"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/checks/Q2/ack", party("terminal"), {"decision": "confirm"})
        self.assertEqual(status, 403)

        # 修理垫款：两个承运人的案件同时申请，先批准的有可执行承诺
        status, _ = self.call("POST", "/claims", MANAGER,
                              {"claim": "C1", "policy": "P-7", "vin": "V-41", "reports": ["D1"],
                               "amount_fen": 60000, "payee": "fixer-2"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/claims/C1/approve", MANAGER)
        self.assertEqual(status, 200)
        self.assertEqual(body["commitment"]["amount_fen"], 60000)
        # 待审申请看得到余量不足的原因
        status, _ = self.call("POST", "/claims", party("haul"),
                              {"claim": "C3", "policy": "P-8", "vin": "V-41", "reports": ["D3"],
                               "amount_fen": 68000, "payee": "fixer-1"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/claims/C3/approve", MANAGER)
        self.assertEqual(status, 409)
        self.assertIn("38000", body["错误"])
        status, body = self.call("GET", "/claims/C3", MANAGER)
        self.assertIn("38000", body["reason"])

        # 付款提交后失去应答：先对账，不能列为可重新付款
        status, body = self.call("POST", "/payments", FINANCE,
                                 {"payment": "P07-D1-1", "claim": "C1",
                                  "party": "fixer-2", "amount_fen": 60000})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "unknown")
        self.assertIs(body["repayable"], False)
        status, body = self.call("POST", "/transfers/P07-D1-1/reconcile", FINANCE)
        self.assertEqual(body["state"], "submitted")
        status, body = self.call("POST", "/transfers/P07-D1-1/reconcile", FINANCE)
        self.assertEqual(body["state"], "settled")
        self.assertEqual(body["settlement"], "Z-0019")

        # 追偿只依据双方认可的份额；实际收到才计入回款
        status, _ = self.call("POST", "/reports/D1/liabilities", MANAGER,
                              {"liability": "L1", "party": "haul", "amount_fen": 40000})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/recoveries", FINANCE,
                                 {"recovery": "P07-R1", "liability": "L1", "amount_fen": 40000})
        self.assertEqual(status, 409)
        status, _ = self.call("POST", "/liabilities/L1/ack", party("haul"), {"decision": "accept"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/recoveries", FINANCE,
                                 {"recovery": "P07-R1", "liability": "L1", "amount_fen": 40000})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "unknown")  # 录制中无此提交，按失去应答处理
        status, body = self.call("POST", "/transfers/P07-R1/reconcile", FINANCE)
        self.assertEqual(body["state"], "settled")

        # 按一辆车查清证据、实际垫付、待追偿金额和未决事项
        status, case = self.call("GET", "/vehicles/V-41/case", MANAGER)
        self.assertEqual(status, 200)
        self.assertEqual(case["advances"]["paid_fen"], 60000)
        self.assertEqual(case["recovery"]["received_fen"], 40000)
        self.assertEqual(case["recovery"]["pending_fen"], 0)
        kinds = {item["kind"] for item in case["open_items"]}
        self.assertIn("争议", kinds)
        # 按一次事故查（J9 涉及 V-41 与 V-42 两辆车）
        status, case = self.call("GET", "/incidents/J9/case", MANAGER)
        self.assertEqual(status, 200)
        self.assertEqual(case["vins"], ["V-41", "V-42"])
        damages = {d["report"]: d for d in case["damages"]}
        self.assertEqual([leg["segment"] for leg in damages["D1"]["candidate_legs"]], ["yard->haul"])

        # 共同事故归属改变：未付款承诺复核，已付款保留对账
        status, _ = self.call("POST", "/claims", MANAGER,
                              {"claim": "C2", "policy": "P-7", "vin": "V-42", "reports": ["D2"],
                               "amount_fen": 75000, "payee": "fixer-2"})
        self.assertEqual(status, 200)
        self.call("POST", "/claims/C2/approve", MANAGER)
        status, _ = self.call("POST", "/reports/D1/incident", SURVEYOR, {"incident": "J10"})
        self.assertEqual(status, 200)
        status, claim = self.call("GET", "/claims/C1", MANAGER)
        self.assertEqual(claim["state"], "review")
        status, policy = self.call("GET", "/policies/P-7", MANAGER)
        self.assertEqual(status, 200)
        self.assertTrue(policy["followups"])  # 额度收紧的不足列为后续处理
        status, body = self.call("POST", "/claims/C1/review", MANAGER, {"decision": "confirm"})
        self.assertEqual(status, 200)
        status, transfer = self.call("GET", "/transfers/P07-D1-1", FINANCE)
        self.assertEqual(transfer["state"], "settled")  # 已付款的垫款仍能对账

        # 租户只能看到自己经手的区间及责任和证据
        status, case = self.call("GET", "/vehicles/V-41/case", party("yard"))
        self.assertEqual(status, 200)
        self.assertEqual({item["segment"] for item in case["evidence"]},
                         {"factory->yard", "yard->haul"})
        self.assertNotIn("advances", case)
        status, case = self.call("GET", "/vehicles/V-41/case", party("haul"))
        self.assertEqual([l["liability"] for l in case["liabilities"]], ["L1"])

    def test_role_required(self):
        status, _ = self.call("GET", "/vehicles/V-41/case", {})
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/claims/C1/approve", FINANCE)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
