"""分段修正与工具往返不能挤占兄弟任务；总额度和截止时间仍有硬边界。"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.planning_budget import PlanningCallLimits, allocate_planning_budgets
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.ports import TaskContext
from erp_agent.application.task_budget import followup_model_call, model_call, task_budget
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from tests.helpers import Control
from tests.test_planning_batches import cost
from tests.test_planning_pipeline import PlannerClient, ToolChatModel, tool_reply
from tests.test_planning_windows import long_snapshot


class PlanningCallBudgetTests(unittest.TestCase):
    def test_two_windows_can_both_repair_after_eight_total_calls(self):
        class TwoRepairs(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                material = loads(request.user)
                periods = material.get("assigned_periods", [])
                if periods and periods[0] in (1, 5) and "diagnostics" not in material:
                    data = loads(response.content)
                    data["periods"][str(periods[0])][0]["details"] = {"target_entity_id": "company"}
                    return replace(response, content=dumps(data))
                return response

        client, control = TwoRepairs(), Control()
        plan = ParallelPlanningGenerator(client, control).propose(long_snapshot()).baseline
        self.assertEqual(len(plan.actions), 16)
        self.assertEqual(len(client.calls), 9)
        self.assertEqual(task_budget(control, TaskContext("solver", "Solver", "", "")).calls, 9)

    def test_native_batches_and_two_repairs_fit_individual_and_workflow_budgets(self):
        class Native(PlannerClient):
            def __init__(self):
                super().__init__()
                self.models = []

            def generate(self, request):
                response = super().generate(request)
                material = loads(request.user)
                periods = material.get("assigned_periods", [])
                data = loads(response.content)
                replies = []
                if periods and "diagnostics" not in material:
                    start = periods[0]
                    calculation = cost(f"w{start}-cost")
                    calculation["period"] = start
                    replies = [
                        tool_reply("erp_calculate_batch", {"calculations": [calculation]}),
                        tool_reply("planning_lookup", {"kind": "facts", "ids": ["cash"]}),
                    ]
                    if start in (1, 5):
                        data["periods"][str(start)][0]["details"] = {"target_entity_id": "company"}
                replies.append(tool_reply(request.schema_name, data))
                model = ToolChatModel(replies=replies)
                self.models.append(model)
                return LangChainLLMClient(
                    Settings(llm_model="test", llm_api_key="fake"), lambda _: model
                ).generate(request)

        client, control = Native(), Control()
        result = ParallelPlanningGenerator(client, control).propose(long_snapshot())
        root = task_budget(control, TaskContext("solver", "Solver", "", ""))
        self.assertEqual(len(result.baseline.preparation.calculations), 4)
        self.assertEqual(sum(m.calls for m in client.models), 17)
        self.assertEqual(root.calls, 17)
        self.assertEqual(root.max_calls, 32)
        for n in range(4):
            child = task_budget(
                control, TaskContext(f"solver:synthesis:window:{n + 1}", "Solver", "", "")
            )
            self.assertLessEqual(child.calls, child.max_calls)

    def test_allocations_prevent_one_child_from_spending_sibling_calls(self):
        control = Control()
        budgets = allocate_planning_budgets(control, PlanningCallLimits(), 4, 0)
        self.assertLessEqual(sum(b.max_calls for b in budgets.values()), 32)
        self.assertTrue(all(b.max_calls <= 8 for b in budgets.values()))
        first = budgets["solver:operations"]
        for _ in range(first.max_calls):
            first.reserve()
        spent = first.parent.calls
        with self.assertRaises(ERPError):
            first.reserve()
        self.assertEqual(first.parent.calls, spent)
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(
                executor.map(lambda b: b.reserve(), [b for b in budgets.values() if b is not first])
            )
        self.assertEqual(first.parent.calls, spent + 6)

    def test_nested_wrappers_and_native_followups_charge_exactly_once(self):
        budgets = allocate_planning_budgets(Control(), PlanningCallLimits(), 4, 0)
        child = budgets["solver:synthesis:window:2"]
        root = child.parent
        with model_call(root), model_call(child), model_call(child):
            self.assertEqual((root.calls, child.calls), (1, 1))
            followup_model_call()
            self.assertEqual((root.calls, child.calls), (2, 2))

    def test_scheduler_cannot_reset_reserved_quota_to_its_generic_default(self):
        control = Control()
        budgets = allocate_planning_budgets(control, PlanningCallLimits(), 4, 0)
        child = budgets["solver:synthesis:window:2"]
        maximum, deadline = child.max_calls, child.deadline
        actual = task_budget(
            control, TaskContext("solver:synthesis:window:2", "Solver", "", "solver"), 8, 900
        )
        self.assertIs(actual, child)
        self.assertEqual((child.max_calls, child.deadline), (maximum, deadline))

    def test_global_deadline_is_not_restarted_for_a_later_window(self):
        budgets = allocate_planning_budgets(Control(), PlanningCallLimits(), 4, 0)
        child = budgets["solver:synthesis:window:2"]
        child.parent.deadline = 0
        with self.assertRaises(ERPError) as caught:
            child.reserve()
        self.assertEqual(caught.exception.code, "task_deadline")
        self.assertEqual(child.calls, 0)

    def test_explicit_strict_total_is_retained_and_inadequate_budget_fails_before_calls(self):
        class Strict(PlannerClient):
            settings = Settings(solver_max_model_calls=6)

        client = Strict()
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control()).propose(long_snapshot())
        self.assertEqual(caught.exception.code, "planning.call_configuration")
        self.assertFalse(client.calls)
        budgets = allocate_planning_budgets(Control(), PlanningCallLimits(workflow_calls=8), 4, 0)
        self.assertEqual(sum(b.max_calls for b in budgets.values()), 8)

    def test_budget_matching_calls_receive_their_own_reserved_allocation(self):
        budgets = allocate_planning_budgets(Control(), PlanningCallLimits(), 4, 4)
        self.assertGreaterEqual(budgets["solver-budget"].max_calls, 4)
        self.assertLessEqual(sum(b.max_calls for b in budgets.values()), 32)

    def test_reallocation_never_resets_consumed_calls(self):
        control = Control()
        first = allocate_planning_budgets(control, PlanningCallLimits(), 4, 0)
        first["solver:finance"].reserve()
        second = allocate_planning_budgets(control, PlanningCallLimits(), 4, 0)
        self.assertIs(first["solver:finance"], second["solver:finance"])
        self.assertEqual(second["solver:finance"].calls, 1)

    def test_solver_limit_is_configurable_and_does_not_change_reader_limit(self):
        with patch.dict(os.environ, {"ERP_SOLVER_MAX_MODEL_CALLS": "20"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.solver_max_model_calls, 20)
        self.assertEqual(settings.task_max_model_calls, 8)
        for value in (0, 257):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(solver_max_model_calls=value)
