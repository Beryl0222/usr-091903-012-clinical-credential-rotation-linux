"""轮转履约领域：协议锁定、权责判定、互斥、幂等授予、逐项交接、结业事实。

设计要点：
- 事件溯源，状态由事件归约得到，restore(seq) 可还原任一操作时的权责；
- 能力 = 有效窗口 ∩ 证照有效 ∩ 监管回执 ∩ 已签署且未撤销的考核；
- 回执与考核以业务编号幂等，重试只回放，不重复授予；
- 交接按事项逐项确认，每项指定新责任人，禁止整体改名。
"""

import re
import threading
from datetime import date

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MUTEX_KIND = "exclusive"


class DomainError(Exception):
    code = "domain_error"


class ValidationError(DomainError):
    code = "validation_error"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class AuthorizationError(DomainError):
    code = "forbidden"


def _date(value, field):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
    return value


def _require(value, field):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"缺少必填项：{field}")
    return value


def _overlap(start_a, end_a, start_b, end_b):
    return start_a <= end_b and start_b <= end_a


class World:
    """事件归约出的当前状态。"""

    def __init__(self):
        self.seq = 0
        self.staff = {}
        self.credentials = {}
        self.rosters = {}
        self.assignments = {}
        self.assessments = {}
        self.receipts = {}
        self.handovers = []
        self.cases = {}
        self.certificates = {}

    def apply(self, event):
        self.seq = event["seq"]
        kind = event["type"]
        data = event["data"]
        handler = getattr(self, f"_on_{kind}", None)
        if handler:
            handler(data, event)

    # ---- 人员与证照 -------------------------------------------------------
    def _on_StaffRegistered(self, data, event):
        self.staff[data["staff_id"]] = {
            "staff_id": data["staff_id"],
            "name": data["name"],
            "role": data["role"],
            "seq": event["seq"],
        }
        self.credentials.setdefault(data["staff_id"], {})

    def _on_CredentialRecorded(self, data, event):
        self.credentials.setdefault(data["staff_id"], {})[data["code"]] = {
            "code": data["code"],
            "title": data.get("title", data["code"]),
            "expires_on": data.get("expires_on"),
            "revoked": False,
            "seq": event["seq"],
        }

    def _on_CredentialRevoked(self, data, event):
        record = self.credentials.get(data["staff_id"], {}).get(data["code"])
        if record:
            record["revoked"] = True

    # ---- 轮转协议 ---------------------------------------------------------
    def _on_AgreementLocked(self, data, event):
        self.rosters[data["roster_id"]] = {
            "roster_id": data["roster_id"],
            "agreement_no": data["agreement_no"],
            "staff_id": data["staff_id"],
            "sending_org": data["sending_org"],
            "receiving_org": data["receiving_org"],
            "practice_site": data["practice_site"],
            "sending_admin": data["sending_admin"],
            "receiving_admin": data["receiving_admin"],
            "mentor_id": data["mentor_id"],
            "privileges": list(data["privileges"]),
            "learning_goals": list(data.get("learning_goals", [])),
            "duties": list(data.get("duties", [])),
            "windows": [
                {"start": data["start_date"], "end": data["end_date"],
                 "kind": "rotation", "status": "locked"}
            ],
            "amendments": [],
            "mentor_on_leave": False,
            "status": "active",
            "locked_seq": event["seq"],
        }

    def _on_AmendmentProposed(self, data, event):
        roster = self.rosters[data["roster_id"]]
        roster["amendments"].append({
            "amendment_id": data["amendment_id"],
            "kind": data["kind"],
            "changes": data["changes"],
            "approvals": list(data.get("approvals", [])),
            "status": "pending",
            "proposed_seq": event["seq"],
        })

    def _on_AmendmentApproved(self, data, event):
        roster = self.rosters[data["roster_id"]]
        amendment = next(a for a in roster["amendments"]
                         if a["amendment_id"] == data["amendment_id"])
        if data["party"] not in amendment["approvals"]:
            amendment["approvals"].append(data["party"])

    def _on_AmendmentApplied(self, data, event):
        roster = self.rosters[data["roster_id"]]
        amendment = next(a for a in roster["amendments"]
                         if a["amendment_id"] == data["amendment_id"])
        amendment["status"] = "applied"
        changes = amendment["changes"]
        if amendment["kind"] == "temp-support":
            window = changes["window"]
            roster["windows"].append({
                "start": window["start"], "end": window["end"],
                "kind": "temp-support", "status": "locked",
                "site": window.get("site"),
            })
        elif amendment["kind"] == "extension":
            window = changes["window"]
            for window_row in roster["windows"]:
                if window_row["kind"] in ("rotation", "extension") \
                        and window_row["status"] == "locked":
                    window_row["status"] = "replaced"
            roster["windows"].append({
                "start": window["start"], "end": window["end"],
                "kind": "extension", "status": "locked",
            })
        if "privileges" in changes:
            roster["privileges"] = list(changes["privileges"])
        if "duties" in changes:
            roster["duties"] = list(changes["duties"])
        if "mentor_id" in changes:
            roster["mentor_id"] = changes["mentor_id"]

    def _on_MentorLeaveSet(self, data, event):
        self.rosters[data["roster_id"]]["mentor_on_leave"] = data["on_leave"]

    def _on_EmergencyReturned(self, data, event):
        roster = self.rosters[data["roster_id"]]
        cutoff = data["return_date"]
        for window in roster["windows"]:
            if window["status"] == "locked" and window["end"] > cutoff:
                window["end"] = cutoff
                window["status"] = "ended"
        roster["status"] = "returned"

    # ---- 职责任务 ---------------------------------------------------------
    def _on_AssignmentCreated(self, data, event):
        self.assignments[data["assignment_id"]] = {
            "assignment_id": data["assignment_id"],
            "roster_id": data["roster_id"],
            "staff_id": data["staff_id"],
            "duty": data["duty"],
            "site": data.get("site"),
            "start": data["start"],
            "end": data["end"],
            "kind": data.get("kind", MUTEX_KIND),
            "status": "pending",
            "seq": event["seq"],
        }

    def _on_AssignmentAccepted(self, data, event):
        self.assignments[data["assignment_id"]]["status"] = "accepted"

    def _on_AssignmentDeclined(self, data, event):
        self.assignments[data["assignment_id"]]["status"] = "declined"

    def _on_AssignmentCompleted(self, data, event):
        self.assignments[data["assignment_id"]]["status"] = "completed"

    def _on_AssignmentReassigned(self, data, event):
        assignment = self.assignments[data["assignment_id"]]
        assignment["status"] = "reassigned"
        assignment["new_owner_id"] = data["new_owner_id"]

    # ---- 监管回执与考核 ---------------------------------------------------
    def _on_RegulatoryReceiptRecorded(self, data, event):
        self.receipts[data["receipt_id"]] = {
            "receipt_id": data["receipt_id"],
            "roster_id": data["roster_id"],
            "staff_id": data["staff_id"],
            "privilege_code": data["privilege_code"],
            "recorded_on": data["recorded_on"],
            "seq": event["seq"],
        }

    def _on_AssessmentAwarded(self, data, event):
        self.assessments[data["assessment_id"]] = {
            "assessment_id": data["assessment_id"],
            "roster_id": data["roster_id"],
            "staff_id": data["staff_id"],
            "privilege_code": data["privilege_code"],
            "assessor_id": data["assessor_id"],
            "graded_on": data["graded_on"],
            "result": data.get("result", "pass"),
            "revoked": False,
            "seq": event["seq"],
        }

    def _on_AssessmentRevoked(self, data, event):
        assessment = self.assessments[data["assessment_id"]]
        assessment["revoked"] = True
        assessment["revoke_reason"] = data.get("reason", "")

    # ---- 交接 -------------------------------------------------------------
    def _on_HandoverTriggered(self, data, event):
        self.handovers.append({
            "handover_id": data["handover_id"],
            "roster_id": data["roster_id"],
            "reason": data["reason"],
            "trigger_date": data["trigger_date"],
            "status": "open",
            "items": [dict(item) for item in data["items"]],
            "seq": event["seq"],
        })

    def _on_HandoverItemConfirmed(self, data, event):
        handover = next(h for h in self.handovers
                        if h["handover_id"] == data["handover_id"])
        item = next(i for i in handover["items"] if i["item_id"] == data["item_id"])
        item["status"] = "confirmed"
        item["new_owner_id"] = data["new_owner_id"]
        item["confirmed_by"] = data["confirmed_by"]
        item["confirmed_seq"] = event["seq"]

    def _on_HandoverCompleted(self, data, event):
        handover = next(h for h in self.handovers
                        if h["handover_id"] == data["handover_id"])
        handover["status"] = "completed"
        handover["completed_seq"] = event["seq"]

    # ---- 病例与随访 -------------------------------------------------------
    def _on_CaseAssigned(self, data, event):
        self.cases[data["case_id"]] = {
            "case_id": data["case_id"],
            "roster_id": data["roster_id"],
            "staff_id": data["staff_id"],
            "patient_ref": data["patient_ref"],
            "summary_ref": data["summary_ref"],
            "status": "open",
            "signed_by": None,
            "signed_on": None,
            "followup": None,
            "seq": event["seq"],
        }

    def _on_CaseSigned(self, data, event):
        case = self.cases[data["case_id"]]
        case["status"] = "signed"
        case["signed_by"] = data["staff_id"]
        case["signed_on"] = data["signed_on"]
        case["sign_independent"] = data.get("independent", False)

    def _on_CaseReassigned(self, data, event):
        case = self.cases[data["case_id"]]
        case["staff_id"] = data["new_owner_id"]

    def _on_FollowupOpened(self, data, event):
        case = self.cases[data["case_id"]]
        case["followup"] = {"due_date": data["due_date"], "status": "open",
                            "owner_id": case["staff_id"]}

    def _on_FollowupDone(self, data, event):
        case = self.cases[data["case_id"]]
        if case["followup"]:
            case["followup"]["status"] = "done"

    def _on_FollowupTransferred(self, data, event):
        case = self.cases[data["case_id"]]
        if case["followup"]:
            case["followup"]["owner_id"] = data["new_owner_id"]

    # ---- 导师与结业 -------------------------------------------------------
    def _on_MentorReplaced(self, data, event):
        roster = self.rosters[data["roster_id"]]
        roster["mentor_id"] = data["new_mentor_id"]
        roster["mentor_on_leave"] = False

    def _on_CertificateIssued(self, data, event):
        self.certificates[data["roster_id"]] = {
            "certificate_no": data["certificate_no"],
            "roster_id": data["roster_id"],
            "staff_id": data["staff_id"],
            "assessment_ids": list(data["assessment_ids"]),
            "issued_on": data["issued_on"],
            "voided": False,
            "seq": event["seq"],
        }

    def _on_CertificateVoided(self, data, event):
        certificate = self.certificates.get(data["roster_id"])
        if certificate:
            certificate["voided"] = True


