"""HTTP 接入层契约测试。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rotation.api import build_server, health_payload
from rotation.domain import RotationService
from rotation.store import EventStore
from rotation.helpers import (
    ADMIN_A, ADMIN_B, MENTOR, STAFF, PRIV,
)


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = RotationService(EventStore(),
                                      clock=lambda: "2026-02-01")
        cls.server = build_server(0, cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, actor=ADMIN_A):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        request = Request(f"{self.base_url}{path}", data=data, method=method,
                          headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload

    def test_health_contract_unchanged(self):
        status, payload = self.request("GET", "/health", actor=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload, health_payload())

    def test_write_requires_actor_header(self):
        status, payload = self.request("POST", "/staff", {
            "staff_id": "s1", "name": "x", "role": "nurse"}, actor=None)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

    def test_full_flow_over_http(self):
        status, _ = self.request("POST", "/staff", {
            "staff_id": STAFF, "name": "王医生", "role": "physician"})
        self.assertEqual(status, 201)
        self.request("POST", "/staff", {
            "staff_id": MENTOR, "name": "陈带教", "role": "physician"},
            actor=ADMIN_B)
        self.request("POST", "/staff/credentials", {
            "staff_id": STAFF, "code": PRIV, "title": "独立接诊",
            "expires_on": "2026-12-31"})

        status, payload = self.request("POST", "/agreements", {
            "roster_id": "R1", "agreement_no": "AGR-1",
            "staff_id": STAFF, "sending_org": "总院", "receiving_org": "县医院",
            "practice_site": "县医院内科", "sending_admin": ADMIN_A,
            "receiving_admin": ADMIN_B, "mentor_id": MENTOR,
            "privileges": [PRIV], "learning_goals": ["独立接诊"],
            "duties": ["门诊"],
            "start_date": "2026-01-01", "end_date": "2026-03-31"})
        self.assertEqual(status, 201, payload)

        # 互斥任务：第二个被 409 拒绝
        status, _ = self.request("POST", "/assignments", {
            "assignment_id": "A1", "roster_id": "R1", "duty": "门诊",
            "start": "2026-02-10", "end": "2026-02-10"}, actor=ADMIN_B)
        self.assertEqual(status, 201)
        status, payload = self.request("POST", "/assignments", {
            "assignment_id": "A2", "roster_id": "R1", "duty": "外院会诊",
            "start": "2026-02-10", "end": "2026-02-11"}, actor=ADMIN_B)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "conflict")

        # 回执幂等
        receipt = {"receipt_id": "RC-1", "roster_id": "R1",
                   "privilege_code": PRIV, "recorded_on": "2026-01-10"}
        status, first = self.request("POST", "/receipts", receipt,
                                     actor=ADMIN_B)
        status, retry = self.request("POST", "/receipts", receipt,
                                     actor=ADMIN_B)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(retry["deduplicated"])

        # 考核 → 独立能力
        status, _ = self.request("POST", "/assessments", {
            "assessment_id": "AS-1", "roster_id": "R1",
            "privilege_code": PRIV, "graded_on": "2026-01-20"}, actor=MENTOR)
        self.assertEqual(status, 201)
        status, capability = self.request(
            "GET", f"/staff/{STAFF}/capability?on_date=2026-02-01", actor=None)
        self.assertTrue(capability["privileges"][0]["independent"])

        # 病例最小可见
        self.request("POST", "/cases", {
            "case_id": "C1", "roster_id": "R1", "patient_ref": "P-1",
            "summary_ref": "summaries/C1"}, actor=ADMIN_B)
        status, mine = self.request("GET", f"/staff/{STAFF}/cases", actor=None)
        self.assertEqual([c["case_id"] for c in mine["cases"]], ["C1"])
        status, others = self.request(
            "GET", f"/staff/{MENTOR}/cases", actor=None)
        self.assertEqual(others["cases"], [])

    def test_dual_approval_amendment_flow(self):
        self.request("POST", "/staff", {
            "staff_id": "dr-lin", "name": "林医生", "role": "physician"})
        self.request("POST", "/staff", {
            "staff_id": "mentor-zhao", "name": "赵带教", "role": "physician"},
            actor=ADMIN_B)
        self.request("POST", "/staff/credentials", {
            "staff_id": "dr-lin", "code": "P2", "title": "权限",
            "expires_on": "2026-12-31"})
        self.request("POST", "/agreements", {
            "roster_id": "R2", "agreement_no": "AGR-2",
            "staff_id": "dr-lin", "sending_org": "总院",
            "receiving_org": "县医院", "practice_site": "内科",
            "sending_admin": ADMIN_A, "receiving_admin": ADMIN_B,
            "mentor_id": "mentor-zhao", "privileges": ["P2"],
            "start_date": "2026-01-01", "end_date": "2026-03-31"})
        status, pending = self.request(
            "POST", "/agreements/R2/amendments", {
                "kind": "extension", "party": "sending",
                "changes": {"window": {"start": "2026-04-01",
                                       "end": "2026-04-20"}}})
        self.assertEqual(pending["status"], "pending")
        amendment_id = pending["amendment_id"]
        status, applied = self.request(
            "POST", f"/agreements/R2/amendments/{amendment_id}/approvals",
            {"party": "receiving"}, actor=ADMIN_B)
        self.assertEqual(status, 200)
        self.assertEqual(applied["status"], "applied")

    def test_trace_endpoint_restores_history(self):
        status, trace = self.request("GET", "/trace", actor=None)
        self.assertEqual(status, 200)
        self.assertIn("rosters", trace)
        status, old = self.request("GET", "/trace?at_seq=1", actor=None)
        self.assertEqual(old["at_seq"], 1)

    def test_unknown_route_404(self):
        status, payload = self.request("GET", "/unknown", actor=None)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_bad_json_is_validation_error(self):
        request = Request(f"{self.base_url}/staff",
                          data=b"{not-json", method="POST",
                          headers={"X-Actor-Id": ADMIN_A,
                                   "Content-Type": "application/json"})
        try:
            urlopen(request, timeout=3)
            self.fail("应当返回 400")
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            error.close()


if __name__ == "__main__":
    unittest.main()
