"""付款对手接口客户端。

POST /transfers 提交汇款或追偿，GET /transfers/{ref} 查询状态。
提交后失去应答时不判死：调用方应先对账确认该笔是否已支付。
"""

import http.client
import json
from urllib.parse import urlsplit


class PeerClient:
    def __init__(self, base_url="http://127.0.0.1:8087", timeout=5):
        parsed = urlsplit(base_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.timeout = timeout

    def _call(self, method, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            content = response.read()
            try:
                decoded = json.loads(content) if content else {}
            except ValueError:
                decoded = {}
            return response.status, decoded
        except (OSError, http.client.HTTPException):
            return 0, {}
        finally:
            connection.close()

    def submit(self, ref, kind, party, amount_fen):
        """提交一笔汇款/追偿。失去应答返回 unknown，由调用方对账。"""
        status, body = self._call(
            "POST", "/transfers",
            {"ref": ref, "kind": kind, "party": party, "amount_fen": amount_fen},
        )
        if 200 <= status < 300:
            return {"result": "accepted", "body": body}
        if status == 409:
            return {"result": "conflict", "body": body}
        return {"result": "unknown", "status": status, "body": body}

    def inquire(self, ref):
        """查询一笔的状态：pending / settled / rejected；尚不可见为 not_found。"""
        status, body = self._call("GET", f"/transfers/{ref}", None)
        if status == 200:
            return {"result": "status", "body": body}
        if status == 404:
            return {"result": "not_found"}
        return {"result": "unknown", "status": status, "body": body}
