import threading
import unittest

from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse, TaskContext
from erp_agent.application.solver import CollaborativeStrategyGenerator
from erp_agent.domain.errors import ERPError
from tests.helpers import Control, proposal_json, snapshot


class CollaborativeTests(unittest.TestCase):
    def test_specialists_work_concurrently_and_coordinator_receives_their_evidence(self):
        barrier = threading.Barrier(2)
        calls = []

        class Client:
            configured = True

            def generate(self, request):
                calls.append(request)
                if request.schema_name == "erp_solver_specialist":
                    barrier.wait(2)
                    role = loads(request.user)["role"]
                    return LLMResponse(
                        dumps(
                            {
                                "recommendations": [
                                    {
                                        "entity_id": "company",
                                        "periods": "1–2",
                                        "recommendation": role,
                                        "fact_ids": ["cash"],
                                        "rule_ids": [],
                                    }
                                ],
                                "dependencies": ["现金先于采购"],
                                "risks": [],
                            }
                        ),
                        role,
                    )
                material = loads(request.user)
                assert len(material["collaboration"]) == 2
                assert all(
                    item["recommendations"][0]["fact_ids"] == ["cash"]
                    for item in material["collaboration"]
                )
                return LLMResponse(proposal_json(), "coordinator")

        proposal = CollaborativeStrategyGenerator(Client(), Control()).propose(snapshot())
        self.assertEqual(len(calls), 3)
        self.assertEqual(proposal.model, "coordinator")
        self.assertEqual(proposal.parameters, ())

    def test_unknown_references_fail_before_coordinator(self):
        class Client:
            configured = True

            def generate(self, request):
                assert request.schema_name == "erp_solver_specialist"
                return LLMResponse(
                    dumps(
                        {
                            "recommendations": [
                                {
                                    "entity_id": "company",
                                    "periods": "1",
                                    "recommendation": "采购",
                                    "fact_ids": ["invented"],
                                    "rule_ids": [],
                                }
                            ],
                            "dependencies": [],
                            "risks": [],
                        }
                    ),
                    "test",
                )

        with self.assertRaises(ERPError) as caught:
            CollaborativeStrategyGenerator(Client(), Control()).propose(snapshot())
        self.assertEqual(caught.exception.code, "solver_source")

    def test_failed_specialist_keeps_successful_tasks_and_retry_restores_from_memory(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from erp_agent.adapters.memory import SQLiteMemoryService

        calls = []

        class Client:
            configured = True
            failing = True

            def generate(self, request):
                data = loads(request.user)
                calls.append(data.get("role", "coordinator"))
                if data.get("role") == "finance" and self.failing:
                    raise ERPError("llm_gateway_timeout", "HTTP 504", 504)
                if request.schema_name == "erp_solver_specialist":
                    return LLMResponse(
                        dumps({"recommendations": [], "dependencies": [], "risks": []}), "test"
                    )
                return LLMResponse(proposal_json(), "test")

        with (
            TemporaryDirectory() as directory,
            patch("erp_agent.application.llm_recovery.time.sleep"),
            patch(
                "erp_agent.application.llm_recovery.wait_random_exponential",
                return_value=lambda state: 0,
            ),
        ):
            path = Path(directory) / "memory.sqlite3"
            SQLiteRepository(path)
            memory = SQLiteMemoryService(path)
            problem = snapshot()
            client = Client()
            scope = memory.scope(problem.project_id, problem.version, "solver-test")
            with self.assertRaises(ERPError):
                CollaborativeStrategyGenerator(client, Control(), scope).propose(problem)
            self.assertEqual(calls.count("operations"), 1)
            self.assertEqual(calls.count("finance"), 2)
            self.assertNotIn("coordinator", calls)
            memory.close()
            SQLiteRepository(path)
            memory = SQLiteMemoryService(path)
            try:
                client.failing = False
                scope = memory.scope(problem.project_id, problem.version, "solver-test")
                result = CollaborativeStrategyGenerator(client, Control(), scope).propose(problem)
                self.assertEqual(result.model, "test")
                self.assertEqual(calls.count("operations"), 1)
                self.assertEqual(calls.count("finance"), 3)
                self.assertEqual(calls.count("coordinator"), 1)
            finally:
                memory.close()

    def test_budget_timeout_splits_only_template_then_resumes_values_without_rebuilding_plan(self):
        from dataclasses import replace
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from erp_agent.adapters.memory import SQLiteMemoryService
        from erp_agent.domain.models import (
            BudgetCell,
            BudgetSheet,
            BudgetTemplate,
            GenerationConfig,
            Issue,
            PlanningContext,
            SourceDocument,
            TemplateIssueReview,
            TemplateReading,
            TemplateSheetReading,
        )

        calls = []
        template = BudgetTemplate(
            SourceDocument("template", "预算.xlsx", "hash", 1, "xlsx"),
            tuple(
                BudgetSheet(
                    name,
                    16 if name == "生产" else 3,
                    2,
                    (
                        BudgetCell("A1", "第一期"),
                        BudgetCell("A2", "借款"),
                        BudgetCell("B2", ""),
                        BudgetCell("B3", "=SUM(B2)", formula=True),
                    )
                    + (
                        (BudgetCell("A12", "采购"), BudgetCell("B12", "")) if name == "生产" else ()
                    ),
                )
                for name in ("资金", "生产")
            ),
        )
        issue = Issue("template.period", "warning", "template", "第一期？")
        reading = TemplateReading(
            "template",
            "hash",
            tuple(TemplateSheetReading(s.name, "预算", "第1期") for s in template.sheets),
            (issue,),
            reviewed=True,
            issue_reviews=(TemplateIssueReview(issue, "第一期"),),
        )
        problem = replace(snapshot(), template_readings=(reading,))
        context = PlanningContext(GenerationConfig(template_ids=("template",)), (template,))

        class Client:
            configured = True
            failing = True

            def generate(self, request):
                data = loads(request.user)
                calls.append((request.schema_name, data))
                if request.schema_name == "erp_solver_specialist":
                    self_sheets = data.get("budget_templates", [{}])[0].get("sheets", [])
                    if data["role"] == "budget" and len(self_sheets) > 1:
                        raise ERPError("llm_gateway_timeout", "HTTP 504", 504)
                    return LLMResponse(
                        dumps({"recommendations": [], "dependencies": [], "risks": []}), "helper"
                    )
                if request.schema_name == "erp_strategy":
                    assert "=SUM" not in request.user
                    assert data["template_readings"][0]["issue_reviews"][0]["answer"] == "第一期"
                    return LLMResponse(proposal_json(), "coordinator")
                assert request.schema_name == "erp_solver_budget"
                name = data["template"]["sheet"]["name"]
                window = data["template"].get("row_window", [1, 16])
                if name == "生产" and window[1] >= 12 and self.failing:
                    raise ERPError("llm_gateway_timeout", "HTTP 504", 504)
                cell = "B12" if window[0] > 2 else "B2"
                return LLMResponse(
                    dumps(
                        {
                            "budget_items": [
                                {
                                    "template_id": "template",
                                    "sheet": name,
                                    "cell": cell,
                                    "label": "借款",
                                    "period": 1,
                                    "unit": "元",
                                    "value": "10",
                                    "action_indices": [0],
                                    "explanation": "引用第1期操作0",
                                }
                            ]
                        }
                    ),
                    "budget",
                )

        with (
            TemporaryDirectory() as directory,
            patch(
                "erp_agent.application.llm_recovery.wait_random_exponential",
                return_value=lambda state: 0,
            ),
        ):
            SQLiteRepository(Path(directory) / "memory.sqlite3")
            memory = SQLiteMemoryService(Path(directory) / "memory.sqlite3")
            try:
                scope = memory.scope(problem.project_id, problem.version, "test")
                client = Client()
                with self.assertRaises(ERPError):
                    CollaborativeStrategyGenerator(client, Control(), scope).propose(
                        problem, context
                    )
                count = len(calls)
                client.failing = False
                result = CollaborativeStrategyGenerator(client, Control(), scope).propose(
                    problem, context
                )
                self.assertEqual(len(result.baseline.budget_items), 3)
                self.assertEqual(len(calls), count + 1)
                self.assertEqual(calls[-1][0], "erp_solver_budget")
                self.assertEqual({i.sheet for i in result.baseline.budget_items}, {"资金", "生产"})
                self.assertEqual([a.period for a in result.baseline.actions], [1, 2])
                full = [
                    d
                    for _, d in calls
                    if d.get("role") == "budget" and len(d["budget_templates"][0]["sheets"]) > 1
                ]
                self.assertEqual(len(full), 2)
                self.assertTrue(
                    all(
                        "blank_ranges" not in s and not any("SUM" in str(c) for c in s["cells"])
                        for d in full
                        for s in d["budget_templates"][0]["sheets"]
                    )
                )
                self.assertEqual(sum(name == "erp_strategy" for name, _ in calls), 1)
                windows = [
                    d["template"].get("row_window")
                    for name, d in calls
                    if name == "erp_solver_budget" and d["template"]["sheet"]["name"] == "生产"
                ]
                self.assertEqual(windows.count(None), 2)
                self.assertEqual(windows.count([1, 8]), 1)
                self.assertEqual(windows.count([9, 16]), 3)
            finally:
                memory.close()

    def test_solver_dispatches_specialists_once_using_priority_and_capacity(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from erp_agent.adapters.memory import SQLiteMemoryService
        from erp_agent.application.memory_client import MemoryLLMClient
        from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
        from erp_agent.application.ports import ModelBinding
        from erp_agent.application.task_reporting import TaskLLMClient
        from tests.test_model_scheduling import choice

        calls = []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                calls.append((self.name, request))
                data = loads(request.user)
                if request.schema_name == "erp_solver_dispatch":
                    assert "经营规划主 Agent" in request.system
                    assert data["worker_concurrency"] == 2
                    return LLMResponse(
                        dumps(
                            {
                                "tasks": [
                                    {
                                        "task_id": task["task_id"],
                                        "difficulty": "moderate",
                                        "assignments": [
                                            {
                                                "preset_id": "helper",
                                                "reason": "适合协作规划，主模型保留统筹。",
                                            }
                                        ],
                                        "summary": "依难度和预计容量安排协作模型。",
                                    }
                                    for task in data["tasks"]
                                ]
                            }
                        ),
                        self.name,
                    )
                if request.schema_name == "erp_model_dispatch":
                    return choice("main")
                if request.schema_name == "erp_solver_specialist":
                    return LLMResponse(
                        dumps({"recommendations": [], "dependencies": [], "risks": []}), self.name
                    )
                return LLMResponse(proposal_json(), self.name)

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 1, "max_input_chars": 100000},
                Client(name),
            )
            for name, priority in [("main", 100), ("helper", 90)]
        )
        pool = ScheduledLLMClient(models, ModelCapacity())
        with TemporaryDirectory() as directory:
            SQLiteRepository(Path(directory) / "memory.sqlite3")
            memory = SQLiteMemoryService(Path(directory) / "memory.sqlite3")
            try:
                scope = memory.scope(snapshot().project_id, 1, "test")
                wrapped = MemoryLLMClient(
                    TaskLLMClient(
                        pool, Control(), TaskContext("solver", "Solver", "生成方案", "测试")
                    ),
                    scope,
                    "经营方案",
                )
                CollaborativeStrategyGenerator(wrapped, Control(), scope).propose(snapshot())
                self.assertEqual(
                    sum(request.schema_name == "erp_solver_dispatch" for _, request in calls), 1
                )
                self.assertTrue(
                    all(
                        name == "helper"
                        for name, request in calls
                        if request.schema_name == "erp_solver_specialist"
                    )
                )
                self.assertEqual(
                    sum(request.schema_name == "erp_model_dispatch" for _, request in calls), 1
                )
                self.assertEqual(pool.capacity.used, {"main": 0, "helper": 0})
            finally:
                memory.close()
