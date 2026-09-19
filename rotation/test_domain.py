"""领域端到端测试：覆盖轮转履约的九类硬约束。"""

import os
import tempfile
import unittest
from datetime import date

from rotation.domain import (
    RotationService, ConflictError, NotFoundError,
    AuthorizationError, ValidationError,
)
from rotation.store import EventStore
from rotation.helpers import (
    make_service, ADMIN_A, ADMIN_B, MENTOR, STAFF, NURSE, NURSE_MENTOR,
    PRIV, PRIV_NURSE,
)


class AgreementLockingTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_agreement_snapshots_scope_site_mentor_window(self):
        view = self.service.get_agreement("R1")
        self.assertEqual(view["privileges"], [PRIV])
        self.assertEqual(view["mentor_id"], MENTOR)
        self.assertEqual(view["practice_site"], "县医院内科")
        self.assertEqual(view["windows"][0]["start"], "2026-01-01")
        self.assertEqual(view["windows"][0]["end"], "2026-03-31")
        self.assertEqual(view["learning_goals"], ["独立接诊", "教学查房"])

    def test_cannot_relock_same_roster(self):
        with self.assertRaises(ConflictError):
            self.service.lock_agreement({
                "roster_id": "R1", "agreement_no": "AGR-X",
                "staff_id": STAFF, "sending_org": "A", "receiving_org": "B",
                "practice_site": "S", "sending_admin": ADMIN_A,
                "receiving_admin": ADMIN_B, "mentor_id": MENTOR,
                "privileges": [PRIV], "start_date": "2026-04-01",
                "end_date": "2026-05-01"}, ADMIN_A)

    def test_privileges_required_before_departure(self):
        payload = {
            "roster_id": "R2", "agreement_no": "AGR-002", "staff_id": STAFF,
            "sending_org": "A", "receiving_org": "B", "practice_site": "S",
            "sending_admin": ADMIN_A, "receiving_admin": ADMIN_B,
            "mentor_id": MENTOR, "privileges": [],
            "start_date": "2026-04-01", "end_date": "2026-05-01"}
        with self.assertRaises(ValidationError):
            self.service.lock_agreement(payload, ADMIN_A)

    def test_two_hospitals_overlapping_windows_are_mutex(self):
        payload = {
            "roster_id": "R2", "agreement_no": "AGR-002", "staff_id": STAFF,
            "sending_org": "市医院", "receiving_org": "中医院",
            "practice_site": "中医院内科", "sending_admin": ADMIN_A,
            "receiving_admin": ADMIN_B, "mentor_id": MENTOR,
            "privileges": [PRIV],
            "start_date": "2026-03-01", "end_date": "2026-04-30"}
        with self.assertRaises(ConflictError) as error:
            self.service.lock_agreement(payload, ADMIN_A)
        self.assertIn("互斥", str(error.exception))

    def test_non_overlapping_second_roster_allowed(self):
        payload = {
            "roster_id": "R2", "agreement_no": "AGR-002", "staff_id": STAFF,
            "sending_org": "市医院", "receiving_org": "中医院",
            "practice_site": "中医院内科", "sending_admin": ADMIN_A,
            "receiving_admin": ADMIN_B, "mentor_id": MENTOR,
            "privileges": [PRIV],
            "start_date": "2026-04-01", "end_date": "2026-04-30"}
        self.service.lock_agreement(payload, ADMIN_A)
        self.assertEqual(self.service.get_agreement("R2")["roster_id"], "R2")

    def test_two_parties_cannot_be_same_admin(self):
        payload = {
            "roster_id": "R3", "agreement_no": "AGR-003", "staff_id": STAFF,
            "sending_org": "A", "receiving_org": "B", "practice_site": "S",
            "sending_admin": ADMIN_A, "receiving_admin": ADMIN_A,
            "mentor_id": MENTOR, "privileges": [PRIV],
            "start_date": "2026-04-01", "end_date": "2026-05-01"}
        with self.assertRaises(ValidationError):
            self.service.lock_agreement(payload, ADMIN_A)


class AmendmentTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def _propose(self, kind, changes, party, actor=None, am_id="AM-1"):
        return self.service.propose_amendment(
            "R1", kind, changes, party, actor or
            (ADMIN_A if party == "sending" else ADMIN_B), am_id)

    def test_temp_support_changes_nothing_until_both_approve(self):
        self._propose("temp-support",
                      {"window": {"start": "2026-02-10",
                                  "end": "2026-02-20",
                                  "site": "中医院急诊科"}},
                      "sending")
        view = self.service.get_agreement("R1")
        self.assertEqual(len([w for w in view["windows"]
                              if w["status"] == "locked"]), 1)
        with self.assertRaises(ConflictError):
            # 支援时段尚未经双方批准，不能按支援地点派任务
            self.service.create_assignment({
                "assignment_id": "T1", "roster_id": "R1",
                "duty": "急诊支援", "site": "中医院急诊科",
                "start": "2026-02-10", "end": "2026-02-12"}, ADMIN_B)

    def test_temp_support_takes_effect_after_dual_approval(self):
        self._propose("temp-support",
                      {"window": {"start": "2026-02-10",
                                  "end": "2026-02-20",
                                  "site": "中医院急诊科"}},
                      "sending")
        result = self.service.approve_amendment("R1", "AM-1", "receiving", ADMIN_B)
        self.assertEqual(result["status"], "applied")
        self.service.create_assignment({
            "assignment_id": "T1", "roster_id": "R1",
            "duty": "急诊支援", "site": "中医院急诊科",
            "start": "2026-02-10", "end": "2026-02-12"}, ADMIN_B)
        rows = self.service.effective_capability(STAFF, "2026-02-11")[
            "privileges"]
        support_row = next(r for r in rows
                           if r["window"]["site"] == "中医院急诊科")
        self.assertEqual(support_row["window"]["kind"], "temp-support")

    def test_extension_replaces_window_after_dual_approval(self):
        self._propose("extension",
                      {"window": {"start": "2026-04-01", "end": "2026-04-30"}},
                      "receiving", ADMIN_B)
        self.service.approve_amendment("R1", "AM-1", "sending", ADMIN_A)
        capability = self.service.effective_capability(STAFF, "2026-04-15")
        self.assertEqual(len(capability["privileges"]), 1)
        self.assertEqual(capability["privileges"][0]["window"]["kind"], "extension")

    def test_non_admin_cannot_approve_for_party(self):
        self._propose("extension",
                      {"window": {"start": "2026-04-01", "end": "2026-04-30"}},
                      "sending")
        with self.assertRaises(AuthorizationError):
            self.service.approve_amendment("R1", "AM-1", "receiving", STAFF)

    def test_double_approval_by_same_party_rejected(self):
        self._propose("extension",
                      {"window": {"start": "2026-04-01", "end": "2026-04-30"}},
                      "sending")
        with self.assertRaises(ConflictError):
            self.service.approve_amendment("R1", "AM-1", "sending", ADMIN_A)

    def test_single_approval_stays_pending(self):
        self._propose("extension",
                      {"window": {"start": "2026-04-01", "end": "2026-04-30"}},
                      "sending")
        # 只有派出方提出，接收方尚未批准：保持 pending，延长窗口不存在
        view = self.service.get_agreement("R1")
        self.assertEqual(view["amendments"][0]["status"], "pending")
        self.assertEqual(
            self.service.effective_capability(STAFF, "2026-04-15")[
                "privileges"], [])

    def test_extension_overlapping_other_hospital_rotation_rejected(self):
        self._propose("extension",
                      {"window": {"start": "2026-04-01", "end": "2026-04-30"}},
                      "sending")
        # 第二次批准之前他院落入了一段互斥轮转
        self.service.lock_agreement({
            "roster_id": "R9", "agreement_no": "AGR-009", "staff_id": STAFF,
            "sending_org": "市医院", "receiving_org": "中医院",
            "practice_site": "中医院", "sending_admin": ADMIN_A,
            "receiving_admin": ADMIN_B, "mentor_id": MENTOR,
            "privileges": [PRIV],
            "start_date": "2026-04-10", "end_date": "2026-04-20"}, ADMIN_A)
        with self.assertRaises(ConflictError):
            self.service.approve_amendment("R1", "AM-1", "receiving", ADMIN_B)
        # 生效失败后修正案仍保持 pending，窗口未被延长
        self.assertEqual(
            self.service.get_agreement("R1")["amendments"][0]["status"],
            "pending")

    def test_temp_support_overlapping_other_hospital_duty_rejected(self):
        self.service.lock_agreement({
            "roster_id": "R9", "agreement_no": "AGR-009", "staff_id": STAFF,
            "sending_org": "市医院", "receiving_org": "中医院",
            "practice_site": "中医院", "sending_admin": ADMIN_A,
            "receiving_admin": ADMIN_B, "mentor_id": MENTOR,
            "privileges": [PRIV],
            "start_date": "2026-05-10", "end_date": "2026-05-20"}, ADMIN_A)
        with self.assertRaises(ConflictError):
            self._propose("temp-support",
                          {"window": {"start": "2026-05-15",
                                      "end": "2026-05-18"}},
                          "sending")


class MutexAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_overlapping_exclusive_assignments_must_be_refused(self):
        self.service.create_assignment({
            "assignment_id": "A1", "roster_id": "R1",
            "duty": "上午门诊", "start": "2026-02-10T08:00".split("T")[0],
            "end": "2026-02-10"}, ADMIN_B)
        with self.assertRaises(ConflictError):
            self.service.create_assignment({
                "assignment_id": "A2", "roster_id": "R1",
                "duty": "同时段另一院会诊",
                "start": "2026-02-10", "end": "2026-02-11"}, ADMIN_B)

    def test_non_exclusive_kind_may_coexist(self):
        self.service.create_assignment({
            "assignment_id": "A1", "roster_id": "R1", "duty": "在线带教答疑",
            "start": "2026-02-10", "end": "2026-02-10",
            "kind": "advisory"}, ADMIN_B)
        self.service.create_assignment({
            "assignment_id": "A2", "roster_id": "R1", "duty": "门诊",
            "start": "2026-02-10", "end": "2026-02-10"}, ADMIN_B)
        self.assertEqual(
            self.service.respond_assignment("A2", True, STAFF)["status"],
            "accepted")

    def test_assignment_outside_any_window_refused(self):
        with self.assertRaises(ConflictError):
            self.service.create_assignment({
                "assignment_id": "A3", "roster_id": "R1", "duty": "过期门诊",
                "start": "2026-04-15", "end": "2026-04-16"}, ADMIN_B)

    def test_only_receiving_admin_dispatches(self):
        with self.assertRaises(AuthorizationError):
            self.service.create_assignment({
                "assignment_id": "A4", "roster_id": "R1", "duty": "门诊",
                "start": "2026-02-10", "end": "2026-02-10"}, ADMIN_A)


class CapabilityLadderTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_window_and_credential_give_supervised_only(self):
        capability = self.service.effective_capability(STAFF, "2026-02-01")
        privilege = capability["privileges"][0]
        self.assertTrue(privilege["supervised"])
        self.assertFalse(privilege["independent"])

    def test_expired_credential_removes_everything(self):
        # 护士证照 2026-02-15 到期
        self.service.lock_agreement({
            "roster_id": "RN1", "agreement_no": "AGR-RN", "staff_id": NURSE,
            "sending_org": "总院", "receiving_org": "县医院",
            "practice_site": "县医院外科", "sending_admin": ADMIN_A,
            "receiving_admin": ADMIN_B, "mentor_id": NURSE_MENTOR,
            "privileges": [PRIV_NURSE],
            "start_date": "2026-01-01", "end_date": "2026-03-31"}, ADMIN_A)
        before = self.service.effective_capability(NURSE, "2026-02-14")
        after = self.service.effective_capability(NURSE, "2026-02-16")
        self.assertTrue(before["privileges"][0]["credential_valid"])
        self.assertEqual(after["privileges"], [])

    def test_receipt_alone_is_not_independent(self):
        self.service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
        privilege = self.service.effective_capability(STAFF, "2026-02-01")[
            "privileges"][0]
        self.assertTrue(privilege["supervised"])
        self.assertFalse(privilege["independent"])

    def test_full_chain_receipt_then_assessment_enables_independent(self):
        self.service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
        self.service.award_assessment({
            "assessment_id": "AS-1", "roster_id": "R1",
            "privilege_code": PRIV, "graded_on": "2026-01-20"}, MENTOR)
        privilege = self.service.effective_capability(STAFF, "2026-02-01")[
            "privileges"][0]
        self.assertTrue(privilege["independent"])

    def test_receipt_retry_is_deduplicated(self):
        first = self.service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
        seq_after_first = self.service.store.seq
        retry = self.service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-02-25"}, ADMIN_B)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(retry["seq"], first["seq"])
        self.assertEqual(self.service.store.seq, seq_after_first)

    def test_receipt_id_reused_for_other_scope_rejected(self):
        self.service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
        with self.assertRaises(ConflictError):
            self.service.record_receipt({
                "receipt_id": "RC-1", "roster_id": "R1",
                "privilege_code": "PRIV-OTHER",
                "recorded_on": "2026-01-11"}, ADMIN_B)

    def test_assessment_retry_does_not_regrant_after_revoke(self):
        self.service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
        awarded = self.service.award_assessment({
            "assessment_id": "AS-1", "roster_id": "R1",
            "privilege_code": PRIV, "graded_on": "2026-01-20"}, MENTOR)
        self.assertTrue(awarded["granted"])
        self.service.revoke_assessment("AS-1", "考核材料不实", ADMIN_B)
        retry = self.service.award_assessment({
            "assessment_id": "AS-1", "roster_id": "R1",
            "privilege_code": PRIV, "graded_on": "2026-01-20"}, MENTOR)
        self.assertTrue(retry["deduplicated"])
        self.assertFalse(retry["granted"])
        privilege = self.service.effective_capability(STAFF, "2026-02-01")[
            "privileges"][0]
        self.assertFalse(privilege["independent"])

    def test_only_mentor_can_award(self):
        with self.assertRaises(AuthorizationError):
            self.service.award_assessment({
                "assessment_id": "AS-X", "roster_id": "R1",
                "privilege_code": PRIV, "graded_on": "2026-01-20"}, STAFF)

    def test_award_with_expired_credential_rejected(self):
        self.service.revoke_credential(STAFF, PRIV, ADMIN_A)
        with self.assertRaises(ConflictError):
            self.service.award_assessment({
                "assessment_id": "AS-X", "roster_id": "R1",
                "privilege_code": PRIV, "graded_on": "2026-01-20"}, MENTOR)


