"""理赔协同服务的 HTTP 接口。

角色（请求头 X-Role）：
- surveyor 查勘员/查勘机构：提交交接证据、确认共同事故编号、提交车损报告；
- party    承运人（须同时给 X-Party）：确认或异议自己经手的交接、补交区间证据、
           认领或保留责任份额异议、用自己的保单申请垫款；只能看到自己经手的区间；
- manager  理赔经理：批准垫款、复核承诺与份额、登记保单、查整案；
- finance  财务：执行修理付款与追偿、对账。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import ClaimService, ServiceError

ROLES = {"manager", "finance", "surveyor", "party"}


def create_server(service=None, peer=None, port=8090, state_path=None):
    if service is None:
        service = ClaimService(peer=peer)

    def maybe_save():
        if state_path:
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(service.to_snapshot(), handle, ensure_ascii=False, indent=1)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------- 基础 ----------

        def log_message(self, fmt, *args):
            return

        def send_json(self, status, payload):
            content = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def viewer(self):
            role = self.headers.get("X-Role", "")
            party = self.headers.get("X-Party", "")
            if role not in ROLES:
                raise ServiceError(401, "缺少或未知角色（X-Role 须为 manager/finance/surveyor/party）")
            if role == "party" and not party:
                raise ServiceError(401, "承运人角色须给出 X-Party")
            return {"role": role, "party": party}

        def require(self, who, *roles):
            if who["role"] not in roles:
                raise ServiceError(403, f"该操作须由 {'/'.join(roles)} 角色发起")

        def body(self):
            size = int(self.headers.get("Content-Length", 0))
            if size > 1_048_576:
                raise ServiceError(413, "请求体过大")
            if not size:
                return {}
            try:
                decoded = json.loads(self.rfile.read(size))
            except ValueError:
                raise ServiceError(400, "请求体不是有效 JSON")
            if not isinstance(decoded, dict):
                raise ServiceError(400, "请求体须为 JSON 对象")
            return decoded

        # ---------- 路由 ----------

        def route(self, method):
            try:
                who = self.viewer()
                payload = self.body() if method == "POST" else {}
                result = self.dispatch(method, self.path, who, payload)
                if method == "POST":
                    maybe_save()
                self.send_json(200, result if result is not None else {"ok": True})
            except ServiceError as error:
                self.send_json(error.status, {"错误": error.message})
            except BrokenPipeError:
                pass

        def dispatch(self, method, path, who, payload):
            # 证据与交接
            if method == "POST" and path == "/evidence":
                self.require(who, "surveyor")
                return service.add_evidence(payload)
            if method == "POST" and path == "/checks":
                self.require(who, "surveyor", "party")
                if who["role"] == "party" and who["party"] not in (
                    payload.get("from_party"), payload.get("to_party")
                ):
                    raise ServiceError(403, "承运人只能补交自己经手区间的证据")
                return service.add_check(payload)
            match = re.fullmatch(r"/checks/([^/]+)/ack", path)
            if method == "POST" and match:
                self.require(who, "party")
                return service.ack_check(
                    match.group(1), who["party"], payload.get("decision", ""), payload.get("reason", "")
                )
            match = re.fullmatch(r"/checks/([^/]+)", path)
            if method == "GET" and match:
                check = service._check(match.group(1))
                if not service._check_visible(check, who):
                    raise ServiceError(403, "租户只能查看自己经手的区间")
                return service._check_view(check)

            # 共同事故与车损报告
            if method == "POST" and path == "/incidents":
                self.require(who, "surveyor")
                return service.confirm_incident(payload.get("incident", ""), payload.get("agency", ""))
            if method == "POST" and path == "/reports":
                self.require(who, "surveyor")
                return service.add_report(payload)
            match = re.fullmatch(r"/reports/([^/]+)/incident", path)
            if method == "POST" and match:
                self.require(who, "surveyor")
                return service.reattribute_report(
                    match.group(1), payload.get("incident", ""), by="survey"
                )
            match = re.fullmatch(r"/reports/([^/]+)", path)
            if method == "GET" and match:
                return service.interpret_report(match.group(1))

            # 责任份额
            match = re.fullmatch(r"/reports/([^/]+)/liabilities", path)
            if method == "POST" and match:
                self.require(who, "manager", "party")
                party = payload.get("party", "")
                if who["role"] == "party" and party != who["party"]:
                    raise ServiceError(403, "承运人只能认领自己的份额")
                by = who["party"] if who["role"] == "party" else "manager"
                return service.propose_liability(
                    payload.get("liability", ""), match.group(1), party,
                    payload.get("amount_fen", 0), by, payload.get("note", ""),
                )
            match = re.fullmatch(r"/liabilities/([^/]+)/ack", path)
            if method == "POST" and match:
                self.require(who, "party")
                return service.ack_liability(
                    match.group(1), who["party"], payload.get("decision", ""), payload.get("reason", "")
                )
            match = re.fullmatch(r"/liabilities/([^/]+)/review", path)
            if method == "POST" and match:
                self.require(who, "manager")
                return service.review_liability(match.group(1), by="manager")

            # 保单
            if method == "POST" and path == "/policies":
                self.require(who, "manager")
                return service.add_policy(payload)
            match = re.fullmatch(r"/policies/([^/]+)", path)
            if method == "GET" and match:
                self.require(who, "manager", "finance")
                return service.policy_view(match.group(1))

            # 垫款申请与审批
            if method == "POST" and path == "/claims":
                self.require(who, "manager", "party")
                policy = service._policy(payload.get("policy", ""))
                if who["role"] == "party" and policy["party"] != who["party"]:
                    raise ServiceError(403, "承运人只能用自己的保单申请垫款")
                applicant = who["party"] if who["role"] == "party" else "manager"
                return service.submit_claim(
                    payload.get("claim", ""), policy["policy"], payload.get("vin", ""),
                    payload.get("reports", []), payload.get("amount_fen", 0),
                    payload.get("payee", ""), applicant,
                )
            match = re.fullmatch(r"/claims/([^/]+)/(approve|reject|review)", path)
            if method == "POST" and match:
                self.require(who, "manager")
                claim_id, action = match.group(1), match.group(2)
                if action == "approve":
                    return service.approve_claim(claim_id, by="manager")
                if action == "reject":
                    return service.reject_claim(claim_id, by="manager", reason=payload.get("reason", ""))
                return service.review_claim(
                    claim_id, payload.get("decision", ""), by="manager",
                    amount_fen=payload.get("amount_fen"), incident=payload.get("incident"),
                )
            match = re.fullmatch(r"/claims/([^/]+)", path)
            if method == "GET" and match:
                self.require(who, "manager", "finance")
                return service.claim_view(match.group(1))

            # 付款、追偿与对账
            if method == "POST" and path == "/payments":
                self.require(who, "finance")
                transfer = service.execute_payment(
                    payload.get("payment", ""), payload.get("claim", ""),
                    payload.get("party", ""), payload.get("amount_fen", 0),
                )
                return service.transfer_view(transfer["ref"])
            if method == "POST" and path == "/recoveries":
                self.require(who, "finance")
                transfer = service.execute_recovery(
                    payload.get("recovery", ""), payload.get("liability", ""),
                    payload.get("amount_fen", 0),
                )
                return service.transfer_view(transfer["ref"])
            match = re.fullmatch(r"/transfers/([^/]+)/reconcile", path)
            if method == "POST" and match:
                self.require(who, "finance")
                return service.transfer_view(service.reconcile_transfer(match.group(1))["ref"])
            match = re.fullmatch(r"/transfers/([^/]+)", path)
            if method == "GET" and match:
                self.require(who, "manager", "finance")
                return service.transfer_view(match.group(1))

            # 案件查询
            match = re.fullmatch(r"/vehicles/([^/]+)/case", path)
            if method == "GET" and match:
                return service.vehicle_case(match.group(1), who)
            match = re.fullmatch(r"/incidents/([^/]+)/case", path)
            if method == "GET" and match:
                return service.incident_case(match.group(1), who)

            raise ServiceError(404, "接口不存在")

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

    return HTTPServer(("127.0.0.1", port), Handler)
