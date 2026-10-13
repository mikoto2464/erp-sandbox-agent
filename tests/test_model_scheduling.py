import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from erp_agent.adapters.jobs import JobControl
from erp_agent.adapters.model_settings import ModelSettingsService
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import decimal, dumps, loads
from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
from erp_agent.application.ports import LLMRequest, LLMResponse, ModelBinding, TaskContext
from erp_agent.application.task_reporting import TaskLLMClient
from erp_agent.config import Settings
from erp_agent.domain.errors import Cancelled, ERPError


class ModelSchedulingTests(unittest.TestCase):
    def test_parallel_models_share_capacity_and_cancellation_releases_waiters(self):
        gate = threading.Barrier(2, timeout=2)
        release, waiting, entered = threading.Event(), threading.Event(), threading.Event()
        capacity = ModelCapacity()
        capacity.configure({"primary": 1, "simple": 1})
        called, messages = [], []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                called.append(self.name)
                messages.append(request.system)
                if len(called) == 2:
                    entered.set()
                gate.wait()
                if not release.wait(3):
                    raise AssertionError("模型未释放")
                return LLMResponse("{}", self.name)

        def binding(name, priority):
            return ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 1, "max_input_chars": 15000},
                Client(name),
            )

        first = ScheduledLLMClient((binding("primary", 100), binding("simple", 50)), capacity)
        # 不同任务/冻结池共用额度，第三个请求不能进入已满模型。
        second = ScheduledLLMClient(first.models, capacity)
        cancelled = threading.Event()

        class Control:
            def checkpoint(self, stage, completed=0):
                pass

            def check_cancelled(self):
                if cancelled.is_set():
                    raise Cancelled()

            def report_task(self, key, changes):
                if changes["status"] == "waiting":
                    waiting.set()

        request = LLMRequest("职责", "{}", "test", "{}")
        task = TaskContext("blocked", "Reader", "规则整理", "原文")
        with ThreadPoolExecutor(max_workers=3) as executor:
            a = executor.submit(first.generate, request)
            b = executor.submit(TaskLLMClient(second, Control(), task, "simple").generate, request)
            try:
                # 两个 SDK 调用同时进入才会越过 Barrier。
                self.assertTrue(entered.wait(2))
                waiting.clear()
                c = executor.submit(TaskLLMClient(second, Control(), task).generate, request)
                self.assertTrue(waiting.wait(2))
                cancelled.set()
                with self.assertRaises(Cancelled):
                    c.result(timeout=2)
                self.assertCountEqual(called, ["primary", "simple"])
            finally:
                release.set()
            a.result(timeout=2)
            b.result(timeout=2)
        self.assertEqual(capacity.used, {"primary": 0, "simple": 0})
        self.assertTrue(all("可用模型" in s for s in messages))

    def test_preset_pool_is_opt_in_frozen_and_migrates_old_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), llm_model="main", llm_api_key="private")
            service = ModelSettingsService(settings)
            auxiliary = service.create_preset(
                {
                    "name": "轻量",
                    "model": "small",
                    "api_key": "secret",
                    "priority": 50,
                    "max_parallel": 1,
                }
            )
            self.assertEqual(len(service.capture_pool().configuration["model_pool"]), 1)
            service.update_preset(
                auxiliary["id"], {"expected_revision": 1, "scheduling_enabled": True}
            )
            frozen = service.capture_pool()
            self.assertEqual(len(frozen.configuration["model_pool"]), 2)
            service.update_preset(
                auxiliary["id"], {"expected_revision": 2, "priority": 300, "model": "changed"}
            )
            self.assertEqual(service.capture_pool().preset_id, auxiliary["id"])
            self.assertEqual(frozen.preset_id, "default")
            service.delete_preset(auxiliary["id"], 3)
            calls = []
            with patch(
                "erp_agent.adapters.model_settings.LangChainLLMClient.generate",
                lambda client, req: (
                    calls.append(client.settings.llm_model) or LLMResponse("{}", "m")
                ),
            ):
                frozen.client.generate(LLMRequest("任务", "{}", "test", "{}", complexity="simple"))
            self.assertEqual(calls, ["small"])
            self.assertNotIn("secret", dumps(frozen.metadata()))
            saved = loads(service.path.read_text())
            saved["schema_version"] = 1
            for preset in saved["presets"]:
                for key in ("priority", "max_parallel", "scheduling_enabled"):
                    preset.pop(key)
            service.path.write_text(dumps(saved))
            reloaded = ModelSettingsService(settings)
            self.assertEqual(reloaded.public()["max_parallel"], 2)
            self.assertEqual(loads(service.path.read_text())["schema_version"], 2)
            for change in ({"max_parallel": 0}, {"priority": 1001}, {"scheduling_enabled": "yes"}):
                with self.assertRaises(ERPError):
                    reloaded.update(change)

    def test_concurrent_child_updates_and_restart_leave_no_running_children(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            repo = SQLiteRepository(path)
            repo.put("job", "p", "j", {"id": "j", "project_id": "p", "status": "running"})
            control = JobControl(repo, "p", "j", threading.Event(), threading.RLock())
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(
                    executor.map(
                        lambda i: control.report_task(
                            str(i),
                            {"status": "running", "agent": "Reader", "object": "文件行" + str(i)},
                        ),
                        range(20),
                    )
                )
            record = repo.get("job", "p", "j")
            self.assertEqual(len(record["tasks"]), 20)
            restarted = SQLiteRepository(path).get("job", "p", "j")
            self.assertEqual(restarted["status"], "failed")
            self.assertTrue(all(t["status"] == "failed" for t in restarted["tasks"]))

    def test_decimal_trailing_zero_normalization_is_exact(self):
        self.assertEqual(decimal("0.050000000000000000000"), decimal("0.05"))
        with self.assertRaises(ERPError):
            decimal("0.050000000000000003")


if __name__ == "__main__":
    unittest.main()