def _equip_for_handover(service):
    """造好未签病历、未完成随访、在执行任务，并具备独立能力。"""
    service.record_receipt({
        "receipt_id": "RC-1", "roster_id": "R1",
        "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
    service.award_assessment({
        "assessment_id": "AS-1", "roster_id": "R1",
        "privilege_code": PRIV, "graded_on": "2026-01-20"}, MENTOR)
    service.create_assignment({
        "assignment_id": "A1", "roster_id": "R1", "duty": "病房负责",
        "start": "2026-02-01", "end": "2026-03-20"}, ADMIN_B)
    service.respond_assignment("A1", True, STAFF)
    service.assign_case({
        "case_id": "C1", "roster_id": "R1", "patient_ref": "P-0001",
        "summary_ref": "summaries/C1"}, ADMIN_B)
    service.open_followup("C1", "2026-03-10", STAFF)


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        _equip_for_handover(self.service)

    def test_mentor_leave_triggers_itemized_checklist(self):
        result = self.service.set_mentor_leave("R1", True, ADMIN_B)
        self.assertTrue(result["mentor_on_leave"])
        detail = self.service.handover_detail("HO-R1-1")
        types = {item["type"] for item in detail["items"]}
        self.assertEqual(types,
                         {"case_sign", "followup", "assignment", "supervisor"})
        self.assertEqual(detail["status"], "open")

    def test_items_confirmed_one_by_one_with_distinct_new_owners(self):
        self.service.set_mentor_leave("R1", True, ADMIN_B)
        detail = self.service.handover_detail("HO-R1-1")
        # 未逐项确认前，交接不算完成
        trace = self.service.responsibility_trace(self.service.store.seq)
        roster_trace = next(r for r in trace["rosters"] if r["roster_id"] == "R1")
        self.assertEqual(roster_trace["handovers"][0]["status"], "open")

        by_type = {item["type"]: item["item_id"] for item in detail["items"]}
        self.service.confirm_handover_item(
            "HO-R1-1", by_type["case_sign"], "dr-backup", ADMIN_B)
        self.service.confirm_handover_item(
            "HO-R1-1", by_type["followup"], "dr-backup", ADMIN_B)
        self.service.confirm_handover_item(
            "HO-R1-1", by_type["assignment"], "dr-backup", ADMIN_B)
        # 只确认三项：整体仍未完成，监督责任未改名
        detail = self.service.handover_detail("HO-R1-1")
        self.assertEqual(detail["status"], "open")
        self.assertEqual(self.service.get_agreement("R1")["mentor_id"], MENTOR)

        with self.assertRaises(ValidationError):
            self.service.confirm_handover_item(
                "HO-R1-1", by_type["supervisor"], MENTOR, ADMIN_B)
        self.service.confirm_handover_item(
            "HO-R1-1", by_type["supervisor"], "dr-backup", ADMIN_B)
        detail = self.service.handover_detail("HO-R1-1")
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(self.service.get_agreement("R1")["mentor_id"],
                         "dr-backup")

    def test_item_cannot_be_confirmed_twice(self):
        self.service.set_mentor_leave("R1", True, ADMIN_B)
        detail = self.service.handover_detail("HO-R1-1")
        item_id = detail["items"][0]["item_id"]
        self.service.confirm_handover_item(
            "HO-R1-1", item_id, "dr-backup", ADMIN_B)
        with self.assertRaises(ConflictError):
            self.service.confirm_handover_item(
                "HO-R1-1", item_id, "dr-backup", ADMIN_B)

    def test_case_and_followup_move_to_new_owner_individually(self):
        self.service.set_mentor_leave("R1", True, ADMIN_B)
        detail = self.service.handover_detail("HO-R1-1")
        by_type = {item["type"]: item["item_id"] for item in detail["items"]}
        self.service.confirm_handover_item(
            "HO-R1-1", by_type["case_sign"], "dr-backup", ADMIN_B)
        self.service.confirm_handover_item(
            "HO-R1-1", by_type["followup"], "dr-backup", ADMIN_B)
        # 新责任人可以签署并完成随访
        self.service.sign_case("C1", "dr-backup", "2026-02-05")
        self.service.complete_followup("C1", "dr-backup")
        # 原轮转人员已看不到该病例摘要
        self.assertEqual(
            [c["case_id"] for c in self.service.my_cases(STAFF)["cases"]], [])
        self.assertEqual(
            [c["case_id"] for c in self.service.my_cases("dr-backup")["cases"]],
            ["C1"])

    def test_new_mentor_can_assess_after_handover(self):
        self.service.set_mentor_leave("R1", True, ADMIN_B)
        detail = self.service.handover_detail("HO-R1-1")
        for item in detail["items"][:-1]:
            self.service.confirm_handover_item(
                "HO-R1-1", item["item_id"], "dr-backup", ADMIN_B)
        self.service.confirm_handover_item(
            "HO-R1-1", detail["items"][-1]["item_id"], "dr-backup", ADMIN_B)
        # 休假期间考核被拒；交接完成后由新带教人登记
        self.service.award_assessment({
            "assessment_id": "AS-2", "roster_id": "R1",
            "privilege_code": PRIV, "graded_on": "2026-02-15"}, "dr-backup")

    def test_open_handover_blocks_certificate(self):
        self.service.set_mentor_leave("R1", True, ADMIN_B)
        self.service.sign_case("C1", STAFF, "2026-02-02")
        self.service.complete_followup("C1", STAFF)
        with self.assertRaises(ConflictError) as error:
            self.service.issue_certificate(
                "R1", ADMIN_B, issued_on="2026-04-01")
        self.assertIn("交接", str(error.exception))

    def test_credential_expiry_triggers_handover(self):
        service = RotationService(EventStore(), clock=lambda: "2026-02-01")
        service.register_staff(NURSE, "赵护士", "nurse", ADMIN_A)
        service.register_staff(NURSE_MENTOR, "李带教", "nurse", ADMIN_B)
        service.register_staff("nurse-backup", "备班护士", "nurse", ADMIN_B)
        service.record_credential(NURSE, PRIV_NURSE, "伤口护理",
                                  "2026-02-15", ADMIN_A)
        service.lock_agreement({
            "roster_id": "RN1", "agreement_no": "AGR-RN", "staff_id": NURSE,
            "sending_org": "总院", "receiving_org": "县医院",
            "practice_site": "县医院外科", "sending_admin": ADMIN_A,
            "receiving_admin": ADMIN_B, "mentor_id": NURSE_MENTOR,
            "privileges": [PRIV_NURSE],
            "start_date": "2026-01-01", "end_date": "2026-03-31"}, ADMIN_A)
        result = service.trigger_handover_credential_expiry(
            "RN1", ADMIN_B, trigger_date="2026-02-16")
        self.assertEqual(result["handover_id"], "HO-RN1-1")
        self.assertIn(PRIV_NURSE, result["expired_credentials"])

    def test_completed_handover_unblocks_certificate(self):
        self.service.set_mentor_leave("R1", True, ADMIN_B)
        detail = self.service.handover_detail("HO-R1-1")
        by_type = {item["type"]: item["item_id"] for item in detail["items"]}
        # 病历、随访、任务、监督全部逐项接续到新责任人
        for item_type in ("case_sign", "followup", "assignment", "supervisor"):
            self.service.confirm_handover_item(
                "HO-R1-1", by_type[item_type], "dr-backup", ADMIN_B)
        # 接续人完成签署与随访，任务已改派
        self.service.sign_case("C1", "dr-backup", "2026-02-05")
        self.service.complete_followup("C1", "dr-backup")
        result = self.service.issue_certificate(
            "R1", ADMIN_B, issued_on="2026-04-01")
        self.assertTrue(result["certificate_no"].startswith("CERT-"))

    def test_emergency_return_cuts_window_and_hands_over(self):
        # 返院前刚派发、尚未接受的任务（与已接受的 A1 不重叠）
        self.service.create_assignment({
            "assignment_id": "A2", "roster_id": "R1", "duty": "待接受会诊",
            "start": "2026-03-25", "end": "2026-03-26"}, ADMIN_B)
        result = self.service.emergency_return("R1", "2026-02-10", ADMIN_A)
        self.assertEqual(result["status"], "returned")
        self.assertEqual(
            self.service.effective_capability(STAFF, "2026-02-11")[
                "privileges"], [])
        detail = self.service.handover_detail("HO-R1-1")
        self.assertEqual(detail["reason"], "emergency_return")
        # 待接受任务也进入交接清单
        self.assertIn("A2:duty", {i["item_id"] for i in detail["items"]})
        with self.assertRaises(ConflictError):
            self.service.respond_assignment("A2", True, STAFF)
        with self.assertRaises(ConflictError):
            self.service.create_assignment({
                "assignment_id": "A9", "roster_id": "R1", "duty": "门诊",
                "start": "2026-02-11", "end": "2026-02-12"}, ADMIN_B)


class CaseVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_staff_sees_only_own_case_summaries(self):
        self.service.assign_case({
            "case_id": "C1", "roster_id": "R1", "patient_ref": "P-1",
            "summary_ref": "summaries/C1"}, ADMIN_B)
        self.assertEqual(len(self.service.my_cases(STAFF)["cases"]), 1)
        self.assertEqual(self.service.my_cases(MENTOR)["cases"], [])
        case = self.service.my_cases(STAFF)["cases"][0]
        # 只暴露摘要指引与状态，不暴露病例全文
        self.assertEqual(set(case),
                         {"case_id", "patient_ref", "summary_ref",
                          "status", "followup", "role"})
        self.assertEqual(case["role"], "owner")

    def test_sign_case_requires_owner(self):
        self.service.assign_case({
            "case_id": "C1", "roster_id": "R1", "patient_ref": "P-1",
            "summary_ref": "summaries/C1"}, ADMIN_B)
        with self.assertRaises(AuthorizationError):
            self.service.sign_case("C1", MENTOR, "2026-02-02")

    def test_signed_case_records_whether_independent(self):
        self.service.assign_case({
            "case_id": "C1", "roster_id": "R1", "patient_ref": "P-1",
            "summary_ref": "summaries/C1"}, ADMIN_B)
        supervised = self.service.sign_case("C1", STAFF, "2026-01-05")
        self.assertFalse(supervised["independent"])


class CertificateTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        _equip_for_handover(self.service)
        self.service.sign_case("C1", STAFF, "2026-03-25")
        self.service.complete_assignment("A1", STAFF)
        self.service.complete_followup("C1", STAFF)

    def test_certificate_uses_only_signed_unrevoked_assessments(self):
        result = self.service.issue_certificate(
            "R1", ADMIN_B, issued_on="2026-04-01")
        self.assertFalse(result["deduplicated"])
        self.assertTrue(result["certificate_no"].startswith("CERT-"))

    def test_certificate_retry_is_deduplicated(self):
        first = self.service.issue_certificate("R1", ADMIN_B,
                                               issued_on="2026-04-01")
        retry = self.service.issue_certificate("R1", ADMIN_B,
                                               issued_on="2026-04-02")
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(retry["certificate_no"], first["certificate_no"])

    def test_revoking_assessment_voids_certificate(self):
        self.service.issue_certificate("R1", ADMIN_B, issued_on="2026-04-01")
        self.service.revoke_assessment("AS-1", "事后发现代签", ADMIN_B)
        with self.assertRaises(ConflictError):
            # 唯一考核被撤销：已发证明作废，且不能再次签发
            self.service.issue_certificate("R1", ADMIN_B,
                                           issued_on="2026-04-05")

    def test_cannot_issue_before_window_ends(self):
        with self.assertRaises(ConflictError):
            self.service.issue_certificate("R1", ADMIN_B,
                                           issued_on="2026-03-30")

    def test_emergency_return_cannot_have_certificate(self):
        service = make_service()
        _equip_for_handover(service)
        service.emergency_return("R1", "2026-02-10", ADMIN_A)
        with self.assertRaises(ConflictError):
            service.issue_certificate("R1", ADMIN_B, issued_on="2026-04-01")

    def test_unsigned_case_blocks_certificate(self):
        service = make_service()
        service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
        service.award_assessment({
            "assessment_id": "AS-1", "roster_id": "R1",
            "privilege_code": PRIV, "graded_on": "2026-01-20"}, MENTOR)
        service.assign_case({
            "case_id": "C9", "roster_id": "R1", "patient_ref": "P-9",
            "summary_ref": "summaries/C9"}, ADMIN_B)
        with self.assertRaises(ConflictError) as error:
            service.issue_certificate("R1", ADMIN_B, issued_on="2026-04-01")
        self.assertIn("C9", str(error.exception))


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_trace_restores_who_was_independent_at_any_seq(self):
        self.service.record_receipt({
            "receipt_id": "RC-1", "roster_id": "R1",
            "privilege_code": PRIV, "recorded_on": "2026-01-10"}, ADMIN_B)
        receipt_seq = self.service.store.seq
        self.service.award_assessment({
            "assessment_id": "AS-1", "roster_id": "R1",
            "privilege_code": PRIV, "graded_on": "2026-01-20"}, MENTOR)
        assessment_seq = self.service.store.seq

        before = self.service.responsibility_trace(receipt_seq)
        roster_before = before["rosters"][0]
        self.assertEqual(roster_before["independent"], [])
        self.assertEqual(roster_before["mentor_id"], MENTOR)
        supervised = roster_before["supervised_only"][0]
        self.assertEqual(supervised["blocked_reason"], "缺少已签署考核")

        after = self.service.responsibility_trace(assessment_seq)
        self.assertEqual(after["rosters"][0]["independent"][0]["code"], PRIV)
        self.assertEqual(after["rosters"][0]["independent"][0]["assessment_id"],
                         "AS-1")

    def test_trace_shows_handover_completion(self):
        _equip_for_handover(self.service)
        self.service.set_mentor_leave("R1", True, ADMIN_B)
        mid_trace = self.service.responsibility_trace(self.service.store.seq)
        handover = mid_trace["rosters"][0]["handovers"][0]
        self.assertEqual(handover["confirmed"], 0)
        self.assertEqual(handover["total"], 4)

        detail = self.service.handover_detail("HO-R1-1")
        for item in detail["items"]:
            self.service.confirm_handover_item(
                "HO-R1-1", item["item_id"], "dr-backup", ADMIN_B)
        end_trace = self.service.responsibility_trace(self.service.store.seq)
        handover = end_trace["rosters"][0]["handovers"][0]
        self.assertEqual(handover["status"], "completed")
        self.assertEqual(handover["confirmed"], handover["total"])
        self.assertEqual(end_trace["rosters"][0]["mentor_id"], "dr-backup")


class PersistenceTest(unittest.TestCase):
    def test_jsonl_store_replays_history_with_continuous_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            service = RotationService(EventStore(path))
            service.register_staff(STAFF, "王医生", "physician", ADMIN_A)
            seq_before_close = service.store.seq
            service.store.close()

            replayed = RotationService(EventStore(path))
            self.assertEqual(replayed.store.seq, seq_before_close)
            self.assertEqual(replayed._world().staff[STAFF]["name"], "王医生")
            event = replayed.store.append("StaffRegistered", {
                "staff_id": "x", "name": "x", "role": "nurse"}, ADMIN_A)
            self.assertEqual(event["seq"], seq_before_close + 1)
            replayed.store.close()


if __name__ == "__main__":
    unittest.main()