class RotationService:
    def __init__(self, store, clock=None):
        self.store = store
        self._clock = clock or (lambda: date.today().isoformat())
        self._lock = threading.RLock()

    # ===== 基础设施 ========================================================
    def _today(self):
        return self._clock()

    def _append(self, event_type, data, actor):
        return self.store.append(event_type, data, actor)

    def _world(self, at_seq=None):
        world = World()
        for event in self.store.all()[: at_seq]:
            world.apply(event)
        return world

    def _roster(self, world, roster_id):
        roster = world.rosters.get(roster_id)
        if not roster:
            raise NotFoundError(f"轮转协议不存在：{roster_id}")
        return roster

    def _staff(self, world, staff_id):
        if staff_id not in world.staff:
            raise NotFoundError(f"人员不存在：{staff_id}")
        return world.staff[staff_id]

    def _active_windows(self, roster, on_date=None):
        on_date = on_date or self._today()
        return [
            w for w in roster["windows"]
            if w["status"] == "locked" and w["start"] <= on_date <= w["end"]
        ]

    def _credential_valid(self, world, staff_id, code, on_date):
        record = world.credentials.get(staff_id, {}).get(code)
        if not record or record["revoked"]:
            return False
        return not record["expires_on"] or record["expires_on"] >= on_date

    def _privilege_facts(self, world, roster, code, on_date):
        """汇聚一项权限在指定日期的全部判定事实（与具体窗口无关部分）。"""
        staff_id = roster["staff_id"]
        windows = [
            {"start": w["start"], "end": w["end"], "kind": w["kind"],
             "site": w.get("site", roster["practice_site"])}
            for w in self._active_windows(roster, on_date)
        ]
        in_scope = code in roster["privileges"]
        credential = world.credentials.get(staff_id, {}).get(code)
        receipt = next((
            r for r in world.receipts.values()
            if r["roster_id"] == roster["roster_id"]
            and r["privilege_code"] == code
            and r["recorded_on"] <= on_date
        ), None)
        assessment = next((
            a for a in world.assessments.values()
            if a["roster_id"] == roster["roster_id"]
            and a["privilege_code"] == code
            and not a["revoked"]
            and a["graded_on"] <= on_date
        ), None)
        credential_ok = self._credential_valid(world, staff_id, code, on_date)
        return {
            "code": code,
            "in_scope": in_scope,
            "windows": windows,
            "credential": credential,
            "credential_valid": credential_ok,
            "receipt": receipt,
            "assessment": assessment,
            "supervised": bool(windows) and in_scope and credential_ok,
            "independent": bool(windows) and in_scope and credential_ok
            and bool(receipt) and bool(assessment),
        }

    # ===== 人员与证照 ======================================================
    def register_staff(self, staff_id, name, role, actor):
        _require(staff_id, "staff_id")
        _require(name, "name")
        if role not in ("physician", "nurse"):
            raise ValidationError("role 仅支持 physician / nurse")
        world = self._world()
        if staff_id in world.staff:
            raise ConflictError(f"人员已注册：{staff_id}")
        event = self._append("StaffRegistered", {
            "staff_id": staff_id, "name": name, "role": role,
        }, actor)
        return {"staff_id": staff_id, "seq": event["seq"]}

    def record_credential(self, staff_id, code, title, expires_on, actor):
        _require(code, "code")
        if expires_on is not None:
            _date(expires_on, "expires_on")
        with self._lock:
            world = self._world()
            self._staff(world, staff_id)
            event = self._append("CredentialRecorded", {
                "staff_id": staff_id, "code": code, "title": title or code,
                "expires_on": expires_on,
            }, actor)
        return {"staff_id": staff_id, "code": code, "seq": event["seq"]}

    def revoke_credential(self, staff_id, code, actor):
        with self._lock:
            world = self._world()
            record = world.credentials.get(staff_id, {}).get(code)
            if not record:
                raise NotFoundError(f"证照不存在：{staff_id}/{code}")
            if record["revoked"]:
                raise ConflictError("证照已被撤销")
            event = self._append("CredentialRevoked", {
                "staff_id": staff_id, "code": code,
            }, actor)
        return {"seq": event["seq"]}

    # ===== 协议锁定 ========================================================
    def _ensure_no_overlapping_roster(self, world, staff_id, start, end,
                                      exclude_roster=None):
        for roster in world.rosters.values():
            if roster["staff_id"] != staff_id:
                continue
            if exclude_roster and roster["roster_id"] == exclude_roster:
                continue
            for window in roster["windows"]:
                if window["status"] == "locked" and _overlap(
                        start, end, window["start"], window["end"]):
                    raise ConflictError(
                        f"人员 {staff_id} 在 {window['start']}~{window['end']} "
                        f"已承担轮转 {roster['roster_id']}，两院职责时间互斥"
                    )

    def lock_agreement(self, payload, actor):
        roster_id = _require(payload.get("roster_id"), "roster_id")
        agreement_no = _require(payload.get("agreement_no"), "agreement_no")
        staff_id = _require(payload.get("staff_id"), "staff_id")
        start = _date(_require(payload.get("start_date"), "start_date"), "start_date")
        end = _date(_require(payload.get("end_date"), "end_date") , "end_date")
        if start > end:
            raise ValidationError("生效时段开始不得晚于结束")
        for field in ("sending_org", "receiving_org", "practice_site",
                      "sending_admin", "receiving_admin", "mentor_id"):
            _require(payload.get(field), field)
        privileges = payload.get("privileges")
        if not isinstance(privileges, list) or not privileges:
            raise ValidationError("必须在出发前锁定至少一项专业权限")
        with self._lock:
            world = self._world()
            self._staff(world, staff_id)
            if payload["mentor_id"] not in world.staff:
                raise ValidationError("带教人必须先注册")
            if roster_id in world.rosters:
                raise ConflictError(f"轮转协议已锁定：{roster_id}")
            if payload["sending_admin"] == payload["receiving_admin"]:
                raise ValidationError("派出方与接收方批准人不得为同一人")
            if payload["mentor_id"] == staff_id:
                raise ValidationError("带教人不能是轮转人员本人")
            self._ensure_no_overlapping_roster(world, staff_id, start, end)
            event = self._append("AgreementLocked", {
                "roster_id": roster_id,
                "agreement_no": agreement_no,
                "staff_id": staff_id,
                "sending_org": payload["sending_org"],
                "receiving_org": payload["receiving_org"],
                "practice_site": payload["practice_site"],
                "sending_admin": payload["sending_admin"],
                "receiving_admin": payload["receiving_admin"],
                "mentor_id": payload["mentor_id"],
                "privileges": list(privileges),
                "learning_goals": payload.get("learning_goals", []),
                "duties": payload.get("duties", []),
                "start_date": start,
                "end_date": end,
            }, actor)
        return self._agreement_view(self._world(), roster_id, event["seq"])

    # ===== 修正案：双方批准才改变职责 ======================================
    def propose_amendment(self, roster_id, kind, changes, party, actor,
                          amendment_id=None):
        world = self._world()
        roster = self._roster(world, roster_id)
        if kind not in ("temp-support", "extension", "scope-change"):
            raise ValidationError("修正案类型非法")
        if party not in ("sending", "receiving"):
            raise ValidationError("party 仅支持 sending / receiving")
        admin = roster["sending_admin"] if party == "sending" else roster["receiving_admin"]
        if actor != admin:
            raise AuthorizationError("只有该方批准人可代表本方提出/同意修正")
        changes = dict(changes or {})
        if kind in ("temp-support", "extension"):
            window = changes.get("window")
            if not window:
                raise ValidationError("临时支援/延长必须携带 window 时段")
            w_start = _date(window["start"], "window.start")
            w_end = _date(window["end"], "window.end")
            if w_start > w_end:
                raise ValidationError("修正案时段开始不得晚于结束")
            changes["window"] = {"start": w_start, "end": w_end,
                                 "site": window.get("site")}
            if kind == "extension":
                if w_start < roster["windows"][-1]["end"]:
                    raise ValidationError("延长时段起点不得早于现行时段结束")
                self._ensure_no_overlapping_roster(
                    world, roster["staff_id"], w_start, w_end,
                    exclude_roster=roster_id)
            if kind == "temp-support":
                self._ensure_no_overlapping_roster(
                    world, roster["staff_id"], w_start, w_end,
                    exclude_roster=roster_id)
        amendment_id = amendment_id or f"AM-{roster_id}-{len(roster['amendments']) + 1}"
        if any(a["amendment_id"] == amendment_id for a in roster["amendments"]):
            raise ConflictError(f"修正案已存在：{amendment_id}")
        with self._lock:
            event = self._append("AmendmentProposed", {
                "roster_id": roster_id,
                "amendment_id": amendment_id,
                "kind": kind,
                "changes": changes,
                "approvals": [party],
            }, actor)
        return {"amendment_id": amendment_id, "status": "pending",
                "approvals": [party], "seq": event["seq"]}

    def approve_amendment(self, roster_id, amendment_id, party, actor):
        if party not in ("sending", "receiving"):
            raise ValidationError("party 仅支持 sending / receiving")
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            amendment = next((a for a in roster["amendments"]
                              if a["amendment_id"] == amendment_id), None)
            if not amendment:
                raise NotFoundError(f"修正案不存在：{amendment_id}")
            if amendment["status"] != "pending":
                raise ConflictError("修正案已生效，不可重复批准")
            admin = roster["sending_admin"] if party == "sending" else roster["receiving_admin"]
            if actor != admin:
                raise AuthorizationError("只有该方批准人可代表本方批准")
            if party in amendment["approvals"]:
                raise ConflictError("该方已批准，不可重复批准")
            will_apply = len(set(amendment["approvals"]) | {party}) == 2
            if will_apply and amendment["kind"] in ("temp-support", "extension"):
                window = amendment["changes"]["window"]
                self._ensure_no_overlapping_roster(
                    world, roster["staff_id"],
                    window["start"], window["end"],
                    exclude_roster=roster_id)
            event = self._append("AmendmentApproved", {
                "roster_id": roster_id, "amendment_id": amendment_id,
                "party": party,
            }, actor)
            applied = None
            if will_apply:
                applied = self._append("AmendmentApplied", {
                    "roster_id": roster_id, "amendment_id": amendment_id,
                    "changes": amendment["changes"],
                    "kind": amendment["kind"],
                }, actor)
        return {
            "amendment_id": amendment_id,
            "status": "applied" if applied else "pending",
            "approvals": sorted(set(amendment["approvals"]) | {party}),
            "seq": (applied or event)["seq"],
        }

    # ===== 互斥职责任务 ====================================================
    def create_assignment(self, payload, actor):
        assignment_id = _require(payload.get("assignment_id"), "assignment_id")
        roster_id = _require(payload.get("roster_id"), "roster_id")
        duty = _require(payload.get("duty"), "duty")
        start = _date(_require(payload.get("start"), "start"), "start")
        end = _date(_require(payload.get("end"), "end"), "end")
        if start > end:
            raise ValidationError("任务开始不得晚于结束")
        kind = payload.get("kind", MUTEX_KIND)
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            if actor != roster["receiving_admin"]:
                raise AuthorizationError("只有接收科室管理方可派发任务")
            if assignment_id in world.assignments:
                raise ConflictError(f"任务已存在：{assignment_id}")
            windows = self._active_windows(roster, start)
            if not windows:
                raise ConflictError("任务起点不在任何已批准的有效时段内")
            site = payload.get("site", roster["practice_site"])
            if not any(w.get("site", roster["practice_site"]) == site
                       for w in windows):
                raise ConflictError(
                    f"执业地点 {site} 在该日期没有经双方批准的有效窗口"
                )
            staff_id = roster["staff_id"]
            if kind == MUTEX_KIND:
                for other in world.assignments.values():
                    if other["staff_id"] != staff_id:
                        continue
                    if other["status"] in ("declined", "completed", "reassigned"):
                        continue
                    if other["kind"] == MUTEX_KIND and _overlap(
                            start, end, other["start"], other["end"]):
                        raise ConflictError(
                            f"与互斥任务 {other['assignment_id']} 时间冲突，必须拒绝"
                        )
            event = self._append("AssignmentCreated", {
                "assignment_id": assignment_id,
                "roster_id": roster_id,
                "staff_id": staff_id,
                "duty": duty,
                "site": payload.get("site", roster["practice_site"]),
                "start": start, "end": end, "kind": kind,
            }, actor)
        return {"assignment_id": assignment_id, "status": "pending",
                "seq": event["seq"]}

    def respond_assignment(self, assignment_id, accept, actor):
        with self._lock:
            world = self._world()
            assignment = world.assignments.get(assignment_id)
            if not assignment:
                raise NotFoundError(f"任务不存在：{assignment_id}")
            if actor != assignment["staff_id"]:
                raise AuthorizationError("只有当事人可以接受/拒绝任务")
            if assignment["status"] != "pending":
                raise ConflictError("任务已被处理")
            roster = self._roster(world, assignment["roster_id"])
            if not self._active_windows(roster, assignment["start"]):
                raise ConflictError("任务已不在任何有效轮转时段内，必须拒绝")
            if accept:
                # 接受瞬间再次校验互斥，防止派发后出现新任务。
                for other in world.assignments.values():
                    if other["assignment_id"] == assignment_id:
                        continue
                    if other["staff_id"] != assignment["staff_id"]:
                        continue
                    if other["status"] in ("declined", "completed", "reassigned"):
                        continue
                    if other["kind"] == MUTEX_KIND and assignment["kind"] == MUTEX_KIND \
                            and _overlap(assignment["start"], assignment["end"],
                                         other["start"], other["end"]):
                        raise ConflictError(
                            f"接受时发现与 {other['assignment_id']} 互斥，必须拒绝"
                        )
                event = self._append("AssignmentAccepted",
                                     {"assignment_id": assignment_id}, actor)
                status = "accepted"
            else:
                event = self._append("AssignmentDeclined",
                                     {"assignment_id": assignment_id}, actor)
                status = "declined"
        return {"assignment_id": assignment_id, "status": status,
                "seq": event["seq"]}

    def complete_assignment(self, assignment_id, actor):
        with self._lock:
            world = self._world()
            assignment = world.assignments.get(assignment_id)
            if not assignment:
                raise NotFoundError(f"任务不存在：{assignment_id}")
            roster = self._roster(world, assignment["roster_id"])
            if actor not in (assignment["staff_id"], roster["receiving_admin"]):
                raise AuthorizationError("只有当事人或接收管理方可完结任务")
            if assignment["status"] != "accepted":
                raise ConflictError("只有已接受的任务可以完结")
            event = self._append("AssignmentCompleted",
                                 {"assignment_id": assignment_id}, actor)
        return {"seq": event["seq"]}

    # ===== 监管回执：幂等，不重复授予 ======================================
    def record_receipt(self, payload, actor):
        receipt_id = _require(payload.get("receipt_id"), "receipt_id")
        roster_id = _require(payload.get("roster_id"), "roster_id")
        code = _require(payload.get("privilege_code"), "privilege_code")
        recorded_on = _date(_require(payload.get("recorded_on"), "recorded_on"),
                            "recorded_on")
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            if actor != roster["receiving_admin"]:
                raise AuthorizationError("监管回执由接收机构管理方登记")
            existing = world.receipts.get(receipt_id)
            if existing:
                # 重试：回放既有回执，不产生新事件、不改变授予时间。
                if (existing["roster_id"], existing["privilege_code"]) != (
                        roster_id, code):
                    raise ConflictError("回执编号已用于其他权限登记")
                return {"receipt_id": receipt_id, "deduplicated": True,
                        "seq": existing["seq"]}
            if code not in roster["privileges"]:
                raise ValidationError("回执权限不在协议锁定的专业权限范围内")
            event = self._append("RegulatoryReceiptRecorded", {
                "receipt_id": receipt_id,
                "roster_id": roster_id,
                "staff_id": roster["staff_id"],
                "privilege_code": code,
                "recorded_on": recorded_on,
            }, actor)
        return {"receipt_id": receipt_id, "deduplicated": False,
                "seq": event["seq"]}

    # ===== 考核：幂等授予、可撤销 ==========================================
    def award_assessment(self, payload, actor):
        assessment_id = _require(payload.get("assessment_id"), "assessment_id")
        roster_id = _require(payload.get("roster_id"), "roster_id")
        code = _require(payload.get("privilege_code"), "privilege_code")
        graded_on = _date(_require(payload.get("graded_on"), "graded_on"),
                          "graded_on")
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            existing = world.assessments.get(assessment_id)
            if existing:
                if (existing["roster_id"], existing["privilege_code"]) != (
                        roster_id, code):
                    raise ConflictError("考核编号已用于其他考核")
                # 上传重试：原样回放，绝不二次授予。
                return {"assessment_id": assessment_id, "deduplicated": True,
                        "granted": not existing["revoked"],
                        "seq": existing["seq"]}
            if actor != roster["mentor_id"]:
                raise AuthorizationError("只有当前带教人可登记考核结果")
            if roster["mentor_on_leave"]:
                raise ConflictError("带教人休假中，须完成交接后由新带教人考核")
            if code not in roster["privileges"]:
                raise ValidationError("考核权限不在协议锁定的专业权限范围内")
            if not self._credential_valid(world, roster["staff_id"], code, graded_on):
                raise ConflictError("轮转人员该权限证照无效或已过期，不能授予")
            if not self._active_windows(roster, graded_on):
                raise ConflictError("考核日期不在有效轮转时段内")
            event = self._append("AssessmentAwarded", {
                "assessment_id": assessment_id,
                "roster_id": roster_id,
                "staff_id": roster["staff_id"],
                "privilege_code": code,
                "assessor_id": actor,
                "graded_on": graded_on,
                "result": payload.get("result", "pass"),
            }, actor)
        return {"assessment_id": assessment_id, "deduplicated": False,
                "granted": True, "seq": event["seq"]}

    def revoke_assessment(self, assessment_id, reason, actor):
        with self._lock:
            world = self._world()
            assessment = world.assessments.get(assessment_id)
            if not assessment:
                raise NotFoundError(f"考核不存在：{assessment_id}")
            if assessment["revoked"]:
                raise ConflictError("考核已被撤销")
            roster = self._roster(world, assessment["roster_id"])
            if actor not in (roster["receiving_admin"], roster["sending_admin"]):
                raise AuthorizationError("只有医务处/护理部管理方可撤销考核")
            self._append("AssessmentRevoked", {
                "assessment_id": assessment_id,
                "reason": reason or "",
            }, actor)
            certificate = world.certificates.get(assessment["roster_id"])
            if certificate and not certificate["voided"]:
                still_valid = [
                    a for a in world.assessments.values()
                    if a["roster_id"] == assessment["roster_id"]
                    and not a["revoked"]
                    and a["assessment_id"] != assessment_id
                ]
                if not still_valid:
                    self._append("CertificateVoided", {
                        "roster_id": assessment["roster_id"],
                        "assessment_id": assessment_id,
                    }, actor)
        return {"assessment_id": assessment_id, "revoked": True}

    # ===== 带教人状态与交接 ================================================
    def set_mentor_leave(self, roster_id, on_leave, actor):
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            if actor != roster["receiving_admin"]:
                raise AuthorizationError("只有接收管理方可登记带教人休假")
            if roster["mentor_on_leave"] == on_leave:
                raise ConflictError("带教人休假状态未变化")
            self._append("MentorLeaveSet", {
                "roster_id": roster_id, "on_leave": on_leave,
            }, actor)
            if on_leave:
                self._trigger_handover(world, roster, "mentor_leave",
                                       self._today(), actor)
        return {"roster_id": roster_id, "mentor_on_leave": on_leave}

    def emergency_return(self, roster_id, return_date, actor):
        _date(return_date, "return_date")
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            if actor != roster["sending_admin"]:
                raise AuthorizationError("紧急返院由派出方管理方发起")
            if roster["status"] != "active":
                raise ConflictError("轮转已结束，不能再次紧急返院")
            if return_date < roster["windows"][0]["start"]:
                raise ValidationError("返院日期不得早于轮转开始")
            self._append("EmergencyReturned", {
                "roster_id": roster_id, "return_date": return_date,
            }, actor)
            world = self._world()
            self._trigger_handover(world, world.rosters[roster_id],
                                   "emergency_return", return_date, actor)
        return {"roster_id": roster_id, "status": "returned",
                "return_date": return_date}

    def _open_handover_items(self, world, roster, reason, trigger_date):
        items = []
        for case in world.cases.values():
            if case["roster_id"] != roster["roster_id"]:
                continue
            if case["staff_id"] != roster["staff_id"]:
                continue  # 已转给他人的事项不再属于本次交接
            if case["status"] != "signed":
                items.append({
                    "item_id": f"{case['case_id']}:sign",
                    "type": "case_sign",
                    "ref": case["case_id"],
                    "detail": "未签署病历",
                    "status": "open",
                })
            if case["followup"] and case["followup"]["status"] == "open":
                items.append({
                    "item_id": f"{case['case_id']}:followup",
                    "type": "followup",
                    "ref": case["case_id"],
                    "detail": "未完成随访",
                    "due_date": case["followup"]["due_date"],
                    "status": "open",
                })
        for assignment in world.assignments.values():
            if assignment["roster_id"] != roster["roster_id"]:
                continue
            if assignment["staff_id"] != roster["staff_id"]:
                continue
            if assignment["status"] == "accepted" and assignment["end"] >= trigger_date:
                items.append({
                    "item_id": f"{assignment['assignment_id']}:duty",
                    "type": "assignment",
                    "ref": assignment["assignment_id"],
                    "detail": assignment["duty"],
                    "status": "open",
                })
            elif (reason == "emergency_return"
                  and assignment["status"] == "pending"
                  and assignment["end"] >= trigger_date):
                items.append({
                    "item_id": f"{assignment['assignment_id']}:duty",
                    "type": "assignment",
                    "ref": assignment["assignment_id"],
                    "detail": f"待接受任务：{assignment['duty']}",
                    "status": "open",
                })
        if reason in ("mentor_leave", "emergency_return"):
            items.append({
                "item_id": f"{roster['roster_id']}:supervisor",
                "type": "supervisor",
                "ref": roster["roster_id"],
                "detail": "带教/监督责任改派",
                "status": "open",
            })
        return items

    def _trigger_handover(self, world, roster, reason, trigger_date, actor):
        open_handover = next((h for h in world.handovers
                              if h["roster_id"] == roster["roster_id"]
                              and h["status"] == "open"), None)
        if open_handover:
            raise ConflictError(f"已有未完成交接清单：{open_handover['handover_id']}")
        items = self._open_handover_items(world, roster, reason, trigger_date)
        handover_id = f"HO-{roster['roster_id']}-{len(world.handovers) + 1}"
        self._append("HandoverTriggered", {
            "handover_id": handover_id,
            "roster_id": roster["roster_id"],
            "reason": reason,
            "trigger_date": trigger_date,
            "items": items,
        }, actor)
        return handover_id, items

    def trigger_handover_credential_expiry(self, roster_id, actor,
                                           trigger_date=None):
        """证照到期触发交接（区别于系统自动检测，由护理部/医务处确认发起）。"""
        trigger_date = _date(trigger_date or self._today(), "trigger_date")
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            if actor != roster["receiving_admin"]:
                raise AuthorizationError("只有接收管理方可发起交接")
            expired = [
                code for code in roster["privileges"]
                if not self._credential_valid(
                    world, roster["staff_id"], code, trigger_date)
            ]
            if not expired:
                raise ConflictError("当前没有到期/失效的证照，不能以此为由发起交接")
            handover_id, items = self._trigger_handover(
                world, roster, "credential_expiry", trigger_date, actor)
        return {"handover_id": handover_id, "expired_credentials": expired,
                "items": items}

    def confirm_handover_item(self, handover_id, item_id, new_owner_id, actor):
        """逐项确认：每个事项单独指定新责任人，不做整体改名。"""
        with self._lock:
            world = self._world()
            handover = next((h for h in world.handovers
                             if h["handover_id"] == handover_id), None)
            if not handover:
                raise NotFoundError(f"交接清单不存在：{handover_id}")
            if handover["status"] != "open":
                raise ConflictError("交接清单已完成")
            roster = self._roster(world, handover["roster_id"])
            if actor != roster["receiving_admin"]:
                raise AuthorizationError("只有接收管理方可逐项确认交接")
            item = next((i for i in handover["items"]
                         if i["item_id"] == item_id), None)
            if not item:
                raise NotFoundError(f"交接事项不存在：{item_id}")
            if item["status"] == "confirmed":
                raise ConflictError("该事项已由新责任人确认，禁止重复确认")
            self._staff(world, new_owner_id)
            if new_owner_id == roster["staff_id"] and item["type"] != "supervisor":
                raise ValidationError("新责任人不能仍是原轮转人员")
            if item["type"] == "case_sign":
                case = world.cases[item["ref"]]
                self._append("CaseReassigned", {
                    "case_id": case["case_id"],
                    "roster_id": roster["roster_id"],
                    "new_owner_id": new_owner_id,
                }, actor)
            elif item["type"] == "followup":
                self._append("FollowupTransferred", {
                    "case_id": item["ref"],
                    "new_owner_id": new_owner_id,
                }, actor)
            elif item["type"] == "assignment":
                self._append("AssignmentReassigned", {
                    "assignment_id": item["ref"],
                    "roster_id": roster["roster_id"],
                    "new_owner_id": new_owner_id,
                }, actor)
            elif item["type"] == "supervisor":
                if new_owner_id == roster["mentor_id"]:
                    raise ValidationError("新带教人不能与原带教人相同")
                self._append("MentorReplaced", {
                    "roster_id": roster["roster_id"],
                    "new_mentor_id": new_owner_id,
                }, actor)
            self._append("HandoverItemConfirmed", {
                "handover_id": handover_id,
                "item_id": item_id,
                "new_owner_id": new_owner_id,
                "confirmed_by": actor,
            }, actor)
            world = self._world()
            handover = next(h for h in world.handovers
                            if h["handover_id"] == handover_id)
            completed = all(i["status"] == "confirmed" for i in handover["items"])
            if completed:
                self._append("HandoverCompleted", {
                    "handover_id": handover_id,
                    "roster_id": roster["roster_id"],
                }, actor)
        return {"handover_id": handover_id, "item_id": item_id,
                "new_owner_id": new_owner_id, "handover_completed": completed}

    # ===== 病例与随访 ======================================================
    def assign_case(self, payload, actor):
        case_id = _require(payload.get("case_id"), "case_id")
        roster_id = _require(payload.get("roster_id"), "roster_id")
        patient_ref = _require(payload.get("patient_ref"), "patient_ref")
        summary_ref = _require(payload.get("summary_ref"), "summary_ref")
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            if actor != roster["receiving_admin"]:
                raise AuthorizationError("只有接收管理方可分派病例")
            if case_id in world.cases:
                raise ConflictError(f"病例已存在：{case_id}")
            if not self._active_windows(roster):
                raise ConflictError("轮转人员当前不在有效时段内，不能承担病例")
            event = self._append("CaseAssigned", {
                "case_id": case_id,
                "roster_id": roster_id,
                "staff_id": roster["staff_id"],
                "patient_ref": patient_ref,
                "summary_ref": summary_ref,
            }, actor)
        return {"case_id": case_id, "staff_id": roster["staff_id"],
                "seq": event["seq"]}

    def sign_case(self, case_id, actor, signed_on=None):
        signed_on = _date(signed_on or self._today(), "signed_on")
        with self._lock:
            world = self._world()
            case = world.cases.get(case_id)
            if not case:
                raise NotFoundError(f"病例不存在：{case_id}")
            if actor != case["staff_id"]:
                raise AuthorizationError("只能签署自己负责的病例")
            if case["status"] == "signed":
                raise ConflictError("病例已签署")
            roster = self._roster(world, case["roster_id"])
            rotator = actor == roster["staff_id"]
            if rotator and not self._active_windows(roster, signed_on):
                raise ConflictError("签署日期不在有效轮转时段内")
            independent = False
            if rotator:
                for code in roster["privileges"]:
                    facts = self._privilege_facts(world, roster, code, signed_on)
                    if facts["independent"]:
                        independent = True
                        break
            else:
                # 交接后接替的本院人员不属于轮转权责链条，按其本院岗位独立签署
                independent = True
            event = self._append("CaseSigned", {
                "case_id": case_id,
                "staff_id": actor,
                "signed_on": signed_on,
                "independent": independent,
            }, actor)
        return {"case_id": case_id, "signed": True,
                "independent": independent, "seq": event["seq"]}

    def open_followup(self, case_id, due_date, actor):
        _date(due_date, "due_date")
        with self._lock:
            world = self._world()
            case = world.cases.get(case_id)
            if not case:
                raise NotFoundError(f"病例不存在：{case_id}")
            if actor not in (case["staff_id"],
                             world.rosters[case["roster_id"]]["receiving_admin"]):
                raise AuthorizationError("只有责任人或管理方可登记随访")
            if case["followup"] and case["followup"]["status"] == "open":
                raise ConflictError("已存在未完成随访")
            event = self._append("FollowupOpened", {
                "case_id": case_id, "due_date": due_date,
            }, actor)
        return {"case_id": case_id, "due_date": due_date, "seq": event["seq"]}

    def complete_followup(self, case_id, actor):
        with self._lock:
            world = self._world()
            case = world.cases.get(case_id)
            if not case:
                raise NotFoundError(f"病例不存在：{case_id}")
            if actor != case["staff_id"]:
                raise AuthorizationError("只有随访责任人可完成随访")
            if not case["followup"] or case["followup"]["status"] != "open":
                raise ConflictError("没有待完成随访")
            if actor != case["followup"]["owner_id"]:
                raise AuthorizationError("只有随访责任人可完成随访")
            event = self._append("FollowupDone", {"case_id": case_id}, actor)
        return {"case_id": case_id, "followup_done": True, "seq": event["seq"]}

    # ===== 结业证明：只依据已签署且未撤销的考核事实 ========================
    def issue_certificate(self, roster_id, actor, certificate_no=None,
                          issued_on=None):
        issued_on = _date(issued_on or self._today(), "issued_on")
        with self._lock:
            world = self._world()
            roster = self._roster(world, roster_id)
            if actor != roster["receiving_admin"]:
                raise AuthorizationError("结业证明由接收机构管理方签发")
            existing = world.certificates.get(roster_id)
            if existing and not existing["voided"]:
                return {"certificate_no": existing["certificate_no"],
                        "deduplicated": True, "seq": existing["seq"]}
            if roster["status"] != "active":
                raise ConflictError("紧急返院等异常结束的轮转不能签发结业证明")
            final_end = max(w["end"] for w in roster["windows"])
            if issued_on < final_end:
                raise ConflictError("轮转有效时段尚未结束，不能提前签发")
            # 只统计仍由轮转人员本人承担、未经交接接续的债务
            unsigned = [c["case_id"] for c in world.cases.values()
                        if c["roster_id"] == roster_id
                        and c["staff_id"] == roster["staff_id"]
                        and c["status"] != "signed"]
            if unsigned:
                raise ConflictError(f"仍有未签署病历：{', '.join(unsigned)}")
            open_followups = [
                c["case_id"] for c in world.cases.values()
                if c["roster_id"] == roster_id
                and c["followup"] and c["followup"]["status"] == "open"
                and c["followup"]["owner_id"] == roster["staff_id"]
            ]
            if open_followups:
                raise ConflictError(f"仍有未完成随访：{', '.join(open_followups)}")
            open_handover = next((h for h in world.handovers
                                  if h["roster_id"] == roster_id
                                  and h["status"] == "open"), None)
            if open_handover:
                raise ConflictError("交接清单未逐项确认完成，不能签发")
            assessments = [
                a for a in world.assessments.values()
                if a["roster_id"] == roster_id and not a["revoked"]
            ]
            if not assessments:
                raise ConflictError("没有已签署且未撤销的考核事实，不能签发")
            certificate_no = certificate_no or f"CERT-{roster_id}"
            event = self._append("CertificateIssued", {
                "certificate_no": certificate_no,
                "roster_id": roster_id,
                "staff_id": roster["staff_id"],
                "assessment_ids": [a["assessment_id"] for a in assessments],
                "issued_on": issued_on,
            }, actor)
        return {"certificate_no": certificate_no, "deduplicated": False,
                "seq": event["seq"]}

    # ===== 查询：能力、病例最小可见、历史还原 ==============================
    def effective_capability(self, staff_id, on_date=None, at_seq=None):
        on_date = _date(on_date or self._today(), "on_date")
        world = self._world(at_seq)
        result = []
        for roster in world.rosters.values():
            if roster["staff_id"] != staff_id:
                continue
            for code in roster["privileges"]:
                facts = self._privilege_facts(world, roster, code, on_date)
                if not facts["credential_valid"]:
                    continue  # 证照失效：窗口内也不产生任何能力行
                for window in facts["windows"]:
                    result.append({
                        "roster_id": roster["roster_id"],
                        "practice_site": roster["practice_site"],
                        "mentor_id": roster["mentor_id"],
                        "code": code,
                        "credential_valid": True,
                        "supervised": facts["supervised"],
                        "independent": facts["independent"],
                        "window": window,
                        "receipt_seq": facts["receipt"]["seq"]
                        if facts["receipt"] else None,
                        "assessment_id": facts["assessment"]["assessment_id"]
                        if facts["assessment"] else None,
                    })
        return {"staff_id": staff_id, "on_date": on_date, "at_seq": world.seq,
                "privileges": result}

    def my_cases(self, staff_id, at_seq=None):
        """轮转人员只能看到自己负责病例的摘要指引，看不到他人病例。"""
        world = self._world(at_seq)
        cases = [{
            "case_id": c["case_id"],
            "patient_ref": c["patient_ref"],
            "summary_ref": c["summary_ref"],
            "status": c["status"],
            "followup": c["followup"]["status"] if c["followup"] else None,
            "role": ("followup-owner"
                     if c["staff_id"] != staff_id
                     and c["followup"]
                     and c["followup"]["owner_id"] == staff_id
                     else "owner"),
        } for c in world.cases.values()
            if c["staff_id"] == staff_id
            or (c["followup"] and c["followup"]["owner_id"] == staff_id)]
        return {"staff_id": staff_id, "cases": cases}

    def responsibility_trace(self, at_seq, on_date=None):
        """还原第 at_seq 个操作发生后：谁可独立执行、谁承担监督、交接是否完成。"""
        if at_seq is None:
            at_seq = self.store.seq
        world = self._world(at_seq)
        today = _date(on_date or self._today(), "on_date")
        rosters = []
        for roster in world.rosters.values():
            privileges = [
                self._privilege_facts(world, roster, code, today)
                for code in roster["privileges"]
            ]
            independent = [
                {"code": p["code"], "assessment_id": p["assessment"]["assessment_id"],
                 "window_site": p["windows"][0]["site"] if p["windows"] else None}
                for p in privileges if p["independent"]
            ]
            supervised = [
                {"code": p["code"],
                 "blocked_reason": self._blocked_reason(p)}
                for p in privileges
                if p["supervised"] and not p["independent"]
            ]
            blocked = [
                {"code": p["code"],
                 "blocked_reason": self._blocked_reason(p)}
                for p in privileges
                if p["windows"] and not p["supervised"]
            ]
            handovers = [{
                "handover_id": h["handover_id"],
                "reason": h["reason"],
                "status": h["status"],
                "confirmed": sum(1 for i in h["items"] if i["status"] == "confirmed"),
                "total": len(h["items"]),
            } for h in world.handovers if h["roster_id"] == roster["roster_id"]]
            rosters.append({
                "roster_id": roster["roster_id"],
                "staff_id": roster["staff_id"],
                "roster_status": roster["status"],
                "mentor_id": roster["mentor_id"],
                "mentor_on_leave": roster["mentor_on_leave"],
                "active_windows": [
                    {"start": w["start"], "end": w["end"], "kind": w["kind"]}
                    for w in roster["windows"] if w["status"] == "locked"
                    and w["start"] <= today <= w["end"]
                ],
                "independent": independent,
                "supervised_only": supervised,
                "blocked": blocked,
                "handovers": handovers,
            })
        return {"at_seq": world.seq, "as_of": today, "rosters": rosters}

    @staticmethod
    def _blocked_reason(facts):
        if not facts["credential_valid"]:
            return "证照无效或到期"
        if not facts["receipt"]:
            return "缺少监管回执"
        if not facts["assessment"]:
            return "缺少已签署考核"
        return None

    def handover_detail(self, handover_id):
        world = self._world()
        handover = next((h for h in world.handovers
                         if h["handover_id"] == handover_id), None)
        if not handover:
            raise NotFoundError(f"交接清单不存在：{handover_id}")
        return handover

    def _agreement_view(self, world, roster_id, seq=None):
        roster = world.rosters[roster_id]
        return {
            "roster_id": roster["roster_id"],
            "agreement_no": roster["agreement_no"],
            "staff_id": roster["staff_id"],
            "sending_org": roster["sending_org"],
            "receiving_org": roster["receiving_org"],
            "practice_site": roster["practice_site"],
            "mentor_id": roster["mentor_id"],
            "privileges": roster["privileges"],
            "learning_goals": roster["learning_goals"],
            "duties": roster["duties"],
            "windows": roster["windows"],
            "amendments": roster["amendments"],
            "status": roster["status"],
            "seq": seq or world.seq,
        }

    def get_agreement(self, roster_id):
        world = self._world()
        self._roster(world, roster_id)
        return self._agreement_view(world, roster_id)

    def event_log(self, after_seq=0):
        return self.store.read(after_seq)
