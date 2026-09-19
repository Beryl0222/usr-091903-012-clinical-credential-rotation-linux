"""轮转履约领域服务。

在不引入第三方依赖的前提下实现区域医联体轮转履约的全部业务规则：

* 出发前锁定派出协议（执业地点、专业权限、学习目标、带教人、有效时段）；
* 临时支援 / 延长轮转必须经派出方与接收方双方批准后才产生新版本职责；
* 同一人员在两院同时承担互斥任务时拒绝分派；
* 监管回执、考核上传按自然键与幂等键双重去重，授予只发生一次；
* 带教人休假、证照到期、紧急返院触发交接清单，未完成事项逐项由新责任人确认；
* 轮转人员只能查看本人负责的病例摘要，管理者可查看全部；
* 全部操作留痕，可还原任一时点谁能独立执行、谁承担监督、交接是否完成；
* 结业证明只能依据已签署且未被撤销的考核事实生成，且可被实时核验。

时间统一使用 ISO-8601 字符串（``YYYY-MM-DDTHH:MM:SS``），字典序即时间序；
证照到期日使用 ``YYYY-MM-DD``。所有写操作接受显式 ``at`` 参数以便测试回放。
"""

from datetime import datetime, timezone
from threading import RLock

# ---- 角色与常量 -----------------------------------------------------------

ROLE_STAFF = "staff"        # 轮转人员 / 带教人（临床人员）
ROLE_MANAGER = "manager"    # 护理部、医务处等管理者
ROLE_AUTHORITY = "authority"  # 监管机构

PHYSICIAN = "physician"
NURSE = "nurse"

SIDE_SENDING = "sending"
SIDE_RECEIVING = "receiving"
SIDES = (SIDE_SENDING, SIDE_RECEIVING)

CHANGE_INITIAL = "initial"
CHANGE_SUPPORT = "support"        # 临时支援
CHANGE_EXTENSION = "extension"    # 延长轮转

REASON_PRECEPTOR_LEAVE = "preceptor_leave"    # 带教人休假
REASON_LICENSE_EXPIRY = "license_expiry"      # 证照到期
REASON_EMERGENCY_RECALL = "emergency_recall"  # 紧急返院
HANDOVER_REASONS = (
    REASON_PRECEPTOR_LEAVE,
    REASON_LICENSE_EXPIRY,
    REASON_EMERGENCY_RECALL,
)

ITEM_RECORD = "record"          # 未签署病历
ITEM_FOLLOWUP = "followup"      # 随访责任
ITEM_SUPERVISION = "supervision"  # 监督（带教）关系

ITEM_PENDING = "pending"
ITEM_CONFIRMED = "confirmed"

# ---- 异常 -----------------------------------------------------------------


