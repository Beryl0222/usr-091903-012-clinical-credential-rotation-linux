"""HTTP JSON 接入层。

所有写操作必须携带 X-Actor-Id 标识主体；路径参数与请求体映射到 RotationService。
"""

import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .domain import (
    RotationService, DomainError, ValidationError, NotFoundError,
    ConflictError, AuthorizationError,
)
from .store import EventStore

SERVICE_ID = "clinical-credential-rotation"
SERVICE_NAME = "医护独立执业轮转"


def health_payload():
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "RotationBackend/1.0"

    # 由 ThreadingHTTPServer 的 server 属性注入 service
    @property
    def service(self):
        return self.server.rotation_service

    # ---- 工具 -------------------------------------------------------------
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, code, message):
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError("请求体必须是合法 JSON")
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _actor(self):
        actor = self.headers.get("X-Actor-Id", "").strip()
        if not actor:
            raise AuthorizationError("缺少 X-Actor-Id 请求头")
        return actor

    def _handle_domain_error(self, error):
        mapping = {
            ValidationError: 400,
            NotFoundError: 404,
            AuthorizationError: 403,
            ConflictError: 409,
        }
        status = mapping.get(type(error), 400)
        self._send_error(status, error.code, str(error))

    def log_message(self, *_args):
        return

    # ---- 路由 -------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/health":
                self._send_json(200, health_payload())
                return
            # GET /staff/{id}/capability?on_date=
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "staff" and parts[2] == "capability":
                self._send_json(200, self.service.effective_capability(
                    parts[1], query.get("on_date"),
                    int(query["at_seq"]) if query.get("at_seq") else None))
                return
            if len(parts) == 3 and parts[0] == "staff" and parts[2] == "cases":
                self._send_json(200, self.service.my_cases(parts[1]))
                return
            if len(parts) == 2 and parts[0] == "agreements":
                self._send_json(200, self.service.get_agreement(parts[1]))
                return
            if len(parts) == 2 and parts[0] == "handovers":
                self._send_json(200, self.service.handover_detail(parts[1]))
                return
            if parts == ["trace"]:
                at_seq = int(query.get("at_seq", self.service.store.seq))
                self._send_json(200, self.service.responsibility_trace(
                    at_seq, query.get("on_date")))
                return
            if parts == ["events"]:
                after = int(query.get("after_seq", 0))
                self._send_json(200, {"events": self.service.event_log(after)})
                return
            self._send_error(404, "not_found", "接口不存在")
        except DomainError as error:
            self._handle_domain_error(error)
        except ValueError:
            self._send_error(400, "validation_error", "序号参数必须是整数")
        except Exception:
            self._send_error(500, "internal_error", "服务内部错误")

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        try:
            body = self._read_body()
            actor = self._actor()
            service = self.service

            if parts == ["staff"]:
                self._send_json(201, service.register_staff(
                    body.get("staff_id"), body.get("name"),
                    body.get("role"), actor))
            elif len(parts) == 2 and parts[0] == "staff" and parts[1] == "credentials":
                self._send_json(201, service.record_credential(
                    body["staff_id"], body["code"], body.get("title"),
                    body.get("expires_on"), actor))
            elif parts == ["agreements"]:
                self._send_json(201, service.lock_agreement(body, actor))
            elif len(parts) == 3 and parts[0] == "agreements" and parts[2] == "amendments":
                result = service.propose_amendment(
                    parts[1], body["kind"], body.get("changes", {}),
                    body["party"], actor, body.get("amendment_id"))
                self._send_json(201, result)
            elif len(parts) == 5 and parts[0] == "agreements" and parts[2] == "amendments" and parts[4] == "approvals":
                self._send_json(200, service.approve_amendment(
                    parts[1], parts[3], body["party"], actor))
            elif parts == ["assignments"]:
                self._send_json(201, service.create_assignment(body, actor))
            elif len(parts) == 3 and parts[0] == "assignments" and parts[2] == "response":
                self._send_json(200, service.respond_assignment(
                    parts[1], bool(body.get("accept")), actor))
            elif len(parts) == 3 and parts[0] == "assignments" and parts[2] == "complete":
                self._send_json(200, service.complete_assignment(parts[1], actor))
            elif parts == ["receipts"]:
                self._send_json(201, service.record_receipt(body, actor))
            elif parts == ["assessments"]:
                self._send_json(201, service.award_assessment(body, actor))
            elif len(parts) == 3 and parts[0] == "assessments" and parts[2] == "revoke":
                self._send_json(200, service.revoke_assessment(
                    parts[1], body.get("reason"), actor))
            elif len(parts) == 3 and parts[0] == "agreements" and parts[2] == "mentor-leave":
                self._send_json(200, service.set_mentor_leave(
                    parts[1], bool(body.get("on_leave")), actor))
            elif len(parts) == 3 and parts[0] == "agreements" and parts[2] == "emergency-return":
                self._send_json(200, service.emergency_return(
                    parts[1], body["return_date"], actor))
            elif len(parts) == 3 and parts[0] == "agreements" and parts[2] == "credential-handover":
                self._send_json(201, service.trigger_handover_credential_expiry(
                    parts[1], actor, body.get("trigger_date")))
            elif (len(parts) == 5 and parts[0] == "handovers"
                  and parts[2] == "items" and parts[4] == "confirm"):
                self._send_json(200, service.confirm_handover_item(
                    parts[1], parts[3], body["new_owner_id"], actor))
            elif parts == ["cases"]:
                self._send_json(201, service.assign_case(body, actor))
            elif len(parts) == 3 and parts[0] == "cases" and parts[2] == "sign":
                self._send_json(200, service.sign_case(
                    parts[1], actor, body.get("signed_on")))
            elif len(parts) == 3 and parts[0] == "cases" and parts[2] == "followup":
                self._send_json(201, service.open_followup(
                    parts[1], body["due_date"], actor))
            elif len(parts) == 3 and parts[0] == "cases" and parts[2] == "followup-complete":
                self._send_json(200, service.complete_followup(parts[1], actor))
            elif len(parts) == 3 and parts[0] == "agreements" and parts[2] == "certificate":
                self._send_json(201, service.issue_certificate(
                    parts[1], actor, body.get("certificate_no"),
                    body.get("issued_on")))
            else:
                self._send_error(404, "not_found", "接口不存在")
        except DomainError as error:
            self._handle_domain_error(error)
        except KeyError as error:
            self._send_error(400, "validation_error", f"缺少必填项：{error.args[0]}")
        except Exception:
            self._send_error(500, "internal_error", "服务内部错误")


def build_server(port, service=None):
    service = service or RotationService(
        EventStore(), clock=lambda: date.today().isoformat())
    server = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    server.rotation_service = service
    return server
