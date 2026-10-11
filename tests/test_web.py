import tempfile
import io
import time
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from types import SimpleNamespace

from erp_agent.api.server import Handler, create_access, create_workflow
from erp_agent.application.contracts import dumps, loads
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.config import Settings
from tests.helpers import FakeLLM, docx_bytes, proposal_json


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="erp-test-")
        self.llm = FakeLLM("")
        settings = Settings(port=8000, data_dir=Path(self.temp.name), access_key="test-access")
        workflow = create_workflow(settings, self.llm)
        self.server = SimpleNamespace(settings=settings, workflow=workflow,
                                      access=create_access(settings), models=workflow.model_settings)
        self.cookie = ""
        self.response_headers = ""
        self.request("/api/auth/login", {"key": "test-access", "remember": True})

    def tearDown(self):
        self.server.workflow.jobs.close()
        self.temp.cleanup()

    def request(self, path, data=None, content=None, headers=None):
        body = dumps(data).encode() if data is not None else content
        actual_headers = headers or ({"Content-Type": "application/json"} if data is not None else {})
        code, result = self.raw_request(path, body, actual_headers)
        if code >= 400:
            raise HTTPError(path, code, result.decode(), None, None)
        return loads(result.decode())

    def raw_request(self, path, body=None, headers=None, method=None):
        method = method or ("POST" if body is not None else "GET")
        content = body or b""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost:8000", "Connection: close",
                 f"Content-Length: {len(content)}"]
        lines.extend(f"{k}: {v}" for k, v in (headers or {}).items())
        if self.cookie:
            lines.append("Cookie: " + self.cookie)
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + content

        class MemorySocket:
            def __init__(self):
                self.input = io.BytesIO(raw)
                self.output = bytearray()
            def makefile(self, mode, buffering=None):
                return self.input
            def sendall(self, data):
                self.output.extend(data)
            def settimeout(self, value):
                pass

        connection = MemorySocket()
        Handler(connection, ("127.0.0.1", 12345), self.server)
        head, result = bytes(connection.output).split(b"\r\n\r\n", 1)
        self.response_headers = head.decode()
        for line in self.response_headers.split("\r\n"):
            if line.startswith("Set-Cookie: "):
                self.cookie = line[len("Set-Cookie: "):].split(";", 1)[0]
        return int(head.split(b" ")[1]), result

    def wait_job(self, project, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.request(f"/api/projects/{project}/jobs/{job}")
            if result["status"] not in {"running", "queued"}:
                return result
            time.sleep(0.01)
        self.fail("后台任务没有完成")

    def setup_problem(self):
        project = self.request("/api/projects", {"name": "离线端到端"})["id"]
        base = f"/api/projects/{project}"
        job = self.request(base + "/documents", content=docx_bytes(), headers={
            "Content-Type": "application/octet-stream", "X-Filename": "topic.docx",
        })
        self.assertEqual(self.wait_job(project, job["id"])["status"], "succeeded")
        docs = self.request(base + "/documents")
        eid = docs[0]["evidence"][0]["id"]
        facts = []
        for key, value, kind in [("initial_cash", "100000", "number"),
                                 ("years", "4", "integer"), ("periods_per_year", "4", "integer")]:
            facts.append({"id": key, "category": "initial", "entity_id": "company",
                          "entity_name": "公司", "key": key, "value": value, "value_type": kind,
                          "unit": "", "state": "known", "evidence_ids": [eid],
                          "conversion": "identity", "explanation": ""})
        facts[1]["state"] = "assumed"
        self.llm.response = dumps({"title": "测试题目", "facts": facts, "rules": [], "issues": []})
        job = self.request(base + "/read", {})
        self.assertEqual(self.wait_job(project, job["id"])["status"], "succeeded")
        result = self.request(base + "/problem")
        return project, result

    def test_upload_read_revise_solve_and_isolation(self):
        project, result = self.setup_problem()
        base = f"/api/projects/{project}"
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["version"], 1)
        editable = {"title": "修正后的题目", "facts": snapshot["reading"]["problem"]["facts"],
                    "rules": [], "issues": []}
        revised = self.request(base + "/revisions", {"expected_version": 1,
                                                      "reading": editable, "note": "复核题目名称"})
        self.assertEqual(revised["version"], 2)
        self.assertEqual(self.request(base + "/problem?version=1")["snapshot"]["reading"]["problem"]["title"],
                         "测试题目")
        with self.assertRaises(HTTPError) as conflict:
            self.request(base + "/revisions", {"expected_version": 1, "reading": editable, "note": "旧版"})
        self.assertEqual(conflict.exception.code, 409)
        self.llm.response = proposal_json()
        job = self.request(base + "/solve", {"version": 2})
        done = self.wait_job(project, job["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertFalse(done["result"]["verified"])
        solutions = self.request(base + "/solutions")
        self.assertEqual(solutions[0]["problem_version"], 2)
        self.assertEqual(solutions[0]["mode"], "llm_only")
        with self.assertRaises(HTTPError) as mc:
            self.request(base + "/solve", {"version": 2, "mode": "llm_monte_carlo"})
        self.assertEqual(mc.exception.code, 409)
        other = self.request("/api/projects", {"name": "另一个项目"})["id"]
        with self.assertRaises(HTTPError) as isolated:
            self.request(f"/api/projects/{other}/jobs/{job['id']}")
        self.assertEqual(isolated.exception.code, 404)

    def test_parse_retry_uses_stored_original(self):
        project = self.request("/api/projects", {"name": "重试"})["id"]
        base = f"/api/projects/{project}"
        job = self.request(base + "/documents", content=docx_bytes(), headers={
            "Content-Type": "application/octet-stream", "X-Filename": "topic.docx",
        })
        done = self.wait_job(project, job["id"])
        document_id = done["result"]["document_id"]
        retry = self.request(base + f"/documents/{document_id}/retry", {})
        self.assertEqual(self.wait_job(project, retry["id"])["status"], "succeeded")
        self.assertEqual(len(self.request(base + "/documents")), 1)

    def test_historical_version_can_have_large_model_response(self):
        project, result = self.setup_problem()
        repository = self.server.workflow.repository
        snapshot = result["snapshot"]
        snapshot["reading"]["raw_response"] = " " * 60000
        repository.save_snapshot(project, snapshot, 1)
        self.llm.response = proposal_json()
        job = self.request(f"/api/projects/{project}/solve", {"version": 2})
        self.assertEqual(self.wait_job(project, job["id"])["status"], "succeeded")

    def test_config_does_not_expose_key_and_static(self):
        status = self.request("/api/status")
        self.assertEqual(status["solver_modes"], ["llm_only", "llm_monte_carlo"])
        self.assertNotIn("llm_api_key", status)
        code, raw = self.raw_request("/")
        self.assertEqual(code, 200)
        html = raw.decode()
        self.assertIn("单 LLM", html)
        self.assertIn("蒙特卡罗", html)

    def test_invalid_upload_and_cross_origin(self):
        project = self.request("/api/projects", {"name": "测试"})["id"]
        with self.assertRaises(HTTPError):
            self.request(f"/api/projects/{project}/documents", content=b"x", headers={
                "Content-Type": "application/octet-stream", "X-Filename": "../secret.docx",
            })
        with self.assertRaises(HTTPError) as cross:
            self.request("/api/projects", {"name": "跨站"}, headers={
                "Content-Type": "application/json", "Origin": "https://unrelated.example",
            })
        self.assertEqual(cross.exception.code, 403)

    def test_restart_persistence(self):
        project = self.request("/api/projects", {"name": "持久化"})["id"]
        self.server.workflow.repository.put("job", project, "c" * 32,
                                            {"id": "c" * 32, "project_id": project, "status": "running"})
        repository = SQLiteRepository(Path(self.temp.name) / "erp.sqlite3")
        self.assertEqual(repository.get("job", project, "c" * 32)["status"], "failed")
        self.assertEqual(repository.get("project", project, project)["name"], "持久化")

    def test_login_required_remember_logout_and_restart(self):
        saved_cookie = self.cookie
        self.cookie = ""
        code, _ = self.raw_request("/api/projects")
        self.assertEqual(code, 401)
        with self.assertRaises(HTTPError):
            self.request("/api/auth/login", {"key": "wrong"})
        self.request("/api/auth/login", {"key": "test-access"})
        self.assertIn("Max-Age=2592000", self.response_headers)
        self.assertIn("HttpOnly", self.response_headers)
        self.assertIn("SameSite=Strict", self.response_headers)
        self.server.access = create_access(self.server.settings)
        self.assertTrue(self.request("/api/auth/session")["authenticated"])
        self.request("/api/auth/logout", {})
        self.assertFalse(self.request("/api/auth/session")["authenticated"])
        self.cookie = saved_cookie
        self.assertTrue(self.request("/api/auth/session")["authenticated"])

    def test_session_cookie_without_remember_and_key_rotation(self):
        self.request("/api/auth/login", {"key": "test-access", "remember": False})
        self.assertNotIn("Max-Age", self.response_headers)
        self.server.access = create_access(replace(self.server.settings, access_key="rotated"))
        self.assertFalse(self.request("/api/auth/session")["authenticated"])

    def test_model_settings_persist_and_never_return_secret(self):
        data = self.request("/api/model-settings", {"base_url": "https://example.com/v1",
                                                    "model": "custom-model", "api_key": "secret-123"})
        self.assertTrue(data["api_key_set"])
        self.assertTrue(data["configured"])
        self.assertNotIn("secret-123", dumps(self.request("/api/model-settings")))
        self.request("/api/model-settings", {"model": "another", "api_key": ""})
        from erp_agent.adapters.model_settings import ModelSettingsService
        settings = ModelSettingsService(self.server.settings)
        self.assertEqual(settings.public()["model"], "another")
        self.assertEqual(settings.settings.llm_api_key, "secret-123")
        self.request("/api/model-settings", {"clear_api_key": True})
        self.assertFalse(self.request("/api/model-settings")["api_key_set"])

    def test_provider_setting_is_visible_and_switches_runtime_status(self):
        metadata = self.request("/api/model-settings")
        self.assertEqual(len(metadata["providers"]), 5)
        changed = self.request("/api/model-settings", {"provider": "ollama", "model": "local-model"})
        self.assertEqual(changed["base_url"], "http://localhost:11434")
        self.assertFalse(changed["api_key_set"])
        self.assertTrue(self.request("/api/status")["llm_configured"])
        self.request("/api/model-settings", {"provider": "gemini", "model": "gemini-model"})
        self.assertFalse(self.request("/api/status")["llm_configured"])

    def test_delete_project_cleans_every_record_and_blocks_late_write(self):
        project, result = self.setup_problem()
        repository = self.server.workflow.repository
        repository.put("solution", project, "s", {"id": "s"})
        code, _ = self.raw_request(f"/api/projects/{project}", method="DELETE")
        self.assertEqual(code, 200)
        self.assertEqual(repository.list("original", project), [])
        self.assertEqual(repository.list("document", project), [])
        self.assertEqual(repository.list("solution", project), [])
        self.assertEqual(repository.versions(project), [])
        from erp_agent.domain.errors import ERPError
        with self.assertRaises(ERPError):
            repository.put("document", project, "late", {"id": "late"})

    def test_selected_documents_must_belong_to_project(self):
        project, _ = self.setup_problem()
        with self.assertRaises(HTTPError) as error:
            self.request(f"/api/projects/{project}/read", {"document_ids": ["not-owned"]})
        self.assertEqual(error.exception.code, 400)

    def test_delete_running_project_does_not_restore_data(self):
        project = self.request("/api/projects", {"name": "运行中删除"})["id"]
        entered, release = threading.Event(), threading.Event()
        repository = self.server.workflow.repository

        def operation(control):
            entered.set()
            release.wait(3)
            control.checkpoint("saving_document")
            repository.put("document", project, "late", {"id": "late"})
            return {"ok": True}

        runner = self.server.workflow.jobs
        job = runner.submit(project, "parse", operation)
        try:
            self.assertTrue(entered.wait(2))
            code, _ = self.raw_request(f"/api/projects/{project}", method="DELETE")
            self.assertEqual(code, 200)
            release.set()
            runner.futures[job["id"]].result(timeout=3)
            self.assertEqual(repository.list("job", project), [])
            self.assertEqual(repository.list("document", project), [])
        finally:
            release.set()

    def test_selected_read_uses_only_selected_document_and_blocks_duplicate(self):
        project, _ = self.setup_problem()
        base = f"/api/projects/{project}"
        job = self.request(base + "/documents", content=docx_bytes(), headers={
            "Content-Type": "application/octet-stream", "X-Filename": "second.docx",
        })
        self.wait_job(project, job["id"])
        document_id = self.request(base + "/documents")[0]["source"]["id"]
        original = self.server.workflow.reader
        entered, release = threading.Event(), threading.Event()

        class DelayedReader:
            llm = original.llm

            def read(self, documents, control):
                entered.set()
                release.wait(3)
                return original.read(documents, control)

        self.server.workflow.reader = DelayedReader()
        try:
            job = self.request(base + "/read", {"document_ids": [document_id]})
            self.assertTrue(entered.wait(2))
            with self.assertRaises(HTTPError) as duplicate:
                self.request(base + "/read", {"document_ids": [document_id]})
            self.assertEqual(duplicate.exception.code, 409)
            release.set()
            self.assertEqual(self.wait_job(project, job["id"])["status"], "succeeded")
            self.assertEqual(self.request(base + "/problem")["snapshot"]["document_ids"],
                             [document_id])
        finally:
            release.set()

    def test_sensitive_endpoints_need_login_and_secure_cookie_is_optional(self):
        project = self.request("/api/projects", {"name": "受保护"})["id"]
        self.cookie = ""
        for path, method in [("/api/model-settings", "GET"),
                             ("/api/model-settings", "POST"),
                             (f"/api/projects/{project}", "DELETE")]:
            self.assertEqual(self.raw_request(path, method=method)[0], 401)
        self.server.settings = replace(self.server.settings, cookie_secure=True)
        self.request("/api/auth/login", {"key": "test-access"})
        self.assertIn("; Secure", self.response_headers)

    def test_document_delete_method_is_rejected(self):
        project = self.request("/api/projects", {"name": "方法检查"})["id"]
        self.assertEqual(self.raw_request(f"/api/projects/{project}/documents", method="DELETE")[0],
                         405)

    def test_reading_errors_block_solving_before_model_call(self):
        project, result = self.setup_problem()
        reading = result["snapshot"]["reading"]
        facts = reading["problem"]["facts"]
        facts[0]["value"] = "72000"
        revision = self.request(f"/api/projects/{project}/revisions", {
            "expected_version": 1, "note": "测试来源不支持的金额",
            "reading": {"title": "错误值", "facts": facts, "rules": [], "issues": []},
        })
        self.assertTrue(any(i["severity"] == "error" for i in revision["reading"]["issues"]))
        calls = len(self.llm.calls)
        with self.assertRaises(HTTPError) as invalid:
            self.request(f"/api/projects/{project}/solve", {"version": revision["version"]})
        self.assertEqual(invalid.exception.code, 409)
        self.assertEqual(len(self.llm.calls), calls)
