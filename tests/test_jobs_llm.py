import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from erp_agent.adapters.jobs import ThreadJobRunner
from erp_agent.adapters.llm import CompatibleLLMClient
from erp_agent.adapters.model_settings import ModelSettingsService
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import READING_SCHEMA, dumps, loads
from erp_agent.application.ports import LLMRequest
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError


class LLMTests(unittest.TestCase):
    def test_structured_request_and_response(self):
        settings = Settings(llm_api_key="private", llm_model="configured-model")
        seen = []

        class Opener:
            def open(self, req, timeout):
                seen.append(req)
                return io.BytesIO(dumps({"model": "returned-model", "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
                ]}).encode())

        with patch("erp_agent.adapters.llm.build_opener", return_value=Opener()):
            result = CompatibleLLMClient(settings).generate(
                LLMRequest("system", "user", "test", dumps(READING_SCHEMA)))
        self.assertEqual(result.model, "returned-model")
        payload = loads(seen[0].data.decode())
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["model"], "configured-model")
        self.assertNotIn("private", repr(settings))

    def test_http_error_does_not_leak_response(self):
        class Opener:
            def open(self, req, timeout):
                raise HTTPError(req.full_url, 401, "private-secret", None, None)
        with patch("erp_agent.adapters.llm.build_opener", return_value=Opener()):
            with self.assertRaises(ERPError) as caught:
                CompatibleLLMClient(Settings(llm_api_key="private", llm_model="model")).generate(
                    LLMRequest("system", "user", "test", "{}"))
        self.assertNotIn("private", caught.exception.message)

    def test_missing_key_and_input_limit(self):
        for settings, code in [
            (Settings(), "llm_not_configured"),
            (Settings(llm_api_key="key", llm_model="model", llm_max_input_chars=2), "llm_input_limit"),
        ]:
            with self.assertRaises(ERPError) as caught:
                CompatibleLLMClient(settings).generate(LLMRequest("system", "user", "test", "{}"))
            self.assertEqual(caught.exception.code, code)

    def test_insecure_remote_url_rejected(self):
        with self.assertRaises(ValueError):
            Settings(llm_base_url="http://remote.example/v1")
        self.assertEqual(Settings(llm_base_url="http://localhost:3000/v1").llm_timeout, 90)

    def test_model_update_changes_subsequent_request_and_uses_private_file(self):
        with tempfile.TemporaryDirectory(prefix="erp-model-") as directory:
            service = ModelSettingsService(Settings(data_dir=Path(directory)))
            service.update({"model": "new-model", "api_key": "new-secret",
                            "base_url": "https://example.com/v1"})
            seen = []

            class Opener:
                def open(self, request, timeout):
                    seen.append(request)
                    return io.BytesIO(dumps({"choices": [
                        {"finish_reason": "stop", "message": {"content": "{}"}}
                    ]}).encode())

            with patch("erp_agent.adapters.llm.build_opener", return_value=Opener()):
                service.client.generate(LLMRequest("system", "user", "test", "{}"))
            self.assertEqual(seen[0].full_url, "https://example.com/v1/chat/completions")
            self.assertEqual(loads(seen[0].data.decode())["model"], "new-model")
            self.assertEqual(seen[0].get_header("Authorization"), "Bearer new-secret")
            self.assertEqual(service.path.stat().st_mode & 0o777, 0o600)
            before = service.path.read_bytes()
            for data in [{"base_url": "https:///v1"}, {"timeout": 0}, {"format": "unknown"}]:
                with self.assertRaises(ERPError):
                    service.update(data)
                self.assertEqual(service.path.read_bytes(), before)


class JobTests(unittest.TestCase):
    def test_cancel_and_needs_input(self):
        with tempfile.TemporaryDirectory(prefix="erp-jobs-") as directory:
            repository = SQLiteRepository(Path(directory) / "db.sqlite3")
            runner = ThreadJobRunner(repository)
            entered, release = threading.Event(), threading.Event()

            def operation(control):
                entered.set()
                release.wait(2)
                control.checkpoint("checking")
                return {"ok": True}

            def wait(job_id):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = repository.get("job", "a" * 32, job_id)
                    if job["status"] not in {"queued", "running"}:
                        return job
                    time.sleep(0.01)
                self.fail("任务未结束")

            try:
                job = runner.submit("a" * 32, "test", operation)
                self.assertTrue(entered.wait(2))
                runner.cancel("a" * 32, job["id"])
                release.set()
                self.assertEqual(wait(job["id"])["status"], "cancelled")
                another = runner.submit("a" * 32, "test", lambda control: {"needs_input": True})
                self.assertEqual(wait(another["id"])["status"], "needs_input")
            finally:
                release.set()
                runner.close()
