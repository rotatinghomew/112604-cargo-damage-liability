"""理赔协同核心规则：查勘时点解释、独立受损、额度审批、归属调整与追偿。"""

from pathlib import Path
import unittest

from claims import ClaimService, ServiceError, seed_from_samples

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class FakePeer:
    """可编排的付款对手。"""

    def __init__(self, submits=None, inquiries=None):
        self.submits = list(submits or [])
        self.inquiries = list(inquiries or [])
        self.submitted = []

    def submit(self, ref, kind, party, amount_fen):
        self.submitted.append({"ref": ref, "kind": kind, "party": party, "amount_fen": amount_fen})
        return self.submits.pop(0) if self.submits else {"result": "accepted"}

    def inquire(self, ref):
        return self.inquiries.pop(0) if self.inquiries else {"result": "not_found"}


def seeded_service(peer=None):
    return seed_from_samples(ClaimService(peer=peer), SAMPLES)


class InterpretationTest(unittest.TestCase):
    def setUp(self):
        self.service = seeded_service()

    def test_interpretation_uses_survey_time_not_upload_order(self):
        # 夜里补传的证据按实际查勘时点解释：颠倒提交顺序后结论不变
        service = ClaimService()
        for record in [
            {"check": "Q3", "vin": "V-41", "from_party": "haul", "to_party": "terminal",
             "at": "2026-11-27T02:00:00Z", "area": "right-door", "mark": "b7",
             "finding": "dent", "evidence_ref": "photo-2"},
            {"check": "Q1", "vin": "V-41", "from_party": "factory", "to_party": "yard",
             "at": "2026-11-27T00:00:00Z", "area": "right-door", "mark": "n2",
             "finding": "clear", "evidence_ref": "photo-0"},
            {"check": "Q2", "vin": "V-41", "from_party": "yard", "to_party": "haul",
             "at": "2026-11-27T01:00:00Z", "area": "right-door", "mark": "a1",
             "finding": "scratch", "evidence_ref": "photo-1"},
        ]:
            service.add_check(record)
        service.confirm_incident("J9", "survey-A")
        service.add_report({"report": "D1", "incident": "J9", "vin": "V-41", "area": "right-door",
                            "check": "Q2", "quote_fen": 80000, "issuer": "survey-A"})
        view = service.interpret_report("D1")
        self.assertEqual(view["prior_observation"]["check"], "Q1")
        self.assertIs(view["damage_present_at_prior"], False)
        self.assertEqual([leg["segment"] for leg in view["candidate_legs"]], ["yard->haul"])

    def test_scratch_and_dent_trace_to_different_legs(self):
        scratch = self.service.interpret_report("D1")
        self.assertEqual(scratch["finding"], "scratch")
        self.assertEqual([leg["segment"] for leg in scratch["candidate_legs"]], ["yard->haul"])
        dent = self.service.interpret_report("D3")
        self.assertEqual(dent["finding"], "dent")
        # 前一次交接记录的是擦痕而非凹陷：凹陷当时不存在，担责区间是后一段
        self.assertIs(dent["damage_present_at_prior"], False)
        self.assertEqual([leg["segment"] for leg in dent["candidate_legs"]], ["haul->terminal"])

    def test_first_record_has_no_prior(self):
        view = self.service.interpret_report("D2")
        self.assertIsNone(view["prior_observation"])
        self.assertIsNone(view["damage_present_at_prior"])
        self.assertEqual([leg["segment"] for leg in view["candidate_legs"]], ["haul->terminal"])

    def test_second_damage_same_area_stays_independent(self):
        reports = self.service.state["reports"]
        self.assertEqual({r["check"] for r in reports.values() if r["vin"] == "V-41"}, {"Q2", "Q3"})
        self.assertNotEqual(reports["D1"]["incident"], reports["D3"]["incident"])
        with self.assertRaises(ServiceError) as caught:
            self.service.add_report({"report": "D9", "incident": "J9", "vin": "V-41",
                                     "area": "right-door", "check": "Q2",
                                     "quote_fen": 1000, "issuer": "survey-A"})
        self.assertEqual(caught.exception.status, 409)