class ServiceError(Exception):
    """业务异常基类，status 为对应的 HTTP 状态码。"""

    status = 400
    code = "validation_error"

    def __init__(self, message, *, code=None, details=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(ServiceError):
    status = 400
    code = "validation_error"


class NotFoundError(ServiceError):
    status = 404
    code = "not_found"


class PermissionError(ServiceError):  # noqa: A001 - 业务语义清晰
    status = 403
    code = "forbidden"


class ConflictError(ServiceError):
    status = 409
    code = "conflict"


# ---- 工具函数 -------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _require(payload, field):
    if not isinstance(payload, dict) or field not in payload:
        raise ValidationError(f"缺少必填字段: {field}")
    return payload[field]


def _overlap(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


class RotationService:
    """轮转履约服务，内存存储，单例可直接挂到 HTTP 层。"""

    def __init__(self, now_fn=_now):
        self._lock = RLock()
        self._now_fn = now_fn
        self.actors = {}        # actor_id -> {id, name, role, org_id}
        self.staff = {}         # staff_id -> {id, name, profession, licenses:[...]}
        self.orgs = {}          # org_id -> {id, name}
        self.plans = {}         # plan_id -> plan
        self.amendments = {}    # amendment_id -> amendment
        self.tasks = {}         # task_id -> task
        self.receipts = {}      # receipt key -> receipt
        self.assessments = {}   # assessment_id -> assessment
        self.records = {}       # record_id -> pending/open 记录
        self.followups = {}     # followup_id -> 随访
        self.handovers = {}     # checklist_id -> checklist
        self.cases = {}         # case_id -> 病例摘要
        self.certificates = {}  # cert_id -> 证书
        self.audit_log = []
        self._seq = 0
        self._idem = {}         # Idempotency-Key -> 首次结果

    # ---- 基础：时间、审计、身份、幂等 -------------------------------------

    def _at(self, at):
        return at or self._now_fn()

    def _audit(self, action, entity_type, entity_id, actor, detail=None, result="ok"):
        self._seq += 1
        self.audit_log.append(
            {
                "seq": self._seq,
                "at": self._now_fn(),
                "actor": actor,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "detail": detail or {},
                "result": result,
            }
        )

    def _actor(self, actor_id):
        actor = self.actors.get(actor_id)
        if not actor:
            raise PermissionError("未知调用方，请先注册身份", code="unknown_actor")
        return actor

    def _require_role(self, actor_id, roles):
        actor = self._actor(actor_id)
        if actor["role"] not in roles:
            raise PermissionError(
                f"角色 {actor['role']} 无权执行该操作",
                details={"required_roles": list(roles)},
            )
        return actor

    def _get_staff(self, staff_id):
        person = self.staff.get(staff_id)
        if not person:
            raise NotFoundError(f"人员不存在: {staff_id}", code="staff_not_found")
        return person

    def _get_plan(self, plan_id):
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFoundError(f"轮转计划不存在: {plan_id}", code="plan_not_found")
        return plan

    def with_idempotency(self, key, signature, operation):
        """按幂等键包裹写操作；同键重试直接返回首次结果，绝不重复执行。"""
        if not key:
            return operation()
        existing = self._idem.get(key)
        if existing:
            if existing["signature"] != signature:
                raise ConflictError(
                    "同一幂等键携带了不同的请求内容", code="idempotency_key_reuse"
                )
            return existing["response"]
        response = operation()
        self._idem[key] = {"signature": signature, "response": response}
        return response

    # ---- 主数据：机构、人员、管理者 ---------------------------------------

    def register_org(self, payload, actor_id, at=None):
        at = self._at(at)
        org_id = _require(payload, "id")
        name = _require(payload, "name")
        with self._lock:
            if org_id in self.orgs:
                raise ConflictError(f"机构已存在: {org_id}", code="org_exists")
            self.orgs[org_id] = {"id": org_id, "name": name}
            self._audit("register_org", "org", org_id, actor_id, {"name": name})
            return self.orgs[org_id]

    def register_staff(self, payload, actor_id=None, at=None):
        """注册临床人员，同时建立其轮转人员身份。"""
        at = self._at(at)
        staff_id = _require(payload, "id")
        name = _require(payload, "name")
        profession = payload.get("profession", PHYSICIAN)
        if profession not in (PHYSICIAN, NURSE):
            raise ValidationError("profession 必须为 physician 或 nurse")
        licenses = payload.get("licenses", [])
        self._validate_licenses(licenses)
        with self._lock:
            if staff_id in self.staff:
                raise ConflictError(f"人员已存在: {staff_id}", code="staff_exists")
            person = {
                "id": staff_id,
                "name": name,
                "profession": profession,
                "licenses": licenses,
            }
            self.staff[staff_id] = person
            self.actors[staff_id] = {
                "id": staff_id,
                "name": name,
                "role": ROLE_STAFF,
                "org_id": payload.get("org_id"),
            }
            self._audit("register_staff", "staff", staff_id, actor_id, {"name": name})
            return person

    def register_actor(self, payload, actor_id=None, at=None):
        """注册管理者（护理部/医务处）或监管机构身份。"""
        at = self._at(at)
        identity = _require(payload, "id")
        name = _require(payload, "name")
        role = _require(payload, "role")
        if role not in (ROLE_MANAGER, ROLE_AUTHORITY):
            raise ValidationError("role 必须为 manager 或 authority")
        org_id = payload.get("org_id")
        with self._lock:
            if identity in self.actors:
                raise ConflictError(f"身份已存在: {identity}", code="actor_exists")
            if org_id and org_id not in self.orgs:
                raise ValidationError(f"机构不存在: {org_id}")
            self.actors[identity] = {
                "id": identity,
                "name": name,
                "role": role,
                "org_id": org_id,
            }
            self._audit("register_actor", "actor", identity, actor_id, {"role": role})
            return self.actors[identity]

    @staticmethod
    def _validate_licenses(licenses):
        if not isinstance(licenses, list) or not licenses:
            raise ValidationError("至少需要一条执业证照")
        for lic in licenses:
            for field in ("number", "scopes", "registered_sites", "expires_on"):
                if field not in lic:
                    raise ValidationError(f"证照缺少字段: {field}")
            if not isinstance(lic["scopes"], list) or not lic["scopes"]:
                raise ValidationError("证照注册范围不能为空")
            if not isinstance(lic["registered_sites"], list):
                raise ValidationError("执业地点备案必须为列表")

    def _active_license(self, person, at, site_id, required_scope):
        """返回在 at 时点、于指定站点、覆盖指定范围的有效证照；否则返回 None。"""
        day = at[:10]
        for lic in person["licenses"]:
            if lic["expires_on"] < day:
                continue
            if site_id is not None and site_id not in lic["registered_sites"]:
                continue
            if required_scope is not None and required_scope not in lic["scopes"]:
                continue
            return lic
        return None

    # ---- 派出协议：创建即锁定，变更需双方批准 -----------------------------

    def create_plan(self, payload, actor_id, at=None):
        """出发前锁定派出协议；注册范围/地点/证照不符在此刻即拒绝。"""
        at = self._at(at)
        plan_id = _require(payload, "plan_id")
        staff_id = _require(payload, "staff_id")
        sending_org = _require(payload, "sending_org")
        receiving_org = _require(payload, "receiving_org")
        valid_from = _require(payload, "valid_from")
        valid_to = _require(payload, "valid_to")
        preceptor_id = _require(payload, "preceptor_id")
        privileges = payload.get("privileges", [])
        objectives = payload.get("learning_objectives", [])
        if not valid_from < valid_to:
            raise ValidationError("有效时段要求 valid_from 早于 valid_to")
        with self._lock:
            actor = self._require_role(actor_id, (ROLE_MANAGER,))
            if plan_id in self.plans:
                raise ConflictError(f"轮转计划已存在: {plan_id}", code="plan_exists")
            person = self._get_staff(staff_id)
            if sending_org not in self.orgs or receiving_org not in self.orgs:
                raise ValidationError("派出方或接收方机构不存在")
            if sending_org == receiving_org:
                raise ValidationError("派出方与接收方不能为同一机构")
            self._get_staff(preceptor_id)
            self._check_privileges_against_license(
                person, receiving_org, privileges, valid_to, at
            )
            plan = {
                "id": plan_id,
                "staff_id": staff_id,
                "sending_org": sending_org,
                "receiving_org": receiving_org,
                "locked_at": at,
                "locked_by": actor_id,
                "versions": {},
            }
            snapshot = self._snapshot(
                1, CHANGE_INITIAL, valid_from, valid_to, receiving_org,
                preceptor_id, privileges, objectives,
                approvals=[
                    {"side": SIDE_SENDING, "by": actor_id, "at": at},
                    {"side": SIDE_RECEIVING, "by": actor_id, "at": at},
                ],
            )
            plan["versions"][1] = snapshot
            self.plans[plan_id] = plan
            self._audit(
                "lock_plan", "plan", plan_id, actor_id,
                {"staff_id": staff_id, "version": 1,
                 "sending_org": sending_org, "receiving_org": receiving_org},
            )
            return {"plan_id": plan_id, "version": snapshot}

    def _check_privileges_against_license(
        self, person, site_id, privileges, valid_to, at
    ):
        """出发前/变更生效前核对注册范围、执业地点与证照有效期。"""
        mismatches = []
        for priv in privileges:
            code = priv.get("code")
            if not code:
                raise ValidationError("专业权限缺少 code")
            scope = priv.get("required_scope")
            lic = self._active_license(person, at, site_id, scope)
            if lic is None:
                # 进一步区分不符原因，便于出发前纠正。
                day = at[:10]
                reason = "scope_mismatch"
                candidate = next(
                    (l for l in person["licenses"] if l["expires_on"] >= day), None
                )
                if candidate is None:
                    reason = "license_expired"
                elif site_id not in candidate["registered_sites"]:
                    reason = "site_not_registered"
                mismatches.append({"privilege": code, "reason": reason})
            elif lic["expires_on"] < valid_to[:10]:
                mismatches.append(
                    {"privilege": code, "reason": "license_expires_before_window_end",
                     "expires_on": lic["expires_on"], "valid_to": valid_to[:10]}
                )
        if mismatches:
            raise ValidationError(
                "注册范围、执业地点或证照有效期与派出计划不符",
                code="credential_mismatch",
                details={"mismatches": mismatches},
            )

    @staticmethod
    def _snapshot(version, change_type, valid_from, valid_to, practice_site,
                  preceptor_id, privileges, objectives, approvals=None):
        return {
            "version": version,
            "change_type": change_type,
            "status": "approved",
            "valid_from": valid_from,
            "valid_to": valid_to,
            "practice_site": practice_site,
            "preceptor_id": preceptor_id,
            # 权限清单是锁定快照，之后只能通过新版本修改。
            "privileges": [dict(p) for p in privileges],
            "learning_objectives": list(objectives),
            "approvals": approvals or [],
        }

    def propose_amendment(self, payload, actor_id, at=None):
        """临时支援或延长轮转：提出后职责不变，须双方批准。"""
        at = self._at(at)
        plan_id = _require(payload, "plan_id")
        change_type = _require(payload, "change_type")
        if change_type not in (CHANGE_SUPPORT, CHANGE_EXTENSION):
            raise ValidationError("change_type 必须为 support 或 extension")
        valid_from = _require(payload, "valid_from")
        valid_to = _require(payload, "valid_to")
        if not valid_from < valid_to:
            raise ValidationError("有效时段要求 valid_from 早于 valid_to")
        with self._lock:
            actor = self._require_role(actor_id, (ROLE_MANAGER,))
            plan = self._get_plan(plan_id)
            latest = self._latest_version(plan)
            if change_type == CHANGE_EXTENSION and valid_to <= latest["valid_to"]:
                raise ValidationError(
                    "延长轮转的新结束时间必须晚于当前已批准的结束时间",
                    code="invalid_extension",
                )
            if valid_from < latest["valid_from"]:
                raise ValidationError("变更生效时间不得早于计划起始时间")
            practice_site = payload.get("practice_site", latest["practice_site"])
            if practice_site not in (plan["receiving_org"], plan["sending_org"]):
                raise ValidationError("临时支援的执业地点只能是派出或接收机构之一")
            preceptor_id = payload.get("preceptor_id", latest["preceptor_id"])
            self._get_staff(preceptor_id)
            privileges = payload.get("privileges", latest["privileges"])
            person = self._get_staff(plan["staff_id"])
            self._check_privileges_against_license(
                person, practice_site, privileges, valid_to, at
            )
            amendment_id = (
                f"amend-{plan_id}-{len([a for a in self.amendments.values()
                                         if a['plan_id'] == plan_id]) + 2}"
            )
            amendment = {
                "id": amendment_id,
                "plan_id": plan_id,
                "proposed_version": latest["version"] + 1,
                "change_type": change_type,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "practice_site": practice_site,
                "preceptor_id": preceptor_id,
                "privileges": [dict(p) for p in privileges],
                "learning_objectives": list(
                    payload.get("learning_objectives", latest["learning_objectives"])
                ),
                "status": "proposed",
                "approvals": {},
                "proposed_by": actor_id,
                "proposed_at": at,
            }
            self.amendments[amendment_id] = amendment
            self._audit(
                "propose_amendment", "amendment", amendment_id, actor_id,
                {"plan_id": plan_id, "change_type": change_type,
                 "valid_from": valid_from, "valid_to": valid_to},
            )
            return self._amendment_view(amendment)

    def approve_amendment(self, amendment_id, payload, actor_id, at=None):
        """派出方与接收方分别批准；第二方批准时职责快照才生效。"""
        at = self._at(at)
        side = _require(payload, "side")
        if side not in SIDES:
            raise ValidationError("side 必须为 sending 或 receiving")
        with self._lock:
            actor = self._require_role(actor_id, (ROLE_MANAGER,))
            amendment = self.amendments.get(amendment_id)
            if not amendment:
                raise NotFoundError(
                    f"变更申请不存在: {amendment_id}", code="amendment_not_found"
                )
            if amendment["status"] != "proposed":
                raise ConflictError(
                    "该变更申请已完成审批", code="amendment_closed"
                )
            plan = self._get_plan(amendment["plan_id"])
            expected_org = plan[f"{side}_org"]
            if actor.get("org_id") != expected_org:
                raise PermissionError(
                    f"只有{side}机构的管理者可以代表该方批准",
                    details={"expected_org": expected_org,
                             "actor_org": actor.get("org_id")},
                )
            if side in amendment["approvals"]:
                raise ConflictError(f"{side} 方已批准", code="already_approved")
            amendment["approvals"][side] = {"by": actor_id, "at": at}
            self._audit(
                "approve_amendment", "amendment", amendment_id, actor_id,
                {"plan_id": plan["id"], "side": side},
            )
            if all(s in amendment["approvals"] for s in SIDES):
                snapshot = self._snapshot(
                    amendment["proposed_version"],
                    amendment["change_type"],
                    amendment["valid_from"],
                    amendment["valid_to"],
                    amendment["practice_site"],
                    amendment["preceptor_id"],
                    amendment["privileges"],
                    amendment["learning_objectives"],
                    approvals=[
                        {"side": s, **amendment["approvals"][s]} for s in SIDES
                    ],
                )
                plan["versions"][snapshot["version"]] = snapshot
                amendment["status"] = "approved"
                self._audit(
                    "amendment_effective", "plan", plan["id"], actor_id,
                    {"amendment_id": amendment_id, "version": snapshot["version"]},
                )
            return self._amendment_view(amendment)

    @staticmethod
    def _amendment_view(amendment):
        return {
            "id": amendment["id"],
            "plan_id": amendment["plan_id"],
            "proposed_version": amendment["proposed_version"],
            "change_type": amendment["change_type"],
            "valid_from": amendment["valid_from"],
            "valid_to": amendment["valid_to"],
            "practice_site": amendment["practice_site"],
            "preceptor_id": amendment["preceptor_id"],
            "privileges": amendment["privileges"],
            "learning_objectives": amendment["learning_objectives"],
            "status": amendment["status"],
            "approvals": amendment["approvals"],
            "proposed_by": amendment["proposed_by"],
            "proposed_at": amendment["proposed_at"],
        }

    @staticmethod
    def _latest_version(plan):
        return plan["versions"][max(plan["versions"])]

    def _version_at(self, plan, at):
        """返回 at 时点有效的最高版本快照；区间外返回 (None, latest)。"""
        latest = self._latest_version(plan)
        effective = None
        for snapshot in plan["versions"].values():
            if snapshot["valid_from"] <= at <= snapshot["valid_to"]:
                if effective is None or snapshot["version"] > effective["version"]:
                    effective = snapshot
        return effective, latest

    def get_plan(self, plan_id, actor_id, at=None):
        at = self._at(at)
        with self._lock:
            self._actor(actor_id)
            plan = self._get_plan(plan_id)
            effective, _ = self._version_at(plan, at)
            return {
                "plan_id": plan_id,
                "staff_id": plan["staff_id"],
                "sending_org": plan["sending_org"],
                "receiving_org": plan["receiving_org"],
                "locked_at": plan["locked_at"],
                "current_version_at": at,
                "effective_version": effective["version"] if effective else None,
                "versions": list(plan["versions"].values()),
            }

    # ---- 互斥任务分派 ------------------------------------------------------

    def assign_task(self, payload, actor_id, at=None):
        """两院互斥任务在此拒绝；同院并行允许。"""
        at = self._at(at)
        task_id = _require(payload, "task_id")
        staff_id = _require(payload, "staff_id")
        site_id = _require(payload, "site_id")
        start = _require(payload, "start")
        end = _require(payload, "end")
        exclusive = payload.get("exclusive", True)
        if not start < end:
            raise ValidationError("任务时段要求 start 早于 end")
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            self._get_staff(staff_id)
            if site_id not in self.orgs:
                raise ValidationError(f"任务地点机构不存在: {site_id}")
            if task_id in self.tasks:
                raise ConflictError(f"任务已存在: {task_id}", code="task_exists")
            for existing in self.tasks.values():
                if existing["cancelled"]:
                    continue
                if existing["staff_id"] != staff_id:
                    continue
                if not _overlap(start, end, existing["start"], existing["end"]):
                    continue
                if existing["site_id"] == site_id:
                    continue  # 同院并行允许
                if exclusive and existing["exclusive"]:
                    raise ConflictError(
                        "该人员在重叠时段已在另一机构承担互斥任务，必须拒绝",
                        code="mutual_exclusion",
                        details={
                            "conflicting_task_id": existing["id"],
                            "other_site": existing["site_id"],
                            "other_window": [existing["start"], existing["end"]],
                        },
                    )
            task = {
                "id": task_id,
                "staff_id": staff_id,
                "site_id": site_id,
                "title": payload.get("title", ""),
                "start": start,
                "end": end,
                "exclusive": exclusive,
                "cancelled": False,
                "assigned_by": actor_id,
                "assigned_at": at,
            }
            self.tasks[task_id] = task
            self._audit("assign_task", "task", task_id, actor_id,
                        {"staff_id": staff_id, "site_id": site_id,
                         "window": [start, end]})
            return dict(task)

    def cancel_task(self, task_id, actor_id, at=None):
        at = self._at(at)
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            task = self.tasks.get(task_id)
            if not task:
                raise NotFoundError(f"任务不存在: {task_id}", code="task_not_found")
            task["cancelled"] = True
            task["cancelled_by"] = actor_id
            task["cancelled_at"] = at
            self._audit("cancel_task", "task", task_id, actor_id, {})
            return dict(task)

    # ---- 监管回执（幂等，不重复授予能力） ----------------------------------

    def submit_receipt(self, payload, actor_id, at=None, idempotency_key=None):
        at = self._at(at)
        staff_id = _require(payload, "staff_id")
        receipt_code = _require(payload, "receipt_code")
        privilege_codes = _require(payload, "privilege_codes")
        if not isinstance(privilege_codes, list) or not privilege_codes:
            raise ValidationError("回执至少关联一个专业权限")
        payload["confirmed"] = payload.get("confirmed", True)

        def operation():
            with self._lock:
                self._require_role(actor_id, (ROLE_AUTHORITY, ROLE_MANAGER))
                person = self._get_staff(staff_id)
                key = f"{staff_id}:{receipt_code}"
                duplicate = key in self.receipts
                receipt = self.receipts.get(key)
                if duplicate:
                    # 自然键重试：原样返回，不再次授予。
                    self._audit("submit_receipt_retry", "receipt", key, actor_id,
                                {"staff_id": staff_id}, result="duplicate")
                    return {"receipt": dict(receipt), "duplicate": True,
                            "grants": []}
                receipt = {
                    "key": key,
                    "staff_id": staff_id,
                    "receipt_code": receipt_code,
                    "authority": actor_id,
                    "privilege_codes": list(privilege_codes),
                    "confirmed": bool(payload["confirmed"]),
                    "confirmed_at": at if payload["confirmed"] else None,
                }
                self.receipts[key] = receipt
                grants = []
                if receipt["confirmed"]:
                    for code in privilege_codes:
                        grants.append(
                            self._grant(person, code, via_type="receipt",
                                        via_id=key, at=at)
                        )
                self._audit("submit_receipt", "receipt", key, actor_id,
                            {"staff_id": staff_id, "privilege_codes":
                             privilege_codes, "granted": [g["code"] for g in grants
                                                          if g["changed"]]})
                return {"receipt": receipt, "duplicate": False, "grants": grants}

        with self._lock:
            return self.with_idempotency(
                idempotency_key,
                ("receipt", staff_id, receipt_code, tuple(privilege_codes),
                 payload["confirmed"]),
                operation,
            )

    # ---- 考核上传、签署、撤销（幂等授予） ----------------------------------

    def upload_assessment(self, payload, actor_id, at=None, idempotency_key=None):
        at = self._at(at)
        assessment_id = _require(payload, "assessment_id")
        plan_id = _require(payload, "plan_id")
        privilege_code = _require(payload, "privilege_code")
        title = payload.get("title", privilege_code)
        result_pass = payload.get("result", "pass")
        if result_pass not in ("pass", "fail"):
            raise ValidationError("result 必须为 pass 或 fail")

        def operation():
            with self._lock:
                self._require_role(
                    actor_id, (ROLE_MANAGER, ROLE_STAFF, ROLE_AUTHORITY)
                )
                plan = self._get_plan(plan_id)
                person = self._get_staff(plan["staff_id"])
                if assessment_id in self.assessments:
                    self._audit("upload_assessment_retry", "assessment",
                                assessment_id, actor_id,
                                {"plan_id": plan_id}, result="duplicate")
                    existing = self.assessments[assessment_id]
                    return {"assessment": dict(existing), "duplicate": True,
                            "grants": []}
                assessment = {
                    "id": assessment_id,
                    "plan_id": plan_id,
                    "staff_id": plan["staff_id"],
                    "privilege_code": privilege_code,
                    "title": title,
                    "result": result_pass,
                    "uploaded_by": actor_id,
                    "uploaded_at": at,
                    "signed_at": None,
                    "signed_by": None,
                    "revoked_at": None,
                    "revoked_by": None,
                    "revoke_reason": None,
                }
                self.assessments[assessment_id] = assessment
                grants = []
                # 考核通过即授予能力；签署是事实确认，用于结业证明。
                if result_pass == "pass":
                    grants.append(
                        self._grant(person, privilege_code, via_type="assessment",
                                    via_id=assessment_id, at=at, plan_id=plan_id)
                    )
                self._audit("upload_assessment", "assessment", assessment_id,
                            actor_id, {"plan_id": plan_id,
                                       "privilege_code": privilege_code,
                                       "granted": [g["code"] for g in grants
                                                   if g["changed"]]})
                return {"assessment": dict(assessment), "duplicate": False,
                        "grants": grants}

        with self._lock:
            return self.with_idempotency(
                idempotency_key,
                ("assessment", assessment_id, plan_id, privilege_code, result_pass),
                operation,
            )

    def sign_assessment(self, assessment_id, actor_id, at=None):
        at = self._at(at)
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            assessment = self.assessments.get(assessment_id)
            if not assessment:
                raise NotFoundError(
                    f"考核不存在: {assessment_id}", code="assessment_not_found"
                )
            if assessment["revoked_at"]:
                raise ConflictError("考核已被撤销，不能签署",
                                    code="assessment_revoked")
            if assessment["signed_at"]:
                raise ConflictError("考核已签署", code="already_signed")
            assessment["signed_at"] = at
            assessment["signed_by"] = actor_id
            self._audit("sign_assessment", "assessment", assessment_id, actor_id,
                        {"plan_id": assessment["plan_id"]})
            return dict(assessment)

    def revoke_assessment(self, assessment_id, payload, actor_id, at=None):
        """撤销考核事实：挂回其授予的能力，并使依据它的证书失效。"""
        at = self._at(at)
        reason = _require(payload, "reason")
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER,))
            assessment = self.assessments.get(assessment_id)
            if not assessment:
                raise NotFoundError(
                    f"考核不存在: {assessment_id}", code="assessment_not_found"
                )
            if assessment["revoked_at"]:
                raise ConflictError("考核已被撤销", code="already_revoked")
            assessment["revoked_at"] = at
            assessment["revoked_by"] = actor_id
            assessment["revoke_reason"] = reason
            suspended = self._suspend_grant(
                via_type="assessment", via_id=assessment_id, at=at, reason=reason
            )
            self._audit("revoke_assessment", "assessment", assessment_id, actor_id,
                        {"reason": reason, "suspended": suspended})
            return {"assessment": dict(assessment), "suspended_grants": suspended}

    # ---- 能力授予台账（来源级，授予只发生一次） ----------------------------

    def _grant(self, person, code, *, via_type, via_id, at, plan_id=None):
        """尝试授予能力。已存在有效来源时 changed=False，绝不重复授予。"""
        existing = self._find_grant(person["id"], code, via_type, via_id)
        if existing:
            return {"code": code, "via": via_type, "via_id": via_id,
                    "changed": False, "status": existing["status"]}
        grant = {
            "staff_id": person["id"],
            "code": code,
            "plan_id": plan_id,
            "via_type": via_type,
            "via_id": via_id,
            "status": "active",
            "granted_at": at,
            "suspended_at": None,
            "suspend_reason": None,
        }
        person.setdefault("grants", []).append(grant)
        self._audit("privilege_granted", "grant", f"{person['id']}:{code}",
                    via_id, {"staff_id": person["id"], "code": code, "via": via_type})
        return {"code": code, "via": via_type, "via_id": via_id,
                "changed": True, "status": "active"}

    def _find_grant(self, staff_id, code, via_type, via_id):
        for grant in self.staff[staff_id].get("grants", []):
            if (grant["code"] == code and grant["via_type"] == via_type
                    and grant["via_id"] == via_id):
                return grant
        return None

    def _suspend_grant(self, *, via_type, via_id, at, reason):
        suspended = []
        for person in self.staff.values():
            for grant in person.get("grants", []):
                if (grant["via_type"] == via_type and grant["via_id"] == via_id
                        and grant["status"] == "active"):
                    grant["status"] = "suspended"
                    grant["suspended_at"] = at
                    grant["suspend_reason"] = reason
                    suspended.append({"staff_id": person["id"],
                                      "code": grant["code"]})
        return suspended

    def _grant_active_at(self, staff_id, code, via_type, at):
        for grant in self.staff[staff_id].get("grants", []):
            if grant["code"] != code or grant["via_type"] != via_type:
                continue
            if grant["granted_at"] > at:
                continue
            if grant["status"] == "active":
                return True
            if grant["status"] == "suspended" and grant["suspended_at"] <= at:
                return False
        return False

    # ---- 未签署病历与随访（交接事项来源） ----------------------------------

    def register_open_record(self, plan_id, payload, actor_id, at=None):
        """登记一份尚未签署、轮转结束仍需接续的病历。"""
        at = self._at(at)
        record_id = _require(payload, "record_id")
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            plan = self._get_plan(plan_id)
            if record_id in self.records:
                raise ConflictError(f"病历已登记: {record_id}", code="record_exists")
            record = {
                "id": record_id,
                "plan_id": plan_id,
                "staff_id": plan["staff_id"],
                "title": payload.get("title", record_id),
                "open": True,
                "registered_by": actor_id,
                "registered_at": at,
                "signed_at": None,
            }
            self.records[record_id] = record
            self._audit("register_record", "record", record_id, actor_id,
                        {"plan_id": plan_id})
            return dict(record)

    def sign_record(self, record_id, actor_id, at=None):
        at = self._at(at)
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            record = self.records.get(record_id)
            if not record:
                raise NotFoundError(f"病历不存在: {record_id}",
                                    code="record_not_found")
            record["open"] = False
            record["signed_at"] = at
            record["signed_by"] = actor_id
            self._audit("sign_record", "record", record_id, actor_id, {})
            return dict(record)

    def register_followup(self, plan_id, payload, actor_id, at=None):
        at = self._at(at)
        followup_id = _require(payload, "followup_id")
        due_at = _require(payload, "due_at")
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            plan = self._get_plan(plan_id)
            if followup_id in self.followups:
                raise ConflictError(f"随访已登记: {followup_id}",
                                    code="followup_exists")
            followup = {
                "id": followup_id,
                "plan_id": plan_id,
                "staff_id": plan["staff_id"],
                "title": payload.get("title", followup_id),
                "due_at": due_at,
                "open": True,
                "registered_by": actor_id,
                "registered_at": at,
                "completed_at": None,
            }
            self.followups[followup_id] = followup
            self._audit("register_followup", "followup", followup_id, actor_id,
                        {"plan_id": plan_id, "due_at": due_at})
            return dict(followup)

    def complete_followup(self, followup_id, actor_id, at=None):
        at = self._at(at)
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            followup = self.followups.get(followup_id)
            if not followup:
                raise NotFoundError(f"随访不存在: {followup_id}",
                                    code="followup_not_found")
            followup["open"] = False
            followup["completed_at"] = at
            followup["completed_by"] = actor_id
            self._audit("complete_followup", "followup", followup_id, actor_id, {})
            return dict(followup)

    # ---- 交接清单：触发、逐项确认 ------------------------------------------

    def trigger_handover(self, plan_id, payload, actor_id, at=None):
        """带教人休假 / 证照到期 / 紧急返院：为每件未完成事项生成清单项。"""
        at = self._at(at)
        reason = _require(payload, "reason")
        if reason not in HANDOVER_REASONS:
            raise ValidationError(
                f"reason 必须为 {', '.join(HANDOVER_REASONS)} 之一"
            )
        to_staff_id = _require(payload, "to_staff_id")
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            plan = self._get_plan(plan_id)
            successor = self._get_staff(to_staff_id)
            effective, latest = self._version_at(plan, at)
            version = effective or latest
            items = []

            def add_item(category, ref, description, from_staff):
                items.append({
                    "id": f"item-{len(items) + 1}",
                    "category": category,
                    "ref": ref,
                    "description": description,
                    "from_staff": from_staff,
                    "to_staff": to_staff_id,
                    "status": ITEM_PENDING,
                    "confirmed_by": None,
                    "confirmed_at": None,
                    "note": None,
                })

            for record in self.records.values():
                if record["plan_id"] == plan_id and record["open"]:
                    add_item(ITEM_RECORD, record["id"],
                             f"接续未签署病历 {record['id']}", plan["staff_id"])
            for followup in self.followups.values():
                if followup["plan_id"] == plan_id and followup["open"]:
                    add_item(ITEM_FOLLOWUP, followup["id"],
                             f"接续随访责任 {followup['id']}（截止 {followup['due_at']}）",
                             plan["staff_id"])
            # 带教人休假或紧急返院必然改变监督关系；证照到期不更换带教人，
            # 此时能力由执业证照校验自动阻断，交接只针对病历与随访。
            if reason in (REASON_PRECEPTOR_LEAVE, REASON_EMERGENCY_RECALL):
                add_item(ITEM_SUPERVISION, f"preceptor@{version['version']}",
                         f"接续带教监督职责（原带教人 {version['preceptor_id']}）",
                         version["preceptor_id"])

            if not items:
                raise ConflictError("当前没有需要交接的未完成事项",
                                    code="nothing_to_handover")
            checklist_id = (
                f"handover-{plan_id}-"
                f"{len([h for h in self.handovers.values() if h['plan_id'] == plan_id]) + 1}"
            )
            checklist = {
                "id": checklist_id,
                "plan_id": plan_id,
                "staff_id": plan["staff_id"],
                "reason": reason,
                "triggered_by": actor_id,
                "triggered_at": at,
                "to_staff_id": to_staff_id,
                "successor_name": successor["name"],
                "items": items,
            }
            self.handovers[checklist_id] = checklist
            self._audit("trigger_handover", "handover", checklist_id, actor_id,
                        {"plan_id": plan_id, "reason": reason,
                         "item_count": len(items), "to_staff_id": to_staff_id})
            return self._handover_view(checklist)

    def confirm_handover_item(self, checklist_id, item_id, payload, actor_id,
                              at=None):
        """新责任人逐项确认；系统不提供整体改名接口。"""
        at = self._at(at)
        note = (payload or {}).get("note")
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            checklist = self.handovers.get(checklist_id)
            if not checklist:
                raise NotFoundError(
                    f"交接清单不存在: {checklist_id}", code="handover_not_found"
                )
            item = next((i for i in checklist["items"] if i["id"] == item_id), None)
            if item is None:
                raise NotFoundError(f"交接事项不存在: {item_id}",
                                    code="handover_item_not_found")
            if item["status"] == ITEM_CONFIRMED:
                raise ConflictError("该事项已由新责任人确认",
                                    code="item_confirmed")
            if actor_id != item["to_staff"]:
                actor = self.actors[actor_id]
                if actor["role"] != ROLE_MANAGER:
                    raise PermissionError(
                        "只有该事项的新责任人才可以逐项确认接续",
                        details={"expected_to_staff": item["to_staff"]},
                    )
            item["status"] = ITEM_CONFIRMED
            item["confirmed_by"] = actor_id
            item["confirmed_at"] = at
            item["note"] = note
            supervision_changed = False
            if item["category"] == ITEM_SUPERVISION:
                supervision_changed = True
            self._audit("confirm_handover_item", "handover_item",
                        f"{checklist_id}:{item_id}", actor_id,
                        {"plan_id": checklist["plan_id"],
                         "category": item["category"], "ref": item["ref"],
                         "supervision_changed": supervision_changed})
            view = self._handover_view(checklist)
            view["just_confirmed"] = item_id
            return view

    @staticmethod
    def _handover_view(checklist):
        items = [dict(i) for i in checklist["items"]]
        confirmed = sum(1 for i in items if i["status"] == ITEM_CONFIRMED)
        return {
            "id": checklist["id"],
            "plan_id": checklist["plan_id"],
            "staff_id": checklist["staff_id"],
            "reason": checklist["reason"],
            "triggered_at": checklist["triggered_at"],
            "triggered_by": checklist["triggered_by"],
            "to_staff_id": checklist["to_staff_id"],
            "successor_name": checklist["successor_name"],
            "items": items,
            "total": len(items),
            "confirmed": confirmed,
            "completed": confirmed == len(items),
            "completed_at": (
                max(i["confirmed_at"] for i in items)
                if items and confirmed == len(items) else None
            ),
        }

    def get_handover(self, checklist_id, actor_id, at=None):
        with self._lock:
            self._actor(actor_id)
            checklist = self.handovers.get(checklist_id)
            if not checklist:
                raise NotFoundError(
                    f"交接清单不存在: {checklist_id}", code="handover_not_found"
                )
            return self._handover_view(checklist)

    # ---- 病例摘要访问控制 --------------------------------------------------

    def add_case_summary(self, payload, actor_id, at=None):
        at = self._at(at)
        case_id = _require(payload, "case_id")
        plan_id = _require(payload, "plan_id")
        patient_ref = _require(payload, "patient_ref")
        abstract = _require(payload, "abstract")
        responsible = _require(payload, "responsible_staff")
        if not isinstance(responsible, list) or not responsible:
            raise ValidationError("至少指定一名负责的轮转人员")
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER, ROLE_STAFF))
            plan = self._get_plan(plan_id)
            for staff_id in responsible:
                self._get_staff(staff_id)
            if case_id in self.cases:
                raise ConflictError(f"病例摘要已存在: {case_id}",
                                    code="case_exists")
            case = {
                "id": case_id,
                "plan_id": plan_id,
                "patient_ref": patient_ref,
                "abstract": abstract,
                "responsible_staff": list(responsible),
                "created_by": actor_id,
                "created_at": at,
            }
            self.cases[case_id] = case
            self._audit("add_case_summary", "case", case_id, actor_id,
                        {"plan_id": plan_id, "responsible": responsible})
            return dict(case)

    def view_case_summary(self, case_id, actor_id, at=None):
        """轮转人员仅限本人负责的病例；管理者不限。"""
        at = self._at(at)
        with self._lock:
            actor = self._actor(actor_id)
            case = self.cases.get(case_id)
            if not case:
                raise NotFoundError(f"病例摘要不存在: {case_id}",
                                    code="case_not_found")
            allowed = (
                actor["role"] == ROLE_MANAGER
                or actor_id in case["responsible_staff"]
            )
            if not allowed:
                self._audit("view_case_denied", "case", case_id, actor_id,
                            {"plan_id": case["plan_id"]}, result="denied")
                raise PermissionError(
                    "轮转人员只能查看自己负责的病例摘要", code="case_access_denied"
                )
            self._audit("view_case_summary", "case", case_id, actor_id,
                        {"plan_id": case["plan_id"]})
            return dict(case)

    def list_case_summaries(self, actor_id, at=None):
        with self._lock:
            actor = self._actor(actor_id)
            if actor["role"] == ROLE_MANAGER:
                cases = list(self.cases.values())
            else:
                cases = [c for c in self.cases.values()
                         if actor_id in c["responsible_staff"]]
            return {"cases": [dict(c) for c in cases]}

    # ---- 时点还原 ----------------------------------------------------------

    def effective_view(self, plan_id, params, actor_id, at=None):
        """还原任一时点：谁可独立执行、谁承担监督、交接是否完成。"""
        at = (params or {}).get("at") or self._at(at)
        with self._lock:
            actor = self._actor(actor_id)
            plan = self._get_plan(plan_id)
            effective, latest = self._version_at(plan, at)
            if effective is None:
                return {
                    "plan_id": plan_id,
                    "at": at,
                    "within_rotation": False,
                    "independent_privileges": [],
                    "supervised_privileges": [],
                    "supervisor": None,
                    "practice_site": None,
                    "handover": None,
                    "visible_to": actor["role"],
                }
            person = self.staff[plan["staff_id"]]
            independent, supervised = [], []
            for priv in effective["privileges"]:
                blockers = self._privilege_blockers(
                    person, plan, effective, priv, at
                )
                entry = {
                    "code": priv["code"],
                    "name": priv.get("name", priv["code"]),
                    "required_scope": priv.get("required_scope"),
                    "blockers": blockers,
                    "authorized": not blockers,
                }
                if priv.get("independent"):
                    independent.append(entry)
                else:
                    supervised.append(entry)
            supervisor = self._supervisor_at(plan, effective, at)
            handover = self._handover_state_at(plan, at)
            return {
                "plan_id": plan_id,
                "at": at,
                "within_rotation": True,
                "version": effective["version"],
                "change_type": effective["change_type"],
                "window": [effective["valid_from"], effective["valid_to"]],
                "practice_site": effective["practice_site"],
                "learning_objectives": effective["learning_objectives"],
                "independent_privileges": independent,
                "supervised_privileges": supervised,
                "supervisor": supervisor,
                "handover": handover,
                "visible_to": actor["role"],
            }

    def _privilege_blockers(self, person, plan, version, priv, at):
        blockers = []
        site = version["practice_site"]
        scope = priv.get("required_scope")
        day = at[:10]
        license_ok = False
        for lic in person["licenses"]:
            scope_ok = scope is None or scope in lic["scopes"]
            site_ok = site in lic["registered_sites"]
            if scope_ok and site_ok and lic["expires_on"] >= day:
                license_ok = True
                break
        if not license_ok:
            blockers.append("license_invalid")
        requires = priv.get("requires", [])
        code = priv["code"]
        if "receipt" in requires:
            ok = any(
                r["staff_id"] == person["id"] and code in r["privilege_codes"]
                and r["confirmed"] and r["confirmed_at"] <= at
                for r in self.receipts.values()
            )
            if not ok:
                blockers.append("awaiting_regulatory_receipt")
        if "assessment" in requires:
            ok = any(
                a["staff_id"] == person["id"]
                and a["plan_id"] == plan["id"]
                and a["privilege_code"] == code
                and a["result"] == "pass"
                and a["signed_at"] and a["signed_at"] <= at
                and (not a["revoked_at"] or a["revoked_at"] > at)
                for a in self.assessments.values()
            )
            if not ok:
                blockers.append("assessment_not_signed_or_revoked")
        return blockers

    def _supervisor_at(self, plan, version, at):
        """合并版本生效与交接确认两类事件，取 at 之前最新的监督人。"""
        events = [(version["valid_from"], version["preceptor_id"], "version")]
        for checklist in self.handovers.values():
            if checklist["plan_id"] != plan["id"]:
                continue
            for item in checklist["items"]:
                if (item["category"] == ITEM_SUPERVISION
                        and item["status"] == ITEM_CONFIRMED
                        and item["confirmed_at"] <= at):
                    events.append(
                        (item["confirmed_at"], item["to_staff"], "handover")
                    )
        events.sort(key=lambda e: e[0])
        current = events[0]
        for event in events:
            if event[0] <= at:
                current = event
        return {"staff_id": current[1], "since": current[0], "source": current[2]}

    def _handover_state_at(self, plan, at):
        candidates = [h for h in self.handovers.values()
                      if h["plan_id"] == plan["id"] and h["triggered_at"] <= at]
        if not candidates:
            return None
        checklist = max(candidates, key=lambda h: h["triggered_at"])
        total = len(checklist["items"])
        confirmed = sum(
            1 for i in checklist["items"]
            if i["status"] == ITEM_CONFIRMED and i["confirmed_at"] <= at
        )
        return {
            "checklist_id": checklist["id"],
            "reason": checklist["reason"],
            "triggered_at": checklist["triggered_at"],
            "to_staff_id": checklist["to_staff_id"],
            "total": total,
            "confirmed": confirmed,
            "completed": confirmed == total,
        }

    # ---- 审计查询 ----------------------------------------------------------

    def list_audit(self, params, actor_id):
        actor = self._require_role(actor_id, (ROLE_MANAGER,))
        params = params or {}
        events = self.audit_log
        if params.get("entity_type"):
            events = [e for e in events
                      if e["entity_type"] == params["entity_type"]]
        if params.get("entity_id"):
            events = [e for e in events
                      if e["entity_id"] == params["entity_id"]]
        if params.get("action"):
            events = [e for e in events if e["action"] == params["action"]]
        return {"actor": actor_id, "events": list(events)}

    # ---- 结业证明 ----------------------------------------------------------

    def issue_certificate(self, plan_id, payload, actor_id, at=None):
        """依据=该计划下已签署、通过、未撤销的考核事实；无依据不得生成。"""
        at = self._at(at)
        with self._lock:
            self._require_role(actor_id, (ROLE_MANAGER,))
            plan = self._get_plan(plan_id)
            facts = [
                {
                    "assessment_id": a["id"],
                    "privilege_code": a["privilege_code"],
                    "title": a["title"],
                    "signed_at": a["signed_at"],
                    "signed_by": a["signed_by"],
                }
                for a in self.assessments.values()
                if a["plan_id"] == plan_id
                and a["result"] == "pass"
                and a["signed_at"] and not a["revoked_at"]
            ]
            if not facts:
                raise ValidationError(
                    "不存在已签署且未被撤销的考核事实，不能生成结业证明",
                    code="no_certificate_basis",
                )
            existing = [c for c in self.certificates.values()
                        if c["plan_id"] == plan_id]
            if existing and not payload.get("allow_reissue"):
                raise ConflictError(
                    "该计划已签发结业证明", code="certificate_exists"
                )
            facts.sort(key=lambda f: f["signed_at"])
            cert_id = f"cert-{plan_id}-{len(existing) + 1}"
            cert = {
                "id": cert_id,
                "plan_id": plan_id,
                "staff_id": plan["staff_id"],
                "issued_at": at,
                "issued_by": actor_id,
                "facts": facts,
            }
            self.certificates[cert_id] = cert
            self._audit("issue_certificate", "certificate", cert_id, actor_id,
                        {"plan_id": plan_id, "fact_count": len(facts)})
            return self._certificate_view(cert)

    def verify_certificate(self, cert_id, actor_id, at=None):
        at = self._at(at)
        with self._lock:
            self._actor(actor_id)
            cert = self.certificates.get(cert_id)
            if not cert:
                raise NotFoundError(f"结业证明不存在: {cert_id}",
                                    code="certificate_not_found")
            invalidated = []
            for fact in cert["facts"]:
                assessment = self.assessments.get(fact["assessment_id"])
                if (assessment is None or assessment["revoked_at"]
                        or not assessment["signed_at"]):
                    invalidated.append(fact["assessment_id"])
            view = self._certificate_view(cert)
            view["status"] = "valid" if not invalidated else "invalidated"
            if invalidated:
                view["invalidated_basis"] = invalidated
            view["verified_at"] = at
            return view

    def _certificate_view(self, cert):
        return {
            "id": cert["id"],
            "plan_id": cert["plan_id"],
            "staff_id": cert["staff_id"],
            "issued_at": cert["issued_at"],
            "issued_by": cert["issued_by"],
            "facts": [dict(f) for f in cert["facts"]],
        }
