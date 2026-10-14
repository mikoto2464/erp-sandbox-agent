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
from erp_agent.application.input_sources import text_source
from erp_agent.application.model_dispatch import table_dispatch_key, task_material
from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
from erp_agent.application.ports import LLMRequest, LLMResponse, ModelBinding, TaskContext
from erp_agent.application.reader import StructuredReader
from erp_agent.application.task_reporting import TaskLLMClient
from erp_agent.config import Settings
from erp_agent.domain.errors import Cancelled, ERPError
from tests.helpers import Control


def choice(preset_id, difficulty="complex", alternative=None):
    assignments = [{"preset_id": preset_id, "reason": "综合实际任务内容、优先级及当前额度。"}]
    if alternative:
        assignments.append(
            {"preset_id": alternative, "reason": "该模型能力也满足此任务，可作为替代。"}
        )
    return LLMResponse(
        dumps(
            {
                "difficulty": difficulty,
                "assignments": assignments,
                "summary": "评估任务内容并结合模型负载分配。",
                "reuse_scope": "none",
            }
        ),
        "main",
    )


class ModelSchedulingTests(unittest.TestCase):
    def test_reader_dispatches_all_chapters_once_and_workers_follow_main_agent(self):
        dispatches, workers = [], []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                if request.schema_name == "erp_reader_dispatch":
                    material = loads(request.user)
                    dispatches.append(material)
                    return LLMResponse(
                        dumps(
                            {
                                "tasks": [
                                    {
                                        "task_id": task["task_id"],
                                        "difficulty": "simple",
                                        "assignments": [
                                            {
                                                "preset_id": ("ds", "other", "ds")[i],
                                                "reason": "按章节格式与预计负载分配常规整理。",
                                            }
                                        ],
                                        "summary": "主模型负责统筹，协作模型整理章节。",
                                    }
                                    for i, task in enumerate(material["tasks"])
                                ]
                            }
                        ),
                        self.name,
                    )
                if request.schema_name == "erp_model_dispatch":
                    raise AssertionError("正常章节不应逐次重复调度")
                workers.append(self.name)
                return LLMResponse(
                    dumps({"title": "题目", "facts": [], "rules": [], "issues": []}), self.name
                )

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 2, "max_input_chars": 100000},
                Client(name),
            )
            for name, priority in [("main", 100), ("other", 90), ("ds", 80)]
        )
        scheduled = ScheduledLLMClient(models, ModelCapacity(), strategy="agent")
        document = text_source(
            "赛题", "一、初始资本\n现金100元\n二、市场\n产品A\n三、融资\n利率5%", "manual_"
        )
        StructuredReader(scheduled).read((document,), Control())
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(len(dispatches[0]["tasks"]), 3)
        self.assertEqual({m["id"] for m in dispatches[0]["models"]}, {"main", "ds", "other"})
        self.assertCountEqual(workers, ["ds", "other", "ds"])
        self.assertEqual(scheduled.capacity.used, {"main": 0, "ds": 0, "other": 0})

    def test_planned_reader_uses_idle_authorized_model_and_releases_capacity(self):
        entered = threading.Barrier(2)
        workers = []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                if request.schema_name == "erp_reader_dispatch":
                    return LLMResponse(
                        dumps(
                            {
                                "tasks": [
                                    {
                                        "task_id": task["task_id"],
                                        "difficulty": "simple",
                                        "assignments": [
                                            {"preset_id": name, "reason": "具备常规整理能力。"}
                                            for name in ("main", "helper")
                                        ],
                                        "summary": "两个模型皆可处理，按当前占用执行。",
                                    }
                                    for task in loads(request.user)["tasks"]
                                ]
                            }
                        ),
                        self.name,
                    )
                workers.append(self.name)
                entered.wait(timeout=2)
                return LLMResponse("{}", self.name)

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 2, "max_input_chars": 100000},
                Client(name),
            )
            for name, priority in [("main", 100), ("helper", 80)]
        )
        scheduled = ScheduledLLMClient(models, ModelCapacity(), strategy="agent")
        requests = tuple(
            LLMRequest(
                "整理数据",
                "{}",
                "erp_reading",
                "{}",
                task=TaskContext(str(i), "Reader", "读题", "章节"),
            )
            for i in range(2)
        )
        scheduled.plan_reading(requests, Control(), 2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(scheduled.generate, requests))
        self.assertCountEqual(workers, ["main", "helper"])
        self.assertEqual(scheduled.capacity.used, {"main": 0, "helper": 0})

    def test_incomplete_initial_reading_plan_is_reported_as_fallback(self):
        events = []

        class ReportingControl(Control):
            def report_task(self, key, changes):
                events.append(changes)

        class Client:
            configured = True

            def generate(self, request):
                return LLMResponse(dumps({"tasks": []}), "main")

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 1, "max_input_chars": 100000},
                Client(),
            )
            for name, priority in [("main", 100), ("helper", 80)]
        )
        scheduled = ScheduledLLMClient(models, ModelCapacity(), strategy="agent")
        request = LLMRequest(
            "整理数据", "{}", "erp_reading", "{}", task=TaskContext("one", "Reader", "读题", "章节")
        )
        scheduled.plan_reading((request,), ReportingControl(), 2)
        self.assertTrue(scheduled._reading_plan["one"][0].fallback)
        self.assertTrue(any(e.get("dispatch_fallback") and e["status"] == "failed" for e in events))
        self.assertEqual(scheduled.capacity.used, {"main": 0})

    def test_optional_agent_authorizes_same_table_reuse(self):
        dispatches, workers = [], []

        class Client:
            configured = True

            def generate(self, request):
                if request.schema_name == "erp_model_dispatch":
                    dispatches.append(loads(request.user))
                    response = loads(choice("helper", "simple").content)
                    response["reuse_scope"] = "same_table"
                    return LLMResponse(dumps(response), "main")
                workers.append(request)
                return LLMResponse("{}", "helper")

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 1, "max_input_chars": 15000},
                Client(),
            )
            for name, priority in [("main", 200), ("helper", 100)]
        )
        scheduled = ScheduledLLMClient(models, ModelCapacity(), strategy="agent", task_max_calls=16)
        requests = []
        for row in range(3):
            document = {
                "document_id": "d",
                "blocks": [
                    {
                        "block_index": 1,
                        "table": [[row, 0, str(10 + row), f"e{row}", 1, "", "General"]],
                    }
                ],
                "table_context_groups": [{"column_header_ids": ["header"]}],
                "file_notes": [],
            }
            request = LLMRequest(
                "读题",
                dumps({"role": "reading", "documents": [document]}),
                "erp_reading",
                "{}",
                task=TaskContext(str(row), "Reader", "读题", "详单"),
            )
            self.assertIsNotNone(table_dispatch_key(request))
            requests.append(request)
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(scheduled.generate, requests))
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(len(workers), 3)
        for _ in range(6):
            scheduled.generate(request)
        self.assertEqual(len(dispatches), 2)
        material = loads(request.user)
        material["error"] = "单位需修正"
        repaired = LLMRequest("读题", dumps(material), "erp_reading", "{}", task=request.task)
        self.assertIsNone(table_dispatch_key(repaired))
        scheduled.generate(repaired)
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(scheduled.capacity.used, {"main": 0, "helper": 0})

    def test_task_sample_contains_content_and_excludes_previous_response(self):
        document = {
            "document_id": "doc",
            "filename": "规则.xls",
            "format": "xls",
            "warnings": [],
            "cell_layout": ["row", "column", "text", "evidence_id"],
            "blocks": [{"table": [[1, 1, "跨表关联", "ref1"], [1, 2, "50%", "ref2"]]}],
            "table_contexts": {"ref2": {"column_header_ids": ["header"]}},
            "context_evidence": [["header", "效率%", "表头"]],
        }
        request = LLMRequest(
            "提取实际参数",
            dumps(
                {
                    "documents": [document],
                    "error": "表头口径需重查",
                    "previous_output": "不发送旧输出" * 10000,
                }
            ),
            "reading",
            "{}",
        )
        sample = task_material(request)
        self.assertTrue(sample["has_repair"])
        self.assertIn("跨表关联", sample["content_sample"])
        self.assertIn("效率%", sample["content_sample"])
        self.assertNotIn("不发送旧输出", sample["content_sample"])
        self.assertLessEqual(len(sample["content_sample"]), 2400)

    def test_main_agent_routes_same_role_differently_using_actual_task_and_load(self):
        calls, decisions, events = [], [], []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                if request.schema_name == "erp_model_dispatch":
                    data = loads(request.user)
                    decisions.append(data)
                    complex_task = "跨表口径冲突" in data["task"]["content_sample"]
                    return choice(
                        "main" if complex_task else "helper",
                        "complex" if complex_task else "simple",
                    )
                calls.append(self.name)
                return LLMResponse("{}", self.name)

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {
                    "model": name,
                    "priority": priority,
                    "max_parallel": 1,
                    "max_input_chars": 15000,
                    "capabilities": capability,
                },
                Client(name),
            )
            for name, priority, capability in [
                ("main", 200, "跨表规则推理"),
                ("helper", 100, "标准表格提取"),
            ]
        )
        capacity = ModelCapacity()
        capacity.configure({"main": 1, "helper": 1})
        scheduled = ScheduledLLMClient(models, capacity, strategy="agent")

        class Control:
            def checkpoint(self, stage, completed=0):
                pass

            def report_task(self, task_id, changes):
                events.append(changes)

        for description in ("标准订单表", "跨表口径冲突"):
            task = TaskContext(description, "Reader", "参数提取", "题目表")
            TaskLLMClient(scheduled, Control(), task).generate(
                LLMRequest("按证据提取参数", dumps({"table": description}), "erp_reading", "{}")
            )
        self.assertEqual(calls, ["helper", "main"])
        self.assertTrue(all(d["task"]["role"] == "参数提取" for d in decisions))
        model = decisions[0]["models"][0]
        self.assertEqual((model["priority"], model["max_parallel"], model["in_use"]), (200, 1, 1))
        self.assertEqual(model["available_after_dispatch"], 1)
        self.assertEqual(model["capabilities"], "跨表规则推理")
        self.assertTrue(
            any(e.get("difficulty") == "complex" and e.get("dispatch_reason") for e in events)
        )
        self.assertEqual(capacity.used, {"main": 0, "helper": 0})

    def test_parallel_workers_and_coordinator_share_capacity_and_cancel_waiting_dispatch(self):
        gate = threading.Barrier(2, timeout=3)
        release, entered, waiting = threading.Event(), threading.Event(), threading.Event()
        capacity = ModelCapacity()
        capacity.configure({"main": 1, "helper": 1})
        called, decisions = [], []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                if request.schema_name == "erp_model_dispatch":
                    decisions.append(loads(request.user))
                    return choice("helper" if "first" in request.user else "main")
                called.append(self.name)
                if len(called) == 2:
                    entered.set()
                gate.wait()
                if not release.wait(3):
                    raise AssertionError("模型未释放")
                return LLMResponse("{}", self.name)

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 1, "max_input_chars": 15000},
                Client(name),
            )
            for name, priority in [("main", 200), ("helper", 100)]
        )
        a_client = ScheduledLLMClient(models, capacity, strategy="agent")
        b_client = ScheduledLLMClient(models, capacity, strategy="agent")
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

        with ThreadPoolExecutor(max_workers=3) as executor:
            a = executor.submit(
                a_client.generate, LLMRequest("职责", '{"task":"first"}', "test", "{}")
            )

            # helper 先进入工作，主模型此时能继续为下一任务分配。
            def wait_helper():
                with capacity.condition:
                    return capacity.condition.wait_for(
                        lambda: capacity.used.get("helper") == 1, timeout=2
                    )

            self.assertTrue(wait_helper())
            b = executor.submit(
                b_client.generate, LLMRequest("职责", '{"task":"second"}', "test", "{}")
            )
            try:
                self.assertTrue(entered.wait(2))
                waiting.clear()
                task = TaskContext("blocked", "Reader", "参数提取", "原文")
                c = executor.submit(
                    TaskLLMClient(b_client, Control(), task).generate,
                    LLMRequest("职责", "{}", "test", "{}"),
                )
                self.assertTrue(waiting.wait(2))
                cancelled.set()
                with self.assertRaises(Cancelled):
                    c.result(timeout=2)
                self.assertCountEqual(called, ["main", "helper"])
                self.assertEqual(len(decisions), 2)
            finally:
                release.set()
            a.result(timeout=2)
            b.result(timeout=2)
        self.assertEqual(capacity.used, {"main": 0, "helper": 0})

    def test_dispatcher_rejects_unregistered_model_and_reports_conservative_fallback(self):
        events, workers = [], []

        class Client:
            configured = True

            def generate(self, request):
                if request.schema_name == "erp_model_dispatch":
                    return choice("invented")
                workers.append("main")
                return LLMResponse("{}", "main")

        client = Client()
        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": rank, "max_parallel": 1, "max_input_chars": 15000},
                client,
            )
            for name, rank in [("main", 200), ("helper", 100)]
        )

        class Control:
            def checkpoint(self, stage, completed=0):
                pass

            def report_task(self, key, changes):
                events.append(changes)

        capacity = ModelCapacity()
        request = LLMRequest("职责", "{}", "test", "{}")
        TaskLLMClient(
            ScheduledLLMClient(models, capacity, strategy="agent"),
            Control(),
            TaskContext("f", "Reader", "提取", "题目"),
        ).generate(request)
        self.assertEqual(workers, ["main"])
        self.assertTrue(any(e.get("dispatch_fallback") for e in events))
        self.assertEqual(capacity.used["main"], 0)

    def test_full_model_directory_marks_input_capacity_and_rejects_ineligible_choice(self):
        payloads, calls = [], []

        class Client:
            configured = True

            def generate(self, request):
                if request.schema_name == "erp_model_dispatch":
                    payloads.append(loads(request.user))
                    return choice("small")
                calls.append("main")
                return LLMResponse("{}", "main")

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 1, "max_input_chars": limit},
                Client(),
            )
            for name, priority, limit in [("main", 200, 15000), ("small", 100, 100)]
        )
        scheduler = ScheduledLLMClient(models, ModelCapacity(), strategy="agent")
        scheduler.generate(LLMRequest("任务", dumps({"table": "内容" * 100}), "reading", "{}"))
        self.assertEqual(len(payloads[0]["models"]), 2)
        self.assertFalse(payloads[0]["models"][1]["eligible"])
        self.assertEqual(calls, ["main"])

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
                    "capabilities": "标准表格提取",
                }
            )
            self.assertEqual(len(service.capture_pool().configuration["model_pool"]), 1)
            service.update_preset(
                auxiliary["id"], {"expected_revision": 1, "scheduling_enabled": True}
            )
            frozen = service.capture_pool()
            service.update_preset(
                auxiliary["id"], {"expected_revision": 2, "priority": 300, "model": "changed"}
            )
            self.assertEqual(service.capture_pool().preset_id, auxiliary["id"])
            self.assertEqual(frozen.preset_id, "default")
            service.delete_preset(auxiliary["id"], 3)
            calls = []

            def generate(client, request):
                calls.append((client.settings.llm_model, request.schema_name))
                return (
                    choice(auxiliary["id"])
                    if request.schema_name == "erp_model_dispatch"
                    else LLMResponse("{}", "m")
                )

            with patch("erp_agent.adapters.model_settings.LangChainLLMClient.generate", generate):
                frozen.client.generate(LLMRequest("任务", "{}", "test", "{}"))
            self.assertEqual(calls, [("main", "test")])
            self.assertNotIn("secret", dumps(frozen.metadata()))
            saved = loads(service.path.read_text())
            saved["schema_version"] = 2
            for preset in saved["presets"]:
                preset.pop("capabilities")
            service.path.write_text(dumps(saved))
            reloaded = ModelSettingsService(settings)
            self.assertEqual(reloaded.public()["capabilities"], "")
            self.assertEqual(loads(service.path.read_text())["schema_version"], 3)
            for change in (
                {"max_parallel": 0},
                {"priority": 1001},
                {"scheduling_enabled": "yes"},
                {"capabilities": "x" * 501},
            ):
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
            self.assertEqual(len(repo.get("job", "p", "j")["tasks"]), 20)
            restarted = SQLiteRepository(path).get("job", "p", "j")
            self.assertEqual(restarted["status"], "failed")
            self.assertTrue(all(t["status"] == "failed" for t in restarted["tasks"]))

    def test_decimal_trailing_zero_normalization_is_exact(self):
        self.assertEqual(decimal("0.050000000000000000000"), decimal("0.05"))
        with self.assertRaises(ERPError):
            decimal("0.050000000000000003")


if __name__ == "__main__":
    unittest.main()