class EvidenceDisputeTest(unittest.TestCase):
    def setUp(self):
        self.service = seeded_service()

    def test_handover_parties_confirm_or_object(self):
        self.service.ack_check("Q2", "yard", "confirm")
        self.service.ack_check("Q2", "haul", "object", "接车时擦痕已存在，非本段造成")
        check = self.service.state["checks"]["Q2"]
        self.assertEqual(check["acks"]["yard"]["decision"], "confirm")
        self.assertEqual(check["acks"]["haul"]["decision"], "object")

    def test_non_party_cannot_ack(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.ack_check("Q2", "terminal", "confirm")
        self.assertEqual(caught.exception.status, 403)

    def test_dispute_is_recorded_without_naming_responsible_party(self):
        self.service.ack_check("Q3", "terminal", "object", "卸车前凹陷情况不明，需补证")
        case = self.service.vehicle_case("V-41", {"role": "manager", "party": ""})
        disputes = [item for item in case["open_items"] if item["kind"] == "争议"]
        self.assertEqual(len(disputes), 1)
        self.assertIn("补交", disputes[0]["note"])
        self.assertEqual(self.service.state["liabilities"], {})

    def test_report_requires_confirmed_incident(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.add_report({"report": "D8", "incident": "J99", "vin": "V-41",
                                     "area": "right-door", "check": "Q2",
                                     "quote_fen": 1000, "issuer": "survey-A"})
        self.assertEqual(caught.exception.status, 409)


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.service = seeded_service()

    def test_deductible_shared_per_incident_and_limit_across_incidents(self):
        # 同一事故两份报告共用一份免赔额：80000 + 75000 - 20000 = 135000
        self.service.submit_claim("C1", "P-7", "V-41", ["D1"], 60000, "fixer-2", "manager")
        self.service.approve_claim("C1", "manager")
        self.service.submit_claim("C2", "P-7", "V-42", ["D2"], 75000, "fixer-2", "manager")
        self.service.approve_claim("C2", "manager")
        cap = self.service.capacity("P-7", "J9")
        self.assertEqual(cap["payable_fen"], 135000)
        self.assertEqual(cap["incident_remaining_fen"], 0)
        self.assertEqual(cap["policy_remaining_fen"], 160000 - 135000)

    def test_pending_claim_shows_capacity_reason(self):
        self.service.submit_claim("C3", "P-8", "V-41", ["D3"], 68000, "fixer-1", "haul")
        # 事故 J10 可赔总额 68000 - 30000 = 38000，不足 68000
        with self.assertRaises(ServiceError) as caught:
            self.service.approve_claim("C3", "manager")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("38000", str(caught.exception))
        view = self.service.claim_view("C3")
        self.assertEqual(view["state"], "pending")
        self.assertIn("38000", view["reason"])

    def test_approval_gives_executable_commitment(self):
        self.service.submit_claim("C1", "P-7", "V-41", ["D1"], 60000, "fixer-2", "manager")
        claim = self.service.approve_claim("C1", "manager")
        self.assertEqual(claim["commitment"]["amount_fen"], 60000)
        self.assertEqual(claim["commitment"]["incident"], "J9")

    def test_policy_limit_shared_across_incidents(self):
        self.service.submit_claim("C1", "P-7", "V-41", ["D1"], 60000, "fixer-2", "manager")
        self.service.approve_claim("C1", "manager")
        self.service.submit_claim("C2", "P-7", "V-42", ["D2"], 75000, "fixer-2", "manager")
        self.service.approve_claim("C2", "manager")
        # 另一事故 J10 在 P-7 下新增报价 90000：可赔 90000 - 20000 = 70000
        self.service.add_check({"check": "Q5", "vin": "V-42", "from_party": "yard", "to_party": "haul",
                                "at": "2026-11-27T03:00:00Z", "area": "rear-door", "mark": "c1",
                                "finding": "dent", "evidence_ref": "photo-4"})
        self.service.add_report({"report": "D4", "incident": "J10", "vin": "V-42", "area": "rear-door",
                                 "check": "Q5", "quote_fen": 90000, "issuer": "survey-B"})
        # 事故可赔够，但保单上限跨事故共用：160000 - 135000 = 25000 不够 70000
        self.service.submit_claim("C4", "P-7", "V-42", ["D4"], 70000, "fixer-2", "manager")
        with self.assertRaises(ServiceError) as caught:
            self.service.approve_claim("C4", "manager")
        self.assertIn("25000", str(caught.exception))
        view = self.service.policy_view("P-7")
        self.assertEqual(view["policy_remaining_fen"], 25000)


class ReattributionTest(unittest.TestCase):
    def setUp(self):
        self.peer = FakePeer(inquiries=[
            {"result": "status", "body": {"status": "settled", "settlement": "Z-1"}},
        ])
        self.service = seeded_service(peer=self.peer)
        self.service.submit_claim("C1", "P-7", "V-41", ["D1"], 60000, "fixer-2", "manager")
        self.service.approve_claim("C1", "manager")
        self.service.submit_claim("C2", "P-7", "V-42", ["D2"], 75000, "fixer-2", "manager")
        self.service.approve_claim("C2", "manager")

    def test_unpaid_commitment_released_on_reattribution(self):
        self.service.reattribute_report("D1", "J10", by="survey")
        claim = self.service.state["claims"]["C1"]
        self.assertEqual(claim["state"], "review")
        self.assertIsNone(claim["commitment"])
        # 未付款承诺停止占用原认定：J9 只剩 C2 的 75000
        cap = self.service.capacity("P-7", "J9")
        self.assertEqual(cap["incident_used_fen"], 75000)
        # 复核确认后按新归属 J10 恢复占用
        self.service.review_claim("C1", "confirm", by="manager")
        self.assertEqual(self.service.capacity("P-7", "J10")["incident_used_fen"], 60000)

    def test_paid_advance_kept_for_reconciliation_and_shortfall_listed(self):
        self.service.execute_payment("PAY-1", "C1", "fixer-2", 60000)
        self.service.reconcile_transfer("PAY-1")
        self.assertEqual(self.service._claim_paid("C1"), 60000)
        # 归属调整：D1 离开 J9，J9 可赔总额收紧为 75000 - 20000 = 55000
        self.service.reattribute_report("D1", "J10", by="survey")
        shortfalls = self.service.shortfalls()
        j9 = [item for item in shortfalls if item["incident"] == "J9"]
        self.assertEqual(len(j9), 1)
        # C1 已付 60000 + C2 承诺 75000 = 135000，超出 55000 的部分列为后续处理
        self.assertEqual(j9[0]["shortfall_fen"], 135000 - 55000)
        # 已付款的垫款仍在原归属下对账，不被删除
        transfer = self.service.transfer_view("PAY-1")
        self.assertEqual(transfer["state"], "settled")
        self.assertEqual(transfer["incident"], "J9")
        # 复核确认 C1 按 J10：未付余额为 0，不重复支付
        self.service.review_claim("C1", "confirm", by="manager")
        claim = self.service.claim_view("C1")
        self.assertEqual(claim["remaining_commitment_fen"], 0)
        self.assertEqual(len(self.service.state["transfers"]), 1)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.peer = FakePeer(inquiries=[
            {"result": "status", "body": {"status": "settled", "settlement": "Z-2"}},
        ])
        self.service = seeded_service(peer=self.peer)
        self.service.propose_liability("L1", "D1", "haul", 40000, by="manager")

    def test_recovery_requires_accepted_or_reviewed_share(self):
        with self.assertRaises(ServiceError) as caught:
            self.service.execute_recovery("R1", "L1", 40000)
        self.assertEqual(caught.exception.status, 409)
        self.service.ack_liability("L1", "haul", "accept")
        self.service.execute_recovery("R1", "L1", 40000)
        # 只有实际收到的钱才计入回款
        case = self.service.vehicle_case("V-41", {"role": "manager", "party": ""})
        self.assertEqual(case["recovery"]["received_fen"], 0)
        self.assertEqual(case["recovery"]["pending_fen"], 40000)
        self.service.reconcile_transfer("R1")
        case = self.service.vehicle_case("V-41", {"role": "manager", "party": ""})
        self.assertEqual(case["recovery"]["received_fen"], 40000)
        self.assertEqual(case["recovery"]["pending_fen"], 0)

    def test_objected_share_can_be_reviewed(self):
        self.service.ack_liability("L1", "haul", "object", "擦痕在接车前已存在")
        with self.assertRaises(ServiceError):
            self.service.execute_recovery("R1", "L1", 40000)
        self.service.review_liability("L1", by="manager")
        self.service.execute_recovery("R1", "L1", 40000)
        self.assertEqual(self.service.transfer_view("R1")["state"], "submitted")

    def test_repair_and_recovery_counted_separately(self):
        self.service.submit_claim("C1", "P-7", "V-41", ["D1"], 60000, "fixer-2", "manager")
        self.service.approve_claim("C1", "manager")
        self.service.execute_payment("PAY-1", "C1", "fixer-2", 60000)
        self.service.ack_liability("L1", "haul", "accept")
        self.service.execute_recovery("R1", "L1", 40000)
        kinds = {t["ref"]: t["kind"] for t in self.service.state["transfers"].values()}
        self.assertEqual(kinds, {"PAY-1": "repair", "R1": "recovery"})


class PaymentReconciliationTest(unittest.TestCase):
    def test_lost_response_is_not_repayable_until_reconciled(self):
        peer = FakePeer(
            submits=[{"result": "unknown"}],
            inquiries=[
                {"result": "status", "body": {"status": "pending"}},
                {"result": "status", "body": {"status": "settled", "settlement": "Z-9"}},
            ],
        )
        service = seeded_service(peer=peer)
        service.submit_claim("C1", "P-7", "V-41", ["D1"], 60000, "fixer-2", "manager")
        service.approve_claim("C1", "manager")
        service.execute_payment("PAY-1", "C1", "fixer-2", 60000)
        view = service.transfer_view("PAY-1")
        self.assertEqual(view["state"], "unknown")
        self.assertIs(view["repayable"], False)
        # 失去应答后先查该笔是否已支付
        self.assertEqual(service.reconcile_transfer("PAY-1")["state"], "submitted")
        self.assertEqual(service.reconcile_transfer("PAY-1")["state"], "settled")
        self.assertEqual(service._claim_paid("C1"), 60000)
        # 已有最终结论的 ref 不能重复提交
        with self.assertRaises(ServiceError) as caught:
            service.execute_payment("PAY-1", "C1", "fixer-2", 60000)
        self.assertEqual(caught.exception.status, 409)

    def test_not_found_may_be_resubmitted_with_same_content(self):
        peer = FakePeer(
            submits=[{"result": "unknown"}, {"result": "accepted"}],
            inquiries=[{"result": "not_found"}],
        )
        service = seeded_service(peer=peer)
        service.submit_claim("C1", "P-7", "V-41", ["D1"], 60000, "fixer-2", "manager")
        service.approve_claim("C1", "manager")
        service.execute_payment("PAY-1", "C1", "fixer-2", 60000)
        service.reconcile_transfer("PAY-1")
        self.assertEqual(service.transfer_view("PAY-1")["state"], "not_found")
        service.execute_payment("PAY-1", "C1", "fixer-2", 60000)
        self.assertEqual(service.transfer_view("PAY-1")["state"], "submitted")
        # 相同 ref 不同金额被拒绝
        with self.assertRaises(ServiceError):
            service.execute_payment("PAY-1", "C1", "fixer-2", 50000)


class TenancyTest(unittest.TestCase):
    def setUp(self):
        self.service = seeded_service()
        self.service.propose_liability("L1", "D1", "haul", 40000, by="manager")
        self.service.propose_liability("L2", "D3", "terminal", 30000, by="manager")

    def test_party_sees_only_own_segments(self):
        case = self.service.vehicle_case("V-41", {"role": "party", "party": "yard"})
        segments = {item["segment"] for item in case["evidence"]}
        self.assertEqual(segments, {"factory->yard", "yard->haul"})
        self.assertNotIn("claims", case)
        self.assertEqual([l["party"] for l in case["liabilities"]], [])
        haul_case = self.service.vehicle_case("V-41", {"role": "party", "party": "haul"})
        self.assertEqual([l["liability"] for l in haul_case["liabilities"]], ["L1"])

    def test_manager_sees_whole_case(self):
        case = self.service.vehicle_case("V-41", {"role": "manager", "party": ""})
        self.assertEqual(len(case["evidence"]), 3)
        self.assertEqual(len(case["liabilities"]), 2)
        self.assertIn("advances", case)


if __name__ == "__main__":
    unittest.main()
