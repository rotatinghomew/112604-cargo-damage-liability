"""端到端演示：同一宗出口车损案件里查勘、承运与财务的协同。

本地同时启动报文重放（付款对手）与理赔协同服务，按样本资料走一遍：
交接确认与异议 → 垫款审批 → 付款失去应答后对账 → 追偿 → 归属调整与复核。
运行：python3 demo_case.py
"""

import http.client
import json
from pathlib import Path
from threading import Thread

from claims import ClaimService, PeerClient, create_server, seed_from_samples
from peer_replay import serve_recording

ROOT = Path(__file__).resolve().parent
SURVEYOR = {"X-Role": "surveyor"}
MANAGER = {"X-Role": "manager"}
FINANCE = {"X-Role": "finance"}


def party(name):
    return {"X-Role": "party", "X-Party": name}


class Client:
    def __init__(self, port):
        self.port = port

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


def show(title, payload):
    print(f"\n== {title} ==")
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def main():
    peer_server = serve_recording(ROOT / "samples" / "peer.json", 0)
    Thread(target=peer_server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    peer = PeerClient(f"http://127.0.0.1:{peer_server.server_port}")
    service = seed_from_samples(ClaimService(peer=peer), ROOT / "samples")
    server = create_server(service=service, port=0)
    Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    api = Client(server.server_port)
    print(f"理赔协同服务 127.0.0.1:{server.server_port}，付款对手 127.0.0.1:{peer_server.server_port}")

    try:
        # 1. 交接双方确认或异议；异议记为争议，不猜定责任人
        api.call("POST", "/checks/Q2/ack", party("yard"), {"decision": "confirm"})
        api.call("POST", "/checks/Q2/ack", party("haul"),
                 {"decision": "object", "reason": "接车时擦痕已存在，非本段造成"})
        show("报告 D1 的证据解释（按实际查勘时点）",
             api.call("GET", "/reports/D1", SURVEYOR)[1])

        # 2. 两个承运人的案件同时申请垫款
        api.call("POST", "/claims", MANAGER,
                 {"claim": "C1", "policy": "P-7", "vin": "V-41", "reports": ["D1"],
                  "amount_fen": 60000, "payee": "fixer-2"})
        show("C1 批准后的可执行承诺", api.call("POST", "/claims/C1/approve", MANAGER)[1])
        api.call("POST", "/claims", party("haul"),
                 {"claim": "C3", "policy": "P-8", "vin": "V-41", "reports": ["D3"],
                  "amount_fen": 68000, "payee": "fixer-1"})
        status, body = api.call("POST", "/claims/C3/approve", MANAGER)
        show(f"C3 批准被拒（{status}），待审原因", api.call("GET", "/claims/C3", MANAGER)[1]["reason"])

        # 3. 付款提交后失去应答：先查该笔是否已支付
        show("P07-D1-1 提交（对端断开）",
             api.call("POST", "/payments", FINANCE,
                      {"payment": "P07-D1-1", "claim": "C1", "party": "fixer-2",
                       "amount_fen": 60000})[1])
        api.call("POST", "/transfers/P07-D1-1/reconcile", FINANCE)
        show("对账后状态", api.call("POST", "/transfers/P07-D1-1/reconcile", FINANCE)[1])

        # 4. 追偿：份额经双方认可后发起，实际收到才计入回款
        api.call("POST", "/reports/D1/liabilities", MANAGER,
                 {"liability": "L1", "party": "haul", "amount_fen": 40000})
        api.call("POST", "/liabilities/L1/ack", party("haul"), {"decision": "accept"})
        api.call("POST", "/recoveries", FINANCE,
                 {"recovery": "P07-R1", "liability": "L1", "amount_fen": 40000})
        show("P07-R1 对账（追偿结果）", api.call("POST", "/transfers/P07-R1/reconcile", FINANCE)[1])

        # 5. 归属调整：未付款承诺复核，已付款保留对账，额度收紧列为后续处理
        api.call("POST", "/claims", MANAGER,
                 {"claim": "C2", "policy": "P-7", "vin": "V-42", "reports": ["D2"],
                  "amount_fen": 75000, "payee": "fixer-2"})
        api.call("POST", "/claims/C2/approve", MANAGER)
        api.call("POST", "/reports/D1/incident", SURVEYOR, {"incident": "J10"})
        show("归属调整后 C1 进入复核", api.call("GET", "/claims/C1", MANAGER)[1]["review_note"])
        show("保单 P-7 的后续处理项", api.call("GET", "/policies/P-7", MANAGER)[1]["followups"])
        api.call("POST", "/claims/C1/review", MANAGER, {"decision": "confirm"})

        # 6. 按一辆车 / 一次事故查清
        status, case = api.call("GET", "/vehicles/V-41/case", MANAGER)
        show("V-41 案件汇总（经理视角）", {
            "实际垫付": case["advances"], "追偿": case["recovery"],
            "未决事项": [item["kind"] for item in case["open_items"]],
        })
        status, case = api.call("GET", "/vehicles/V-41/case", party("yard"))
        show("V-41 案件（承运人 yard 视角，仅自己经手的区间）", {
            "可见区间": [item["segment"] for item in case["evidence"]],
            "责任份额": case["liabilities"],
        })
    finally:
        server.shutdown()
        peer_server.shutdown()


if __name__ == "__main__":
    main()
