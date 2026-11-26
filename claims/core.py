"""转运车损理赔协同的核心状态与规则。

查勘员、承运人和财务在同一宗案件里各自推进自己的决定：

- 交接证据按实际查勘时点解释，保留当时交接是否已有该伤以及哪几段还可能担责；
- 同部位第二次受损独立申请、独立确认，不并案；
- 证据不足时只记争议，不猜定责任人；
- 修理垫款按事故可赔总额（同事故共用一份免赔额）和保单剩余额度（跨事故共用）审批；
- 共同事故归属改变时，未付款承诺复核并停止占用原认定，已付款垫款保留对账；
- 修理付款与追偿分别计数，追偿只依据双方认可或复核完毕的责任份额。
"""

from datetime import datetime, timezone

FINDINGS = {"clear", "scratch", "dent"}
ACK_DECISIONS = {"confirm", "object"}
LIABILITY_FINAL = {"accepted", "reviewed"}


class ServiceError(Exception):
    """业务规则拒绝，携带 HTTP 状态码与中文说明。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _survey_time(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ServiceError(400, "时间须为带 Z 的 ISO 8601 UTC 表示")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ServiceError(400, f"时间无法解析：{value!r}")
    if parsed.tzinfo != timezone.utc:
        raise ServiceError(400, "时间须为 UTC")
    return parsed


def _sort_key(check):
    return (_survey_time(check["at"]), check["check"])


def _segment(check):
    return f'{check["from_party"]}->{check["to_party"]}'


class ClaimService:
    """同一宗案件内查勘、承运与财务各自推进的协同状态。"""

    def __init__(self, peer=None):
        self.peer = peer
        self.state = {
            "evidence": {},       # evidence_ref -> 照片索引登记
            "checks": {},         # check -> 交接查勘记录（含双方确认/异议）
            "incidents": {},      # incident -> 查勘机构确认的共同事故编号
            "reports": {},        # report -> 车损报告（同部位第二次受损独立存在）
            "report_policy": {},  # report -> 首次申请时绑定的保单
            "policies": {},       # policy -> 保单（免赔额按事故，上限跨事故共用）
            "liabilities": {},    # liability -> 责任份额
            "claims": {},         # claim -> 修理垫款申请/承诺
            "transfers": {},      # ref -> 付款/追偿指令（修理与追偿分别计数）
        }

    # ---------- 快照（可选持久化） ----------

    def to_snapshot(self):
        return self.state

    @classmethod
    def from_snapshot(cls, state, peer=None):
        service = cls(peer=peer)
        service.state = state
        return service

    # ---------- 查找辅助 ----------

    def _check(self, check_id):
        try:
            return self.state["checks"][check_id]
        except KeyError:
            raise ServiceError(404, f"交接查勘记录不存在：{check_id}")

    def _report(self, report_id):
        try:
            return self.state["reports"][report_id]
        except KeyError:
            raise ServiceError(404, f"车损报告不存在：{report_id}")

    def _incident(self, incident):
        try:
            return self.state["incidents"][incident]
        except KeyError:
            raise ServiceError(409, f"共同事故编号未经查勘机构确认：{incident}")

    def _policy(self, policy_id):
        try:
            return self.state["policies"][policy_id]
        except KeyError:
            raise ServiceError(404, f"保单不存在：{policy_id}")

    def _claim(self, claim_id):
        try:
            return self.state["claims"][claim_id]
        except KeyError:
            raise ServiceError(404, f"垫款申请不存在：{claim_id}")

    def _liability(self, liability_id):
        try:
            return self.state["liabilities"][liability_id]
        except KeyError:
            raise ServiceError(404, f"责任份额不存在：{liability_id}")

    def _transfer(self, ref):
        try:
            return self.state["transfers"][ref]
        except KeyError:
            raise ServiceError(404, f"付款/追偿指令不存在：{ref}")

    # ---------- 证据与交接 ----------

    def add_evidence(self, rec):
        ref = rec.get("evidence_ref")
        if not ref:
            raise ServiceError(400, "缺少 evidence_ref")
        if ref in self.state["evidence"]:
            raise ServiceError(409, f"照片索引已登记：{ref}")
        entry = {"evidence_ref": ref, "side": rec.get("side"), "caption": rec.get("caption", "")}
        self.state["evidence"][ref] = entry
        return entry

    def add_check(self, rec):
        """查勘员或有区间证据的交接方提交交接查勘记录。

        解释一律以记录中的查勘时点 at 为准，与提交先后无关。
        """
        required = ["check", "vin", "from_party", "to_party", "at", "area", "mark", "finding", "evidence_ref"]
        missing = [field for field in required if not rec.get(field)]
        if missing:
            raise ServiceError(400, f"交接记录缺少字段：{', '.join(missing)}")
        if rec["finding"] not in FINDINGS:
            raise ServiceError(400, f"finding 须为 {'/'.join(sorted(FINDINGS))}")
        _survey_time(rec["at"])
        if rec["check"] in self.state["checks"]:
            raise ServiceError(409, f"交接查勘记录已存在：{rec['check']}")
        entry = {field: rec[field] for field in required}
        entry["acks"] = {}
        self.state["checks"][entry["check"]] = entry
        if rec.get("caption") or rec.get("side"):
            ref = entry["evidence_ref"]
            if ref not in self.state["evidence"]:
                self.state["evidence"][ref] = {
                    "evidence_ref": ref,
                    "side": rec.get("side"),
                    "caption": rec.get("caption", ""),
                }
        return entry

    def ack_check(self, check_id, party, decision, reason=""):
        """交接双方确认或提出异议；异议即记争议，双方可查看并补交区间证据。"""
        check = self._check(check_id)
        if party not in (check["from_party"], check["to_party"]):
            raise ServiceError(403, f"{party} 不是交接 {check_id} 的当事方，不能确认或异议")
        if decision not in ACK_DECISIONS:
            raise ServiceError(400, "decision 须为 confirm 或 object")
        if decision == "object" and not reason:
            raise ServiceError(400, "提出异议须说明理由")
        check["acks"][party] = {"decision": decision, "reason": reason}
        return check

    # ---------- 共同事故与车损报告 ----------

    def confirm_incident(self, incident, agency):
        """共同事故编号由查勘机构确认。"""
        if not incident or not agency:
            raise ServiceError(400, "缺少 incident 或 agency")
        existing = self.state["incidents"].get(incident)
        if existing:
            if existing["agency"] != agency:
                raise ServiceError(409, f"共同事故 {incident} 已由 {existing['agency']} 确认")
            return existing
        entry = {"incident": incident, "agency": agency}
        self.state["incidents"][incident] = entry
        return entry

    def add_report(self, rec):
        """车损报告：一次事故可涉及多车，一车多处受损可各自关联不同事故。

        同部位第二次受损引用各自的查勘记录，独立存在，不并案。
        """
        required = ["report", "incident", "vin", "area", "check", "quote_fen", "issuer"]
        missing = [field for field in required if rec.get(field) in (None, "")]
        if missing:
            raise ServiceError(400, f"车损报告缺少字段：{', '.join(missing)}")
        if not isinstance(rec["quote_fen"], int) or rec["quote_fen"] <= 0:
            raise ServiceError(400, "quote_fen 须为正整数（分）")
        self._incident(rec["incident"])
        check = self._check(rec["check"])
        if check["vin"] != rec["vin"] or check["area"] != rec["area"]:
            raise ServiceError(400, "报告的车架号/部位须与所引查勘记录一致")
        if rec["report"] in self.state["reports"]:
            raise ServiceError(409, f"车损报告已存在：{rec['report']}")
        for other in self.state["reports"].values():
            if other["check"] == rec["check"]:
                raise ServiceError(409, f"查勘记录 {rec['check']} 已有报告，第二次受损应引用各自的查勘记录")
        entry = {field: rec[field] for field in required}
        entry["history"] = []
        self.state["reports"][entry["report"]] = entry
        return entry

    def interpret_report(self, report_id):
        """按实际查勘时点解释：当时交接是否已有该伤、哪几段还可能担责。

        只列出可能担责的区间，不猜定某个责任人。
        """
        report = self._report(report_id)
        vin, area = report["vin"], report["area"]
        first = self._check(report["check"])
        finding = first["finding"]
        area_checks = sorted(
            (c for c in self.state["checks"].values() if c["vin"] == vin and c["area"] == area),
            key=_sort_key,
        )
        prior = None
        for candidate in area_checks:
            if _sort_key(candidate) < _sort_key(first):
                prior = candidate
        legs = {}
        for c in self.state["checks"].values():
            if c["vin"] == vin:
                legs.setdefault((c["from_party"], c["to_party"], c["at"]), c)
        ordered = sorted(legs.values(), key=_sort_key)
        if prior is None:
            candidates = [leg for leg in ordered if _sort_key(leg) <= _sort_key(first)]
            present = None
        else:
            candidates = [leg for leg in ordered if _sort_key(prior) < _sort_key(leg) <= _sort_key(first)]
            present = prior["finding"] == finding
        return {
            "report": report_id,
            "incident": report["incident"],
            "vin": vin,
            "area": area,
            "finding": finding,
            "quote_fen": report["quote_fen"],
            "issuer": report["issuer"],
            "first_observed": {"check": first["check"], "at": first["at"], "segment": _segment(first)},
            "prior_observation": (
                {"check": prior["check"], "at": prior["at"], "finding": prior["finding"]} if prior else None
            ),
            "damage_present_at_prior": present,
            "candidate_legs": [
                {"check": leg["check"], "segment": _segment(leg), "at": leg["at"]} for leg in candidates
            ],
            "note": "按实际查勘时点解释；仅列出可能担责区间，不指定单一责任人",
        }

    def reattribute_report(self, report_id, incident, by):
        """共同事故归属改变：未付款承诺复核并停止占用原认定，已付款保留对账。"""
        report = self._report(report_id)
        self._incident(incident)
        old = report["incident"]
        if old == incident:
            return report
        report["incident"] = incident
        report["history"].append({"from": old, "to": incident, "by": by})
        for claim in self.state["claims"].values():
            if report_id in claim["reports"] and claim["state"] == "approved":
                claim["state"] = "review"
                claim["commitment"] = None
                claim["review_note"] = (
                    f"报告 {report_id} 的共同事故归属由 {old} 调整为 {incident}；"
                    "未付款承诺停止占用原认定并待复核，已付款部分保留对账、不重复支付"
                )
                claim["history"].append(f"归属调整 {old}->{incident}，承诺进入复核")
            elif report_id in claim["reports"] and claim["state"] == "pending":
                claim["history"].append(f"归属调整 {old}->{incident}，待审原因已重算")
        return report

    # ---------- 保单与额度 ----------

    def add_policy(self, rec):
        required = ["policy", "agent", "party", "deductible_fen", "limit_fen"]
        missing = [field for field in required if rec.get(field) in (None, "")]
        if missing:
            raise ServiceError(400, f"保单缺少字段：{', '.join(missing)}")
        for field in ("deductible_fen", "limit_fen"):
            if not isinstance(rec[field], int) or rec[field] < 0:
                raise ServiceError(400, f"{field} 须为非负整数（分）")
        if rec["policy"] in self.state["policies"]:
            raise ServiceError(409, f"保单已存在：{rec['policy']}")
        entry = {field: rec[field] for field in required}
        self.state["policies"][entry["policy"]] = entry
        return entry

    def _occupancy(self):
        """额度占用：批准的未付承诺占当前归属，已付款占付款时的归属。"""
        by_pair = {}
        by_policy = {}

        def add(policy, incident, amount):
            if amount <= 0:
                return
            by_pair[(policy, incident)] = by_pair.get((policy, incident), 0) + amount
            by_policy[policy] = by_policy.get(policy, 0) + amount

        for claim in self.state["claims"].values():
            paid_by_incident = {}
            for t in self.state["transfers"].values():
                if t["kind"] == "repair" and t.get("claim") == claim["claim"] and t["state"] == "settled":
                    paid_by_incident[t["incident"]] = paid_by_incident.get(t["incident"], 0) + t["amount_fen"]
            if claim["state"] == "approved":
                committed = claim["amount_fen"] - sum(paid_by_incident.values())
                add(claim["policy"], claim["incident"], committed)
            for incident, amount in paid_by_incident.items():
                add(claim["policy"], incident, amount)
        return {"by_pair": by_pair, "by_policy": by_policy}

    def capacity(self, policy_id, incident):
        """事故可赔总额（共用一份免赔额）与保单剩余额度（跨事故共用）。"""
        policy = self._policy(policy_id)
        quotes = sum(
            r["quote_fen"]
            for r in self.state["reports"].values()
            if r["incident"] == incident and self.state["report_policy"].get(r["report"]) == policy_id
        )
        payable = max(0, quotes - policy["deductible_fen"])
        occ = self._occupancy()
        used_incident = occ["by_pair"].get((policy_id, incident), 0)
        used_policy = occ["by_policy"].get(policy_id, 0)
        return {
            "policy": policy_id,
            "incident": incident,
            "quote_total_fen": quotes,
            "deductible_fen": policy["deductible_fen"],
            "payable_fen": payable,
            "incident_used_fen": used_incident,
            "incident_remaining_fen": payable - used_incident,
            "policy_limit_fen": policy["limit_fen"],
            "policy_used_fen": used_policy,
            "policy_remaining_fen": policy["limit_fen"] - used_policy,
        }

    def shortfalls(self):
        """因额度收紧造成的不足，列为可解释的后续处理。"""
        occ = self._occupancy()
        items = []
        for (policy_id, incident), used in sorted(occ["by_pair"].items()):
            cap = self.capacity(policy_id, incident)
            if used > cap["payable_fen"]:
                items.append({
                    "kind": "额度不足后续处理",
                    "policy": policy_id,
                    "incident": incident,
                    "used_fen": used,
                    "payable_fen": cap["payable_fen"],
                    "shortfall_fen": used - cap["payable_fen"],
                    "note": (
                        f"事故 {incident} 在保单 {policy_id} 下已占用 {used} 分，"
                        f"超出当前可赔总额 {cap['payable_fen']} 分；差额列为后续处理，"
                        "已付款部分保留对账、不按新归属重复支付"
                    ),
                })
        for policy_id, used in sorted(occ["by_policy"].items()):
            policy = self.state["policies"][policy_id]
            if used > policy["limit_fen"]:
                items.append({
                    "kind": "额度不足后续处理",
                    "policy": policy_id,
                    "incident": None,
                    "used_fen": used,
                    "payable_fen": policy["limit_fen"],
                    "shortfall_fen": used - policy["limit_fen"],
                    "note": f"保单 {policy_id} 累计占用 {used} 分超出上限 {policy['limit_fen']} 分，差额列为后续处理",
                })
        return items

    # ---------- 责任份额 ----------

    def propose_liability(self, liability_id, report_id, party, amount_fen, by, note=""):
        """登记责任份额建议；承运人自己提出即视为认领。"""
        if not liability_id:
            raise ServiceError(400, "缺少 liability")
        if liability_id in self.state["liabilities"]:
            raise ServiceError(409, f"责任份额已存在：{liability_id}")
        self._report(report_id)
        if not isinstance(amount_fen, int) or amount_fen <= 0:
            raise ServiceError(400, "amount_fen 须为正整数（分）")
        state = "accepted" if by == party else "proposed"
        entry = {
            "liability": liability_id,
            "report": report_id,
            "party": party,
            "amount_fen": amount_fen,
            "state": state,
            "note": note,
            "reason": "",
        }
        self.state["liabilities"][liability_id] = entry
        return entry

    def ack_liability(self, liability_id, party, decision, reason=""):
        """承运人认领或保留异议；逾期未认领的份额保持未决，不猜定责任人。"""
        liability = self._liability(liability_id)
        if party != liability["party"]:
            raise ServiceError(403, f"只能由份额当事方 {liability['party']} 认领或异议")
        if decision == "accept":
            liability["state"] = "accepted"
            liability["reason"] = ""
        elif decision == "object":
            if not reason:
                raise ServiceError(400, "保留异议须说明理由")
            liability["state"] = "objected"
            liability["reason"] = reason
        else:
            raise ServiceError(400, "decision 须为 accept 或 object")
        return liability

    def review_liability(self, liability_id, by):
        """理赔经理复核完毕的份额可作为追偿依据。"""
        liability = self._liability(liability_id)
        if liability["state"] in LIABILITY_FINAL:
            raise ServiceError(409, f"份额 {liability_id} 已确认，无需复核")
        liability["state"] = "reviewed"
        liability["reviewed_by"] = by
        return liability

    # ---------- 垫款申请与审批 ----------

    def submit_claim(self, claim_id, policy_id, vin, reports, amount_fen, payee, applicant):
        """维修清单逐车报价；同一清单的报告须属于同一共同事故。"""
        self._policy(policy_id)
        if not claim_id:
            raise ServiceError(400, "缺少 claim")
        if claim_id in self.state["claims"]:
            raise ServiceError(409, f"垫款申请已存在：{claim_id}")
        if not reports:
            raise ServiceError(400, "维修清单至少包含一笔报告")
        if not isinstance(amount_fen, int) or amount_fen <= 0:
            raise ServiceError(400, "amount_fen 须为正整数（分）")
        incidents = set()
        for report_id in reports:
            report = self._report(report_id)
            if report["vin"] != vin:
                raise ServiceError(400, f"维修清单逐车报价，报告 {report_id} 不属于车辆 {vin}")
            incidents.add(report["incident"])
            mapped = self.state["report_policy"].get(report_id)
            if mapped and mapped != policy_id:
                raise ServiceError(409, f"报告 {report_id} 已在保单 {mapped} 下申请")
        entry = {
            "claim": claim_id,
            "policy": policy_id,
            "vin": vin,
            "reports": list(reports),
            "amount_fen": amount_fen,
            "payee": payee,
            "applicant": applicant,
            "incident": incidents.pop() if len(incidents) == 1 else None,
            "state": "pending",
            "reason": "",
            "commitment": None,
            "review_note": "",
            "history": [f"{applicant} 提交申请 {amount_fen} 分"],
        }
        for report_id in reports:
            self.state["report_policy"][report_id] = policy_id
        self.state["claims"][claim_id] = entry
        entry["reason"] = self._pending_reason(entry)
        return entry

    def _claim_incidents(self, claim):
        return {self.state["reports"][r]["incident"] for r in claim["reports"]}

    def _pending_reason(self, claim):
        incidents = self._claim_incidents(claim)
        if len(incidents) != 1:
            return "清单内报告的共同事故归属已分化，需先复核调整"
        incident = incidents.pop()
        cap = self.capacity(claim["policy"], incident)
        if claim["amount_fen"] > cap["incident_remaining_fen"]:
            return (
                f"事故 {incident} 可赔余额 {cap['incident_remaining_fen']} 分"
                f"（报价合计 {cap['quote_total_fen']} 分，共用免赔额 {cap['deductible_fen']} 分，"
                f"已占用 {cap['incident_used_fen']} 分），不足以承诺 {claim['amount_fen']} 分"
            )
        if claim["amount_fen"] > cap["policy_remaining_fen"]:
            return (
                f"保单 {claim['policy']} 剩余额度 {cap['policy_remaining_fen']} 分"
                f"（上限 {cap['policy_limit_fen']} 分，跨事故已占用 {cap['policy_used_fen']} 分），"
                f"不足以承诺 {claim['amount_fen']} 分"
            )
        return ""

    def approve_claim(self, claim_id, by):
        """批准后构成明确可执行承诺；占用不得超过事故可赔总额与保单剩余额度。"""
        claim = self._claim(claim_id)
        if claim["state"] != "pending":
            raise ServiceError(409, f"申请 {claim_id} 当前状态为 {claim['state']}，不能批准")
        incidents = self._claim_incidents(claim)
        if len(incidents) != 1:
            raise ServiceError(409, "清单内报告的共同事故归属不一致，需先复核调整")
        claim["incident"] = incidents.pop()
        reason = self._pending_reason(claim)
        claim["reason"] = reason
        if reason:
            raise ServiceError(409, reason)
        claim["state"] = "approved"
        claim["commitment"] = {
            "policy": claim["policy"],
            "incident": claim["incident"],
            "amount_fen": claim["amount_fen"],
            "approved_by": by,
            "note": "已批准，构成可执行承诺",
        }
        claim["history"].append(f"{by} 批准，承诺 {claim['amount_fen']} 分")
        return claim

    def reject_claim(self, claim_id, by, reason=""):
        claim = self._claim(claim_id)
        if claim["state"] != "pending":
            raise ServiceError(409, f"申请 {claim_id} 当前状态为 {claim['state']}，不能驳回")
        claim["state"] = "rejected"
        claim["reason"] = reason or "理赔经理驳回"
        claim["history"].append(f"{by} 驳回：{claim['reason']}")
        return claim

    def review_claim(self, claim_id, decision, by, amount_fen=None, incident=None):
        """复核归属调整后的承诺：确认则按新归属恢复占用，取消则释放未付部分。"""
        claim = self._claim(claim_id)
        if claim["state"] != "review":
            raise ServiceError(409, f"申请 {claim_id} 当前状态为 {claim['state']}，不在复核中")
        if decision == "cancel":
            claim["state"] = "cancelled"
            claim["commitment"] = None
            claim["history"].append(f"{by} 复核后取消承诺，未付部分释放，已付部分保留对账")
            return claim
        if decision != "confirm":
            raise ServiceError(400, "decision 须为 confirm 或 cancel")
        incidents = self._claim_incidents(claim)
        target = incident or (incidents.pop() if len(incidents) == 1 else None)
        if target is None:
            raise ServiceError(400, "清单内报告归属不一致，须显式指定复核后的共同事故")
        self._incident(target)
        paid = self._claim_paid(claim_id)
        new_amount = amount_fen if amount_fen is not None else claim["amount_fen"]
        if not isinstance(new_amount, int) or new_amount < paid:
            raise ServiceError(400, f"复核后的承诺不得低于已付款 {paid} 分")
        unpaid = new_amount - paid
        cap = self.capacity(claim["policy"], target)
        if unpaid > cap["incident_remaining_fen"] or unpaid > cap["policy_remaining_fen"]:
            claim["review_note"] = (
                f"复核未通过：事故 {target} 可赔余额 {cap['incident_remaining_fen']} 分、"
                f"保单剩余额度 {cap['policy_remaining_fen']} 分，不足以恢复未付款承诺 {unpaid} 分；"
                "不足部分列为可解释的后续处理"
            )
            claim["history"].append("复核未通过：额度不足")
            raise ServiceError(409, claim["review_note"])
        claim["incident"] = target
        claim["amount_fen"] = new_amount
        claim["state"] = "approved"
        claim["commitment"] = {
            "policy": claim["policy"],
            "incident": target,
            "amount_fen": new_amount,
            "approved_by": by,
            "note": "复核后恢复的可执行承诺",
        }
        claim["review_note"] = ""
        claim["history"].append(f"{by} 复核确认，按归属 {target} 承诺 {new_amount} 分")
        return claim

    # ---------- 付款与追偿（分别计数） ----------

    def _claim_paid(self, claim_id):
        return sum(
            t["amount_fen"]
            for t in self.state["transfers"].values()
            if t["kind"] == "repair" and t.get("claim") == claim_id and t["state"] == "settled"
        )

    def _claim_outgoing(self, claim_id):
        return sum(
            t["amount_fen"]
            for t in self.state["transfers"].values()
            if t["kind"] == "repair" and t.get("claim") == claim_id and t["state"] in {"submitted", "unknown"}
        )

    def _liability_amounts(self, liability_id):
        requested = recovered = 0
        for t in self.state["transfers"].values():
            if t["kind"] == "recovery" and t.get("liability") == liability_id:
                if t["state"] in {"submitted", "unknown", "settled"}:
                    requested += t["amount_fen"]
                if t["state"] == "settled":
                    recovered += t["amount_fen"]
        return {"requested_fen": requested, "recovered_fen": recovered}

    def _dispatch(self, transfer):
        if self.peer is None:
            transfer["state"] = "submitted"
            transfer["note"] = "未配置付款对手，仅本地登记"
            return
        result = self.peer.submit(
            transfer["ref"], transfer["kind"], transfer["party"], transfer["amount_fen"]
        )
        if result["result"] == "accepted":
            transfer["state"] = "submitted"
            transfer["note"] = "对端已受理"
        elif result["result"] == "conflict":
            raise ServiceError(409, "付款对手拒绝：相同 ref 的金额或对象不一致")
        else:
            transfer["state"] = "unknown"
            transfer["note"] = "提交后失去应答，需先查询该笔是否已支付，不能列为可重新付款"

    def _submit_transfer(self, ref, kind, party, amount_fen, link):
        existing = self.state["transfers"].get(ref)
        if existing:
            same = (
                existing["kind"] == kind
                and existing["party"] == party
                and existing["amount_fen"] == amount_fen
                and all(existing.get(key) == value for key, value in link.items())
            )
            if not same:
                raise ServiceError(409, "相同 ref 不能用于不同的金额或对象")
            if existing["state"] in {"submitted", "unknown", "settled"}:
                raise ServiceError(409, "该笔已提交且未有最终结论，失去应答应先对账，不能列为可重新付款")
            transfer = existing
        else:
            transfer = {
                "ref": ref,
                "kind": kind,
                "party": party,
                "amount_fen": amount_fen,
                **link,
                "state": "new",
                "settlement": None,
                "note": "",
            }
            self.state["transfers"][ref] = transfer
        self._dispatch(transfer)
        return transfer

    def execute_payment(self, ref, claim_id, party, amount_fen):
        """修理垫款付款：金额不得超过承诺的未付余额。"""
        claim = self._claim(claim_id)
        if claim["state"] != "approved":
            raise ServiceError(409, f"申请 {claim_id} 当前状态为 {claim['state']}，没有可执行承诺")
        if not isinstance(amount_fen, int) or amount_fen <= 0:
            raise ServiceError(400, "amount_fen 须为正整数（分）")
        link = {"claim": claim_id, "policy": claim["policy"], "incident": claim["incident"]}
        existing = self.state["transfers"].get(ref)
        if existing and existing["state"] in {"submitted", "unknown", "settled"}:
            # 已提交的 ref 先由重用规则拒绝，不进入余额判断
            return self._submit_transfer(ref, "repair", party, amount_fen, link)
        remaining = claim["amount_fen"] - self._claim_paid(claim_id) - self._claim_outgoing(claim_id)
        if amount_fen > remaining:
            raise ServiceError(400, f"承诺未付余额 {remaining} 分，不足以支付 {amount_fen} 分")
        return self._submit_transfer(ref, "repair", party, amount_fen, link)

    def execute_recovery(self, ref, liability_id, amount_fen):
        """追偿只依据双方已经认可或复核完毕的责任份额。"""
        liability = self._liability(liability_id)
        if liability["state"] not in LIABILITY_FINAL:
            raise ServiceError(409, f"份额 {liability_id} 未经双方认可或复核完毕，不能发起追偿")
        if not isinstance(amount_fen, int) or amount_fen <= 0:
            raise ServiceError(400, "amount_fen 须为正整数（分）")
        report = self._report(liability["report"])
        link = {"liability": liability_id, "report": report["report"], "incident": report["incident"]}
        existing = self.state["transfers"].get(ref)
        if existing and existing["state"] in {"submitted", "unknown", "settled"}:
            return self._submit_transfer(ref, "recovery", liability["party"], amount_fen, link)
        amounts = self._liability_amounts(liability_id)
        if amount_fen > liability["amount_fen"] - amounts["requested_fen"]:
            raise ServiceError(400, "追偿金额超出该份额尚未请求的余额")
        return self._submit_transfer(ref, "recovery", liability["party"], amount_fen, link)

    def reconcile_transfer(self, ref):
        """对账：失去应答的付款先查是否已支付，不列为可重新付款。"""
        transfer = self._transfer(ref)
        if self.peer is None:
            raise ServiceError(400, "未配置付款对手，无法对账")
        result = self.peer.inquire(ref)
        if result["result"] == "status":
            body = result["body"]
            status = body.get("status")
            if status == "pending":
                transfer["state"] = "submitted"
                transfer["note"] = "对端已受理，等待结算"
            elif status == "settled":
                transfer["state"] = "settled"
                transfer["settlement"] = body.get("settlement")
                transfer["note"] = "已结算"
            elif status == "rejected":
                transfer["state"] = "rejected"
                transfer["note"] = "对端拒绝，可换新 ref 重新提交"
        elif result["result"] == "not_found":
            if transfer["state"] in {"unknown", "submitted"}:
                transfer["state"] = "not_found"
                transfer["note"] = "对端未收到该笔，可按原内容重新提交"
        else:
            transfer["note"] = "对端仍无应答，保持原状态，稍后再查"
        return transfer

    # ---------- 查询视图 ----------

    def claim_view(self, claim_id):
        claim = self._claim(claim_id)
        paid = self._claim_paid(claim_id)
        outgoing = self._claim_outgoing(claim_id)
        view = dict(claim)
        view["paid_fen"] = paid
        view["outgoing_fen"] = outgoing
        view["remaining_commitment_fen"] = claim["amount_fen"] - paid - outgoing
        if claim["state"] == "pending":
            view["reason"] = self._pending_reason(claim)
            incidents = self._claim_incidents(claim)
            if len(incidents) == 1:
                view["capacity"] = self.capacity(claim["policy"], incidents.pop())
        return view

    def transfer_view(self, ref):
        transfer = self._transfer(ref)
        notes = {
            "submitted": "对端已受理，等待结算",
            "unknown": "提交后失去应答，需先查询该笔是否已支付，不能列为可重新付款",
            "settled": "已结算",
            "rejected": "对端拒绝，可换新 ref 重新提交",
            "not_found": "对端未收到该笔，可按原内容重新提交",
        }
        view = dict(transfer)
        view["repayable"] = transfer["state"] in {"rejected", "not_found"}
        view["note"] = transfer.get("note") or notes.get(transfer["state"], "")
        return view

    def policy_view(self, policy_id):
        policy = self._policy(policy_id)
        occ = self._occupancy()
        incidents = {
            incident
            for (pid, incident) in occ["by_pair"]
            if pid == policy_id
        } | {
            r["incident"]
            for r in self.state["reports"].values()
            if self.state["report_policy"].get(r["report"]) == policy_id
        }
        used = occ["by_policy"].get(policy_id, 0)
        return {
            **policy,
            "policy_used_fen": used,
            "policy_remaining_fen": policy["limit_fen"] - used,
            "incidents": {incident: self.capacity(policy_id, incident) for incident in sorted(incidents)},
            "followups": [item for item in self.shortfalls() if item["policy"] == policy_id],
        }

    def _check_visible(self, check, viewer):
        if viewer["role"] == "party":
            return viewer["party"] in (check["from_party"], check["to_party"])
        return True

    def _check_view(self, check):
        return {
            "check": check["check"],
            "vin": check["vin"],
            "segment": _segment(check),
            "from_party": check["from_party"],
            "to_party": check["to_party"],
            "at": check["at"],
            "area": check["area"],
            "mark": check["mark"],
            "finding": check["finding"],
            "evidence_ref": check["evidence_ref"],
            "evidence": self.state["evidence"].get(check["evidence_ref"]),
            "acks": check["acks"],
        }

    def _open_items(self, checks, reports, claims, liabilities, transfers, viewer):
        items = []
        for check in checks:
            for party in (check["from_party"], check["to_party"]):
                if viewer["role"] == "party" and party != viewer["party"]:
                    continue
                ack = check["acks"].get(party)
                if ack is None:
                    items.append({
                        "kind": "交接待确认",
                        "ref": check["check"],
                        "note": f"交接 {check['check']}（{_segment(check)}）待 {party} 确认",
                    })
                elif ack["decision"] == "object":
                    items.append({
                        "kind": "争议",
                        "ref": check["check"],
                        "note": (
                            f"交接 {check['check']} 被 {party} 异议：{ack['reason']}；"
                            "双方可查看并补交自己的区间证据"
                        ),
                    })
        for liability in liabilities:
            if liability["state"] == "proposed":
                items.append({
                    "kind": "份额待认领",
                    "ref": liability["liability"],
                    "note": (
                        f"{liability['party']} 尚未认领份额 {liability['amount_fen']} 分，"
                        "可继续保留异议"
                    ),
                })
            elif liability["state"] == "objected":
                items.append({
                    "kind": "份额异议待复核",
                    "ref": liability["liability"],
                    "note": f"{liability['party']} 保留异议：{liability['reason']}",
                })
        if viewer["role"] != "party":
            for claim in claims:
                if claim["state"] == "pending":
                    reason = self._pending_reason(claim)
                    if reason:
                        items.append({"kind": "申请待审", "ref": claim["claim"], "note": reason})
                elif claim["state"] == "review":
                    items.append({"kind": "承诺待复核", "ref": claim["claim"], "note": claim["review_note"]})
            for transfer in transfers:
                if transfer["state"] == "unknown":
                    items.append({
                        "kind": "付款待对账",
                        "ref": transfer["ref"],
                        "note": "提交后失去应答，需先查询该笔是否已支付，不能列为可重新付款",
                    })
        return items

    def _assemble_case(self, checks, reports, claims, liabilities, transfers, viewer):
        evidence = [self._check_view(c) for c in sorted(checks, key=_sort_key)]
        damages = [self.interpret_report(r["report"]) for r in reports]
        view = {
            "evidence": evidence,
            "damages": damages,
            "liabilities": [
                {**liability, **self._liability_amounts(liability["liability"])}
                for liability in liabilities
            ],
            "open_items": self._open_items(checks, reports, claims, liabilities, transfers, viewer),
        }
        if viewer["role"] != "party":
            paid = sum(self._claim_paid(c["claim"]) for c in claims)
            committed = sum(
                c["amount_fen"] - self._claim_paid(c["claim"]) for c in claims if c["state"] == "approved"
            )
            pending_recovery = sum(
                liability["amount_fen"] - self._liability_amounts(liability["liability"])["recovered_fen"]
                for liability in liabilities
                if liability["state"] in LIABILITY_FINAL
            )
            received = sum(
                self._liability_amounts(liability["liability"])["recovered_fen"] for liability in liabilities
            )
            view["claims"] = [self.claim_view(c["claim"]) for c in claims]
            view["transfers"] = [self.transfer_view(t["ref"]) for t in transfers]
            view["advances"] = {"committed_fen": committed, "paid_fen": paid}
            view["recovery"] = {"pending_fen": pending_recovery, "received_fen": received}
            view["followups"] = self._scoped_shortfalls(reports, claims)
        return view

    def _scoped_shortfalls(self, reports, claims):
        incidents = {r["incident"] for r in reports} | {c["incident"] for c in claims if c["incident"]}
        policies = {c["policy"] for c in claims}
        return [
            item
            for item in self.shortfalls()
            if item["policy"] in policies and (item["incident"] is None or item["incident"] in incidents)
        ]

    def vehicle_case(self, vin, viewer):
        """按一辆车查清证据、实际垫付、待追偿金额和未决事项。"""
        checks = [c for c in self.state["checks"].values() if c["vin"] == vin]
        visible_checks = [c for c in checks if self._check_visible(c, viewer)]
        visible_ids = {c["check"] for c in visible_checks}
        reports = []
        for report in self.state["reports"].values():
            if report["vin"] != vin:
                continue
            if viewer["role"] == "party":
                interpretation = self.interpret_report(report["report"])
                involved = {report["check"]} | {leg["check"] for leg in interpretation["candidate_legs"]}
                if not involved & visible_ids:
                    continue
            reports.append(report)
        report_ids = {r["report"] for r in reports}
        liabilities = [l for l in self.state["liabilities"].values() if l["report"] in report_ids]
        if viewer["role"] == "party":
            liabilities = [l for l in liabilities if l["party"] == viewer["party"]]
        claims = [c for c in self.state["claims"].values() if c["vin"] == vin]
        claim_ids = {c["claim"] for c in claims}
        liability_ids = {l["liability"] for l in self.state["liabilities"].values() if l["report"] in report_ids}
        transfers = [
            t
            for t in self.state["transfers"].values()
            if t.get("claim") in claim_ids or t.get("liability") in liability_ids
        ]
        case = self._assemble_case(visible_checks, reports, claims, liabilities, transfers, viewer)
        case["vin"] = vin
        return case

    def incident_case(self, incident, viewer):
        """按一次事故查清证据、实际垫付、待追偿金额和未决事项。"""
        self._incident(incident)
        reports = [r for r in self.state["reports"].values() if r["incident"] == incident]
        report_ids = {r["report"] for r in reports}
        vins = sorted({r["vin"] for r in reports})
        checks = [
            c
            for c in self.state["checks"].values()
            if c["check"] in {r["check"] for r in reports} or c["vin"] in vins
        ]
        visible_checks = [c for c in checks if self._check_visible(c, viewer)]
        if viewer["role"] == "party":
            visible_ids = {c["check"] for c in visible_checks}
            reports = [
                r
                for r in reports
                if {r["check"]}
                | {leg["check"] for leg in self.interpret_report(r["report"])["candidate_legs"]}
                & visible_ids
            ]
            report_ids = {r["report"] for r in reports}
        liabilities = [l for l in self.state["liabilities"].values() if l["report"] in report_ids]
        if viewer["role"] == "party":
            liabilities = [l for l in liabilities if l["party"] == viewer["party"]]
        claims = [
            c
            for c in self.state["claims"].values()
            if c["incident"] == incident or set(c["reports"]) & report_ids
        ]
        claim_ids = {c["claim"] for c in claims}
        liability_ids = {l["liability"] for l in self.state["liabilities"].values() if l["report"] in report_ids}
        transfers = [
            t
            for t in self.state["transfers"].values()
            if t.get("claim") in claim_ids
            or t.get("liability") in liability_ids
            or t.get("incident") == incident
        ]
        case = self._assemble_case(visible_checks, reports, claims, liabilities, transfers, viewer)
        case["incident"] = incident
        case["vins"] = vins
        return case
