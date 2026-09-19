"""轮转履约后端的 HTTP 路由分发。

仅依赖标准库，将 JSON 请求映射到 :class:`rotation_service.RotationService`。

约定：
* 调用方身份通过 ``X-Actor`` 头传递；
* 写操作可携带 ``Idempotency-Key`` 头，重试不会重复授予能力；
* 时间点可通过请求体或查询串的 ``at`` 显式指定，便于回放与测试。
"""

import json
import re
from urllib.parse import parse_qs, urlsplit

from rotation_service import (
    PermissionError as ServicePermissionError,
    ConflictError,
    NotFoundError,
    RotationService,
    ServiceError,
    ValidationError,
)

# (方法, 路径正则, 处理函数名)
ROUTES = [
    ("POST", r"^/orgs$", "register_org"),
    ("POST", r"^/staff$", "register_staff"),
    ("POST", r"^/actors$", "register_actor"),
    ("POST", r"^/plans$", "create_plan"),
    ("GET", r"^/plans/(?P<plan_id>[^/]+)$", "get_plan"),
    ("POST", r"^/amendments$", "propose_amendment"),
    ("POST", r"^/amendments/(?P<amendment_id>[^/]+)/approvals$",
     "approve_amendment"),
    ("POST", r"^/tasks$", "assign_task"),
    ("POST", r"^/tasks/(?P<task_id>[^/]+)/cancel$", "cancel_task"),
    ("POST", r"^/receipts$", "submit_receipt"),
    ("POST", r"^/assessments$", "upload_assessment"),
    ("POST", r"^/assessments/(?P<assessment_id>[^/]+)/sign$", "sign_assessment"),
    ("POST", r"^/assessments/(?P<assessment_id>[^/]+)/revoke$",
     "revoke_assessment"),
    ("POST", r"^/plans/(?P<plan_id>[^/]+)/records$", "register_open_record"),
    ("POST", r"^/records/(?P<record_id>[^/]+)/sign$", "sign_record"),
    ("POST", r"^/plans/(?P<plan_id>[^/]+)/followups$", "register_followup"),
    ("POST", r"^/followups/(?P<followup_id>[^/]+)/complete$",
     "complete_followup"),
    ("POST", r"^/plans/(?P<plan_id>[^/]+)/handovers$", "trigger_handover"),
    ("GET", r"^/handovers/(?P<checklist_id>[^/]+)$", "get_handover"),
    ("POST", r"^/handovers/(?P<checklist_id>[^/]+)/items/"
     r"(?P<item_id>[^/]+)/confirm$", "confirm_handover_item"),
    ("POST", r"^/cases$", "add_case_summary"),
    ("GET", r"^/cases$", "list_case_summaries"),
    ("GET", r"^/cases/(?P<case_id>[^/]+)$", "view_case_summary"),
    ("GET", r"^/plans/(?P<plan_id>[^/]+)/effective$", "effective_view"),
    ("GET", r"^/audit$", "list_audit"),
    ("POST", r"^/plans/(?P<plan_id>[^/]+)/certificates$", "issue_certificate"),
    ("GET", r"^/certificates/(?P<cert_id>[^/]+)/verify$", "verify_certificate"),
]

COMPILED_ROUTES = [
    (method, re.compile(pattern), handler) for method, pattern, handler in ROUTES
]

IDEMPOTENT_HANDLERS = {"submit_receipt", "upload_assessment"}


class BadRequest(ServiceError):
    status = 400
    code = "bad_request"


class MethodNotAllowed(ServiceError):
    status = 405
    code = "method_not_allowed"


def dispatch(service, method, path, headers, raw_body):
    """纯函数式分发，返回 (status_code, response_dict)。"""
    parts = urlsplit(path)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    actor_id = headers.get("x-actor")
    idem_key = headers.get("idempotency-key")

    for route_method, pattern, handler_name in COMPILED_ROUTES:
        if route_method != method:
            continue
        match = pattern.match(parts.path)
        if not match:
            continue
        kwargs = match.groupdict()
        body = {}
        if method in ("POST", "PUT", "PATCH"):
            if raw_body:
                try:
                    body = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BadRequest(f"请求体不是合法 JSON: {exc}")
            if not isinstance(body, dict):
                raise BadRequest("请求体必须是 JSON 对象")
        handler = getattr(service, handler_name)
        at = query.get("at")
        if not at and isinstance(body, dict):
            at = body.get("at")
        if handler_name in IDEMPOTENT_HANDLERS:
            return handler(body, actor_id, at=at, idempotency_key=idem_key)
        if handler_name in ("effective_view",):
            return handler(kwargs.get("plan_id"), query, actor_id)
        if handler_name == "list_audit":
            return handler(query, actor_id)
        if handler_name == "list_case_summaries":
            return handler(actor_id, at=at)
        if handler_name in ("get_plan", "get_handover", "view_case_summary",
                            "verify_certificate"):
            return handler(
                kwargs.get("plan_id")
                or kwargs.get("checklist_id")
                or kwargs.get("case_id")
                or kwargs.get("cert_id"),
                actor_id,
                at=at,
            )
        if handler_name in ("approve_amendment", "revoke_assessment"):
            id_arg = (kwargs.get("amendment_id")
                      or kwargs.get("assessment_id"))
            return handler(id_arg, body, actor_id, at=at)
        if handler_name in ("sign_assessment", "cancel_task", "sign_record",
                            "complete_followup"):
            id_arg = (kwargs.get("assessment_id") or kwargs.get("task_id")
                      or kwargs.get("record_id") or kwargs.get("followup_id"))
            return handler(id_arg, actor_id, at=at)
        if handler_name == "confirm_handover_item":
            return handler(
                kwargs["checklist_id"], kwargs["item_id"], body, actor_id, at=at
            )
        if handler_name in ("register_open_record", "register_followup",
                            "trigger_handover", "issue_certificate"):
            return handler(kwargs["plan_id"], body, actor_id, at=at)
        # 无路径参数的写接口
        return handler(body, actor_id, at=at)

    # 路径存在但方法不符时返回 405。
    known_path = any(
        re.compile(pattern).match(parts.path)
        for _m, pattern, _h in ROUTES
    )
    if known_path:
        raise MethodNotAllowed("方法不被允许")
    raise NotFoundError("未知接口", code="unknown_route")


def error_status(exc):
    if isinstance(exc, ValidationError):
        return 400
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ServicePermissionError):
        return 403
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, ServiceError):
        return exc.status
    return 500


def build_handler(service=None):
    """构造绑定到指定服务实例的 Handler 类，便于测试隔离。"""
    from http.server import BaseHTTPRequestHandler

    service = service or RotationService()

    class RotationHandler(BaseHTTPRequestHandler):
        server_version = "RotationService/1.0"

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                from service import health_payload
                self._write_json(200, health_payload())
                return
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                response = dispatch(
                    service, method, self.path, headers, raw_body
                )
            except ServiceError as exc:
                self._write_json(error_status(exc), exc.to_dict())
                return
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接挂死
                self._write_json(
                    500,
                    {"error": "internal_error", "message": str(exc)},
                )
                return
            self._write_json(200, response)

        def _write_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return RotationHandler
