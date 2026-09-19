"""轮转履约后端的领域契约测试（经真实 HTTP 端口打全链路）。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rotation_api import build_handler
from rotation_service import RotationService

H_A = "H_A"          # 派出医院
H_B = "H_B"          # 县级接收机构
MGR_A = "mgr_a"      # 派出方护理部/医务处
MGR_B = "mgr_b"      # 接收方管理者
AUTH = "auth_1"      # 监管机构
WANG = "dr_wang"     # 轮转骨干医师
LI = "dr_li"         # 原带教人
ZHAO = "dr_zhao"     # 交接后的新责任人
CHEN = "nurse_chen"  # 证照将到期的注册护士

PRIV_INDEP = "PRIV_THORACENTESIS"
PRIV_SUPERVISED = "PRIV_ENDOSCOPY_ASSIST"


class RotationContractTest(unittest.TestCase):
    def setUp(self):
        self.service = RotationService()
        handler = build_handler(self.service)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self._seed()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    # ---- 工具 -------------------------------------------------------------

    def call(self, method, path, body=None, actor=None, headers=None,
             raw=None):
        data = None
        hdrs = {"Content-Type": "application/json; charset=utf-8"}
        if actor:
            hdrs["X-Actor"] = actor
        if headers:
            hdrs.update(headers)
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(f"{self.base}{path}", data=data, headers=hdrs,
                          method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            return exc.code, payload

    def get(self, path, actor=None, headers=None):
        return self.call("GET", path, actor=actor, headers=headers)

    def post(self, path, body=None, actor=None, headers=None):
        return self.call("POST", path, body=body or {}, actor=actor,
                         headers=headers)

    def _seed(self):
        self.post("/orgs", {"id": H_A, "name": "市中心医院"}, "system")
        self.post("/orgs", {"id": H_B, "name": "县人民医院"}, "system")
        self.post("/actors", {"id": MGR_A, "name": "市护理部",
                              "role": "manager", "org_id": H_A}, "system")
        self.post("/actors", {"id": MGR_B, "name": "县医务处",
                              "role": "manager", "org_id": H_B}, "system")
        self.post("/actors", {"id": AUTH, "name": "监管平台",
                              "role": "authority", "org_id": H_A}, "system")
        self.post("/staff", {
            "id": WANG, "name": "王医生", "profession": "physician",
            "org_id": H_A,
            "licenses": [{
                "number": "LIC-W", "scopes": ["internal"],
                "registered_sites": [H_A, H_B], "expires_on": "2027-12-31",
            }],
        }, "system")
        self.post("/staff", {
            "id": LI, "name": "李带教", "profession": "physician",
            "org_id": H_B,
            "licenses": [{
                "number": "LIC-L", "scopes": ["internal"],
                "registered_sites": [H_B], "expires_on": "2027-12-31",
            }],
        }, "system")
        self.post("/staff", {
            "id": ZHAO, "name": "赵接任", "profession": "physician",
            "org_id": H_B,
            "licenses": [{
                "number": "LIC-Z", "scopes": ["internal"],
                "registered_sites": [H_B], "expires_on": "2027-12-31",
            }],
        }, "system")
        # 陈护士证照在轮转窗口结束前到期。
        self.post("/staff", {
            "id": CHEN, "name": "陈护士", "profession": "nurse",
            "org_id": H_A,
            "licenses": [{
                "number": "LIC-C", "scopes": ["nursing_general"],
                "registered_sites": [H_B], "expires_on": "2026-03-01",
            }],
        }, "system")

    def _plan_payload(self, **overrides):
        payload = {
            "plan_id": "plan-1",
            "staff_id": WANG,
            "sending_org": H_A,
            "receiving_org": H_B,
            "valid_from": "2026-02-01T08:00:00",
            "valid_to": "2026-05-31T18:00:00",
            "preceptor_id": LI,
            "learning_objectives": ["掌握县级常见内科处置"],
            "privileges": [
                {"code": PRIV_INDEP, "name": "胸腔穿刺",
                 "required_scope": "internal", "independent": True,
                 "requires": ["receipt"]},
                {"code": PRIV_SUPERVISED, "name": "内镜辅助",
                 "required_scope": "internal", "independent": False,
                 "requires": ["assessment"]},
            ],
        }
        payload.update(overrides)
        return payload

    def _lock_plan(self, **overrides):
        return self.post("/plans", self._plan_payload(**overrides), MGR_A)

    # ---- 1. 出发前锁定 -----------------------------------------------------

    def test_01_plan_locks_full_agreement_before_departure(self):
        status, body = self._lock_plan(at="2026-01-20T09:00:00")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["version"]["version"], 1)
        self.assertEqual(body["version"]["status"], "approved")
        self.assertEqual(body["version"]["preceptor_id"], LI)
        self.assertEqual(body["version"]["practice_site"], H_B)
        self.assertEqual(
            {p["code"] for p in body["version"]["privileges"]},
            {PRIV_INDEP, PRIV_SUPERVISED},
        )
        # 双方批准随锁定一并固定。
        sides = {a["side"] for a in body["version"]["approvals"]}
        self.assertEqual(sides, {"sending", "receiving"})

    def test_02_plan_rejected_when_license_expires_before_window_end(self):
        status, body = self._lock_plan(
            staff_id=CHEN,
            privileges=[{"code": "PRIV_NURSING", "name": "护理操作",
                         "required_scope": "nursing_general",
                         "independent": True}],
            at="2026-01-20T09:00:00",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "credential_mismatch")
        reasons = {m["reason"]: m["privilege"]
                   for m in body["details"]["mismatches"]}
        self.assertEqual(
            reasons["license_expires_before_window_end"], "PRIV_NURSING"
        )

    def test_03_plan_rejected_for_unregistered_site_or_scope(self):
        # 备案地点不含县医院。
        status, body = self._lock_plan(at="2026-01-20T09:00:00")
        self.assertEqual(status, 200)
        status, body = self.post("/plans", self._plan_payload(
            plan_id="plan-bad-site",
            privileges=[{"code": "X", "required_scope": "surgery",
                         "independent": True}],
        ), MGR_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "credential_mismatch")

    def test_04_staff_cannot_lock_plan(self):
        status, _ = self.post("/plans", self._plan_payload(), WANG)
        self.assertEqual(status, 403)

    # ---- 2. 变更须双方批准 -------------------------------------------------

    def test_05_support_amendment_changes_duties_only_after_dual_approval(self):
        self._lock_plan()
        # 单方提出，职责不变。
        status, amend = self.post("/amendments", {
            "plan_id": "plan-1", "change_type": "support",
            "valid_from": "2026-03-10T08:00:00",
            "valid_to": "2026-03-20T18:00:00",
            "practice_site": H_A,
        }, MGR_A, )
        self.assertEqual(status, 200, amend)
        self.assertEqual(amend["status"], "proposed")
        aid = amend["id"]

        # 机构与批准方必须匹配。
        status, body = self.post(f"/amendments/{aid}/approvals",
                                 {"side": "receiving"}, MGR_A)
        self.assertEqual(status, 403)
        status, body = self.post(f"/amendments/{aid}/approvals",
                                 {"side": "sending"}, MGR_B)
        self.assertEqual(status, 403)

        # 派出方批准后，支援窗口内仍按原版本执行（执业地点仍为县医院）。
        status, _ = self.post(f"/amendments/{aid}/approvals",
                              {"side": "sending"}, MGR_A)
        self.assertEqual(status, 200)
        _, view = self.get("/plans/plan-1/effective?at=2026-03-15T09:00:00",
                           MGR_A)
        self.assertEqual(view["version"], 1)
        self.assertEqual(view["practice_site"], H_B)

        # 接收方批准后新版本才生效。
        status, body = self.post(f"/amendments/{aid}/approvals",
                                 {"side": "receiving"}, MGR_B)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "approved")
        _, view = self.get("/plans/plan-1/effective?at=2026-03-15T09:00:00",
                           MGR_A)
        self.assertEqual(view["version"], 2)
        self.assertEqual(view["change_type"], "support")
        self.assertEqual(view["practice_site"], H_A)
        # 支援窗口外仍是原版本。
        _, view = self.get("/plans/plan-1/effective?at=2026-04-01T09:00:00",
                           MGR_A)
        self.assertEqual(view["version"], 1)

        # 重复批准被拒绝。
        status, body = self.post(f"/amendments/{aid}/approvals",
                                 {"side": "sending"}, MGR_A)
        self.assertEqual(status, 409)

    def test_06_extension_must_extend_end_and_get_dual_approval(self):
        self._lock_plan()
        status, body = self.post("/amendments", {
            "plan_id": "plan-1", "change_type": "extension",
            "valid_from": "2026-02-01T08:00:00",
            "valid_to": "2026-05-01T18:00:00",
        }, MGR_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_extension")

        status, amend = self.post("/amendments", {
            "plan_id": "plan-1", "change_type": "extension",
            "valid_from": "2026-06-01T08:00:00",
            "valid_to": "2026-07-31T18:00:00",
        }, MGR_A)
        self.assertEqual(status, 200)
        aid = amend["id"]
        # 只有一方批准时，6 月不在轮转窗口内。
        self.post(f"/amendments/{aid}/approvals",
                  {"side": "sending"}, MGR_A)
        _, view = self.get("/plans/plan-1/effective?at=2026-06-15T09:00:00",
                           MGR_A)
        self.assertFalse(view["within_rotation"])
        self.post(f"/amendments/{aid}/approvals",
                  {"side": "receiving"}, MGR_B)
        _, view = self.get("/plans/plan-1/effective?at=2026-06-15T09:00:00",
                           MGR_A)
        self.assertTrue(view["within_rotation"])
        self.assertEqual(view["change_type"], "extension")

    # ---- 3. 两院互斥 -------------------------------------------------------

    def test_07_overlapping_exclusive_tasks_across_sites_rejected(self):
        self._lock_plan()
        status, _ = self.post("/tasks", {
            "task_id": "t1", "staff_id": WANG, "site_id": H_B,
            "start": "2026-03-01T08:00:00", "end": "2026-03-05T18:00:00",
            "title": "县医院值班",
        }, MGR_B)
        self.assertEqual(status, 200)

        # 重叠时段在另一医院承担互斥任务：拒绝并指出冲突。
        status, body = self.post("/tasks", {
            "task_id": "t2", "staff_id": WANG, "site_id": H_A,
            "start": "2026-03-03T08:00:00", "end": "2026-03-06T18:00:00",
        }, MGR_A)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "mutual_exclusion")
        self.assertEqual(body["details"]["conflicting_task_id"], "t1")

        # 同院并行允许。
        status, _ = self.post("/tasks", {
            "task_id": "t3", "staff_id": WANG, "site_id": H_B,
            "start": "2026-03-02T08:00:00", "end": "2026-03-02T12:00:00",
            "exclusive": False,
        }, MGR_B)
        self.assertEqual(status, 200)

        # 不重叠的跨院任务允许。
        status, _ = self.post("/tasks", {
            "task_id": "t4", "staff_id": WANG, "site_id": H_A,
            "start": "2026-03-06T08:00:00", "end": "2026-03-07T18:00:00",
        }, MGR_A)
        self.assertEqual(status, 200)

        # 原任务取消后，重叠分派放行。
        self.post("/tasks/t1/cancel", {}, MGR_B)
        status, _ = self.post("/tasks", {
            "task_id": "t5", "staff_id": WANG, "site_id": H_A,
            "start": "2026-03-01T08:00:00", "end": "2026-03-05T18:00:00",
        }, MGR_A)
        self.assertEqual(status, 200)

    # ---- 4. 回执/考核重试不重复授予 ----------------------------------------

    def test_08_receipt_retry_grants_only_once(self):
        self._lock_plan()
        headers = {"Idempotency-Key": "rcpt-key-1"}
        payload = {
            "staff_id": WANG, "receipt_code": "RC-1",
            "privilege_codes": [PRIV_INDEP],
        }
        status, body = self.post("/receipts", payload, AUTH,
                                 headers=headers)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["duplicate"])
        self.assertTrue(body["grants"][0]["changed"])

        # 幂等键重试：返回首结果，不再授予。
        status, retry = self.post("/receipts", payload, AUTH,
                                  headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(retry["grants"], body["grants"])

        # 自然键重试（换调用、不带幂等键）同样识别为重复。
        status, again = self.post("/receipts", payload, AUTH)
        self.assertEqual(status, 200)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["grants"], [])

        # 同幂等键携带不同内容：拒绝。
        status, body = self.post("/receipts",
                                 {**payload, "receipt_code": "RC-OTHER"},
                                 AUTH, headers=headers)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "idempotency_key_reuse")

        # 审计中该能力只被授予一次。
        _, audit = self.get(
            "/audit?entity_type=grant&entity_id="
            f"{WANG}:{PRIV_INDEP}", MGR_A)
        grants = [e for e in audit["events"]
                  if e["action"] == "privilege_granted"]
        self.assertEqual(len(grants), 1)

    def test_09_assessment_retry_grants_once_and_signing_unblocks(self):
        self._lock_plan()
        # 考核通过上传前：监督项目不可独立执行。
        _, view = self.get("/plans/plan-1/effective?at=2026-02-10T09:00:00",
                           MGR_A)
        supervised = {p["code"]: p for p in view["supervised_privileges"]}
        self.assertIn("assessment_not_signed_or_revoked",
                      supervised[PRIV_SUPERVISED]["blockers"])

        headers = {"Idempotency-Key": "assess-key-1"}
        payload = {
            "assessment_id": "asmt-1", "plan_id": "plan-1",
            "privilege_code": PRIV_SUPERVISED, "result": "pass",
            "at": "2026-02-10T10:00:00",
        }
        status, body = self.post("/assessments", payload, MGR_B,
                                 headers=headers)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["grants"][0]["changed"])
        # 幂等重试：首次结果原样回放，写操作不重复执行。
        status, retry = self.post("/assessments", payload, MGR_B,
                                  headers=headers)
        self.assertEqual(retry, body)
        # 自然键重试同样识别为重复。
        status, again = self.post("/assessments", payload, MGR_B)
        self.assertTrue(again["duplicate"])
        # 审计中该能力只被授予一次。
        _, audit = self.get(
            "/audit?entity_type=grant&entity_id="
            f"{WANG}:{PRIV_SUPERVISED}", MGR_A)
        grants = [e for e in audit["events"]
                  if e["action"] == "privilege_granted"]
        self.assertEqual(len(grants), 1)

        # 已上传未签署：仍然阻断。
        _, view = self.get("/plans/plan-1/effective?at=2026-02-11T09:00:00",
                           MGR_A)
        supervised = {p["code"]: p for p in view["supervised_privileges"]}
        self.assertIn("assessment_not_signed_or_revoked",
                      supervised[PRIV_SUPERVISED]["blockers"])

        # 签署后阻断解除。
        status, signed = self.post("/assessments/asmt-1/sign",
                                   {"at": "2026-02-11T10:00:00"}, MGR_B)
        self.assertEqual(status, 200)
        self.assertTrue(signed["signed_at"])
        _, view = self.get("/plans/plan-1/effective?at=2026-02-12T09:00:00",
                           MGR_A)
        supervised = {p["code"]: p for p in view["supervised_privileges"]}
        self.assertEqual(supervised[PRIV_SUPERVISED]["blockers"], [])
        self.assertTrue(supervised[PRIV_SUPERVISED]["authorized"])

        # 监管回执到位后独立项目解除阻断。
        self.post("/receipts", {
            "staff_id": WANG, "receipt_code": "RC-X",
            "privilege_codes": [PRIV_INDEP],
            "at": "2026-02-12T10:00:00",
        }, AUTH)
        _, view = self.get("/plans/plan-1/effective?at=2026-02-13T09:00:00",
                           MGR_A)
        independent = {p["code"]: p for p in view["independent_privileges"]}
        self.assertEqual(independent[PRIV_INDEP]["blockers"], [])

    # ---- 5. 交接清单逐项确认 -----------------------------------------------

    def _prepare_open_items(self):
        self._lock_plan()
        self.post("/plans/plan-1/records",
                  {"record_id": "rec-1", "title": "未签署病历"}, MGR_B)
        self.post("/plans/plan-1/followups",
                  {"followup_id": "fu-1", "title": "术后随访",
                   "due_at": "2026-06-10T18:00:00"}, MGR_B)

    def test_10_handover_is_itemized_not_bulk_renamed(self):
        self._prepare_open_items()
        status, checklist = self.post("/plans/plan-1/handovers", {
            "reason": "preceptor_leave", "to_staff_id": ZHAO,
        }, MGR_B, )
        self.assertEqual(status, 200, checklist)
        self.assertEqual(checklist["total"], 3)  # 病历 + 随访 + 监督关系
        self.assertEqual(checklist["confirmed"], 0)
        self.assertFalse(checklist["completed"])
        cid = checklist["id"]
        categories = {i["category"] for i in checklist["items"]}
        self.assertEqual(
            categories, {"record", "followup", "supervision"}
        )

        # 非新责任人不能代为确认。
        status, body = self.post(
            f"/handovers/{cid}/items/item-1/confirm", {}, WANG)
        self.assertEqual(status, 403)

        # 逐项确认：每一件由新责任人独立确认。
        status, view = self.post(
            f"/handovers/{cid}/items/item-1/confirm",
            {"note": "病历已接手"}, ZHAO)
        self.assertEqual(status, 200)
        self.assertEqual(view["confirmed"], 1)
        self.assertFalse(view["completed"])

        # 重复确认同一项被拒绝；系统没有整体改名入口。
        status, body = self.post(
            f"/handovers/{cid}/items/item-1/confirm", {}, ZHAO)
        self.assertEqual(status, 409)

        # 管理者代签接续也算逐项留痕（不是整体改名）。
        self.post(f"/handovers/{cid}/items/item-2/confirm", {}, MGR_B)
        status, view = self.post(
            f"/handovers/{cid}/items/item-3/confirm", {}, ZHAO)
        self.assertTrue(view["completed"])
        self.assertIsNotNone(view["completed_at"])

        # 每件事项保留各自的确认人与时间。
        confirmers = {i["id"]: i["confirmed_by"] for i in view["items"]}
        self.assertEqual(confirmers["item-1"], ZHAO)
        self.assertEqual(confirmers["item-2"], MGR_B)
        self.assertEqual(confirmers["item-3"], ZHAO)

    def test_11_supervision_switches_only_when_supervision_item_confirmed(self):
        self._prepare_open_items()
        _, checklist = self.post("/plans/plan-1/handovers", {
            "reason": "emergency_recall", "to_staff_id": ZHAO,
            "at": "2026-04-01T09:00:00",
        }, MGR_B)
        cid = checklist["id"]
        # 仅确认病历与随访，监督关系尚未交接：监督人仍是李带教。
        self.post(f"/handovers/{cid}/items/item-1/confirm",
                  {"at": "2026-04-02T09:00:00"}, ZHAO)
        self.post(f"/handovers/{cid}/items/item-2/confirm",
                  {"at": "2026-04-03T09:00:00"}, ZHAO)
        _, view = self.get("/plans/plan-1/effective?at=2026-04-10T09:00:00",
                           MGR_A)
        self.assertEqual(view["supervisor"]["staff_id"], LI)
        self.assertFalse(view["handover"]["completed"])

        self.post(f"/handovers/{cid}/items/item-3/confirm",
                  {"at": "2026-04-05T09:00:00"}, ZHAO)
        _, view = self.get("/plans/plan-1/effective?at=2026-04-10T09:00:00",
                           MGR_A)
        self.assertEqual(view["supervisor"]["staff_id"], ZHAO)
        self.assertEqual(view["supervisor"]["source"], "handover")
        self.assertTrue(view["handover"]["completed"])

    def test_12_handover_without_open_items_rejected(self):
        self._lock_plan()
        status, body = self.post("/plans/plan-1/handovers", {
            "reason": "license_expiry", "to_staff_id": ZHAO,
        }, MGR_B)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "nothing_to_handover")

    # ---- 6. 病例摘要可见性 -------------------------------------------------

    def test_13_staff_sees_only_own_case_summaries(self):
        self._lock_plan()
        status, _ = self.post("/cases", {
            "case_id": "case-1", "plan_id": "plan-1",
            "patient_ref": "P-0001",
            "abstract": "胸痛待查，摘要...",
            "responsible_staff": [WANG],
        }, MGR_B)
        self.assertEqual(status, 200)

        status, case = self.get("/cases/case-1", WANG)
        self.assertEqual(status, 200)
        self.assertEqual(case["patient_ref"], "P-0001")

        # 不负责该病例的轮转人员被拒绝。
        status, body = self.get("/cases/case-1", ZHAO)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "case_access_denied")

        # 管理者可查看全部。
        status, _ = self.get("/cases/case-1", MGR_A)
        self.assertEqual(status, 200)

        # 列表同样按责任人过滤。
        _, listing = self.get("/cases", ZHAO)
        self.assertEqual(listing["cases"], [])
        _, listing = self.get("/cases", WANG)
        self.assertEqual([c["id"] for c in listing["cases"]], ["case-1"])
        _, listing = self.get("/cases", MGR_B)
        self.assertEqual(len(listing["cases"]), 1)

    # ---- 7. 时点还原 -------------------------------------------------------

    def test_14_reconstruct_authority_at_any_point_in_time(self):
        self._lock_plan()
        # 窗口前。
        _, view = self.get("/plans/plan-1/effective?at=2026-01-15T09:00:00",
                           MGR_A)
        self.assertFalse(view["within_rotation"])

        # 窗口初期：v1、李带教监督、无交接。
        _, view = self.get("/plans/plan-1/effective?at=2026-02-05T09:00:00",
                           MGR_A)
        self.assertEqual(view["version"], 1)
        self.assertEqual(view["supervisor"]["staff_id"], LI)
        self.assertIsNone(view["handover"])
        self.assertEqual(view["window"][0], "2026-02-01T08:00:00")

        # 审计可按实体还原全部操作链。
        status, audit = self.get("/audit?entity_type=plan&entity_id=plan-1",
                                 MGR_B)
        self.assertEqual(status, 200)
        actions = [e["action"] for e in audit["events"]]
        self.assertIn("lock_plan", actions)

        # 非管理者不能拉审计。
        status, _ = self.get("/audit?entity_type=plan", WANG)
        self.assertEqual(status, 403)

    # ---- 8. 结业证明 -------------------------------------------------------

    def test_15_certificate_uses_only_signed_unrevoked_facts(self):
        self._lock_plan()
        # 无事实：不能生成。
        status, body = self.post("/plans/plan-1/certificates", {}, MGR_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "no_certificate_basis")

        # 考核未通过：不算依据。
        self.post("/assessments", {
            "assessment_id": "asmt-fail", "plan_id": "plan-1",
            "privilege_code": PRIV_INDEP, "result": "fail",
        }, MGR_B)
        status, body = self.post("/plans/plan-1/certificates", {}, MGR_A)
        self.assertEqual(status, 400)

        # 通过但未签署：不算依据。
        self.post("/assessments", {
            "assessment_id": "asmt-1", "plan_id": "plan-1",
            "privilege_code": PRIV_INDEP, "result": "pass",
        }, MGR_B)
        status, body = self.post("/plans/plan-1/certificates", {}, MGR_A)
        self.assertEqual(status, 400)

        # 签署后可签发。
        self.post("/assessments/asmt-1/sign", {}, MGR_B)
        status, cert = self.post("/plans/plan-1/certificates", {}, MGR_A)
        self.assertEqual(status, 200, cert)
        self.assertEqual(len(cert["facts"]), 1)
        self.assertEqual(cert["facts"][0]["assessment_id"], "asmt-1")
        cert_id = cert["id"]

        # 重复签发需显式允许。
        status, body = self.post("/plans/plan-1/certificates", {}, MGR_A)
        self.assertEqual(status, 409)

        # 撤销前核验有效。
        status, verification = self.get(
            f"/certificates/{cert_id}/verify", WANG)
        self.assertEqual(status, 200)
        self.assertEqual(verification["status"], "valid")

        # 撤销考核事实：已发证明立即失效。
        status, revoked = self.post("/assessments/asmt-1/revoke",
                                    {"reason": "复核发现代签"}, MGR_A)
        self.assertEqual(status, 200)
        self.assertEqual(
            revoked["suspended_grants"],
            [{"staff_id": WANG, "code": PRIV_INDEP}],
        )
        status, verification = self.get(
            f"/certificates/{cert_id}/verify", WANG)
        self.assertEqual(status, 200)
        self.assertEqual(verification["status"], "invalidated")
        self.assertEqual(verification["invalidated_basis"], ["asmt-1"])

        # 撤销后无法再据残留事实签发新证。
        status, body = self.post("/plans/plan-1/certificates", {}, MGR_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "no_certificate_basis")


if __name__ == "__main__":
    unittest.main()
