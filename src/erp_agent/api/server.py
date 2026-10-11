import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from erp_agent.adapters.documents import LibreOfficeConverter, OOXMLDocumentParser
from erp_agent.adapters.jobs import ThreadJobRunner
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.adapters.model_settings import ModelSettingsService
from erp_agent.adapters.sessions import SQLiteSessionStore
from erp_agent.application.access import AccessService
from erp_agent.application.contracts import JSONObject, dumps, integer, loads, obj, string
from erp_agent.application.ports import LLMClient, PlanEvaluator
from erp_agent.application.reader import StructuredReader
from erp_agent.application.solver import DraftPlanEvaluator, LLMStrategyGenerator, SolverService
from erp_agent.application.workflow import WorkflowService
from erp_agent.config import Settings, load_env_file
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import SolverMode

ID = re.compile(r"^[a-f0-9]{32}$")


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings: Settings, workflow: WorkflowService) -> None:
        self.settings, self.workflow = settings, workflow
        self.access = create_access(settings)
        self.models = workflow.model_settings
        assert self.models is not None
        super().__init__((settings.host, settings.port), Handler)

    def server_close(self) -> None:
        self.workflow.jobs.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: AppHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            if method in {"POST", "DELETE"}:
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    raise ERPError("origin_rejected", "不接受跨站写入请求。", 403)
            self._dispatch(method)
        except ERPError as exc:
            self.close_connection = True
            self._json({"error": {"code": exc.code, "message": exc.message}}, exc.status)
        except (ValueError, UnicodeError) as exc:
            self.close_connection = True
            self._json({"error": {"code": "invalid_request", "message": "请求内容或参数无效。"}}, 400)
            logging.debug("请求校验失败: %s", type(exc).__name__)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            logging.exception("Web 请求处理失败")
            self.close_connection = True
            self._json({"error": {"code": "internal_error", "message": "服务端处理失败。"}}, 500)

    def _dispatch(self, method: str) -> None:
        workflow = self.server.workflow
        parsed = urlsplit(self.path)
        path = parsed.path
        if method == "GET" and path in {"/", "/app.js", "/style.css"}:
            name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
            content_type = {
                "/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css",
            }[path]
            body = files("erp_agent").joinpath(f"api/static/{name}").read_bytes()
            self._respond(body, content_type + "; charset=utf-8")
            return
        if path == "/api/auth/session" and method == "GET":
            self._json({"authenticated": self.server.access.authenticated(self._session_token()),
                        "configured": self.server.access.configured})
            return
        if path == "/api/auth/login" and method == "POST":
            data = self._body_json()
            remember = data.get("remember", True)
            if not isinstance(remember, bool):
                raise ERPError("invalid_remember", "记住登录选项无效。")
            session = self.server.access.login(string(data.get("key"), maximum=2048), remember,
                                                self.client_address[0])
            self._set_session_cookie(session.token, session.max_age)
            self._json({"authenticated": True})
            return
        if path.startswith("/api/"):
            if not self.server.access.authenticated(self._session_token()):
                raise ERPError("authentication_required", "请先输入登录密钥。", 401)
        if path == "/api/auth/logout" and method == "POST":
            self._body_json()
            self.server.access.logout(self._session_token())
            self._set_session_cookie("", 0)
            self._json({"authenticated": False})
            return
        if path == "/api/model-settings":
            if method == "GET":
                self._json(self.server.models.public())
            elif method == "POST":
                self._json(self.server.models.update(self._body_json()))
            else:
                raise ERPError("method_not_allowed", "不支持该操作。", 405)
            return
        if path == "/api/model-presets":
            if method == "GET":
                self._json(self.server.models.catalog())
            elif method == "POST":
                self._json(self.server.models.create_preset(self._body_json()), 201)
            else:
                raise ERPError("method_not_allowed", "不支持该操作。", 405)
            return
        preset_parts = path.strip("/").split("/")
        if len(preset_parts) >= 3 and preset_parts[:2] == ["api", "model-presets"]:
            preset_id = preset_parts[2]
            if len(preset_parts) == 4 and preset_parts[3] == "activate" and method == "POST":
                self._json(self.server.models.activate(preset_id, self._body_json()))
            elif len(preset_parts) == 3 and method == "POST":
                self._json(self.server.models.update_preset(preset_id, self._body_json()))
            elif len(preset_parts) == 3 and method == "DELETE":
                query = parse_qs(parsed.query)
                raw_revision = query.get("revision", [""])[0]
                revision = integer(int(raw_revision))
                self.server.models.delete_preset(preset_id, revision)
                self._json({"deleted": True})
            else:
                raise ERPError("method_not_allowed", "不支持该操作。", 405)
            return
        if method == "GET" and path == "/api/status":
            self._json({"llm_configured": workflow.reader.llm.configured,
                        "simulation_ready": workflow.solver.evaluator.supports_scoring,
                        "default_solver": self.server.settings.default_solver.value,
                        "solver_modes": [m.value for m in SolverMode],
                        "max_upload_mb": self.server.settings.max_upload_mb})
            return
        if path == "/api/projects":
            if method == "GET":
                self._json(workflow.repository.list_all("project"))
            elif method == "POST":
                self._json(workflow.create_project(string(self._body_json().get("name"))), 201)
            else:
                raise ERPError("method_not_allowed", "不支持该操作。", 405)
            return
        parts = path.strip("/").split("/")
        if (len(parts) == 3 and parts[:2] == ["api", "projects"]
                and ID.fullmatch(parts[2]) and method == "DELETE"):
            workflow.delete_project(parts[2])
            self._json({"deleted": True})
            return
        if len(parts) < 4 or parts[:2] != ["api", "projects"] or not ID.fullmatch(parts[2]):
            raise ERPError("not_found", "接口不存在。", 404)
        project_id, resource = parts[2:4]
        workflow.project(project_id)
        if len(parts) == 4:
            if resource == "documents":
                if method == "GET":
                    self._json(workflow.documents(project_id))
                elif method == "POST":
                    name = unquote(self.headers.get("X-Filename", ""))
                    self._json(workflow.upload(project_id, name, self._body(
                        self.server.settings.max_upload_mb * 1024 * 1024)), 202)
                else:
                    raise ERPError("method_not_allowed", "不支持该操作。", 405)
            elif resource == "read" and method == "POST":
                self._json(workflow.read(project_id, self._body_json()), 202)
            elif resource == "problem" and method == "GET":
                query = parse_qs(parsed.query)
                version = int(query["version"][0]) if "version" in query else None
                snapshot = workflow.repository.snapshot(project_id, version)
                self._json({"snapshot": snapshot,
                            "versions": workflow.repository.versions(project_id)})
            elif resource == "revisions" and method == "POST":
                self._json(workflow.revise(project_id, self._body_json()), 201)
            elif resource == "solve" and method == "POST":
                self._json(workflow.solve(project_id, self._body_json()), 202)
            elif resource in {"jobs", "solutions"} and method == "GET":
                self._json(workflow.repository.list(resource.rstrip("s"), project_id))
            else:
                raise ERPError("not_found", "接口不存在。", 404)
            return
        if resource == "jobs" and ID.fullmatch(parts[4]):
            if len(parts) == 5 and method == "GET":
                self._json(workflow.repository.get("job", project_id, parts[4]))
                return
            if len(parts) == 6 and parts[5] == "cancel" and method == "POST":
                self._body_json()
                self._json(workflow.jobs.cancel(project_id, parts[4]))
                return
        if (resource == "documents" and len(parts) == 6 and ID.fullmatch(parts[4])
                and parts[5] == "retry" and method == "POST"):
            self._body_json()
            self._json(workflow.retry_parse(project_id, parts[4]), 202)
            return
        raise ERPError("not_found", "接口不存在。", 404)

    def _body(self, limit: int) -> bytes:
        self.connection.settimeout(30)
        if self.headers.get("Transfer-Encoding"):
            raise ERPError("invalid_transfer", "请使用明确长度的请求体。", 400)
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > limit:
            raise ERPError("body_too_large", "上传内容超过大小限制。", 413)
        content = self.rfile.read(length)
        if len(content) != length:
            raise ERPError("incomplete_body", "上传内容不完整。")
        return content

    def _body_json(self) -> JSONObject:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ERPError("content_type", "请求需要 application/json。", 415)
        return obj(loads(self._body(4 * 1024 * 1024).decode("utf-8")))

    def _json(self, payload: object, status: int = 200) -> None:
        self._respond(dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status)

    def _respond(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if hasattr(self, "_cookie_header"):
            self.send_header("Set-Cookie", self._cookie_header)
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "object-src 'none'; frame-ancestors 'none'")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _session_token(self) -> str:
        cookie: SimpleCookie[str] = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        value = cookie.get("erp_session")
        return value.value if value else ""

    def _set_session_cookie(self, token: str, max_age: int | None) -> None:
        cookie: SimpleCookie[str] = SimpleCookie()
        cookie["erp_session"] = token
        item = cookie["erp_session"]
        item["path"] = "/"
        item["httponly"] = True
        item["samesite"] = "Strict"
        item["secure"] = self.server.settings.cookie_secure
        if max_age is not None:
            item["max-age"] = max_age
        self._cookie_header = item.OutputString()


def create_access(settings: Settings) -> AccessService:
    return AccessService(settings.access_key,
                         SQLiteSessionStore(settings.data_dir / "access.sqlite3"))


def create_workflow(settings: Settings, llm: LLMClient | None = None,
                    evaluator: PlanEvaluator | None = None) -> WorkflowService:
    repository = SQLiteRepository(settings.data_dir / "erp.sqlite3")
    jobs = ThreadJobRunner(repository)
    models = ModelSettingsService(settings, llm)
    client = models.client
    parser = OOXMLDocumentParser(LibreOfficeConverter(settings.libreoffice))
    solver = SolverService(LLMStrategyGenerator(client),
                           evaluator if evaluator is not None else DraftPlanEvaluator())
    return WorkflowService(repository, jobs, parser, StructuredReader(client, client.current), solver,
                           settings.default_solver, models)


def create_server(settings: Settings, llm: LLMClient | None = None,
                  evaluator: PlanEvaluator | None = None) -> AppHTTPServer:
    workflow = create_workflow(settings, llm, evaluator)
    try:
        return AppHTTPServer(settings, workflow)
    except Exception:
        workflow.jobs.close()
        raise


def main() -> None:
    load_env_file(Path(".env"))
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = create_server(settings)
    logging.info("ERP 沙盘工作台 http://%s:%s", settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("正在关闭服务")
    finally:
        server.server_close()
