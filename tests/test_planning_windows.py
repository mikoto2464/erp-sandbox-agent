"""分段并行、跨段承诺与 payment 详情契约的回归。"""

import threading
import unittest
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_decisions import compile_decisions, decision_schema
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.planning_windows import period_windows
from erp_agent.application.ports import TaskContext
from erp_agent.application.task_budget import task_budget
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from tests.helpers import Control, snapshot
from tests.test_planning_pipeline import (
    PlannerClient,
    ToolChatModel,
    decision,
    decisions,
    tool_reply,
)


def long_snapshot(years=4, periods=4):
    snap = snapshot()
    return replace(
        snap,
        reading=replace(
            snap.reading,
            problem=replace(snap.reading.problem, years=years, periods_per_year=periods),
        ),
    )


class Memory:
    def __init__(self):
        self.records = {}

    def get(self, key):
        return self.records.get(key)

    def put(self, key, value):
        self.records[key] = value


class WindowPlanningTests(unittest.TestCase):
    def test_parallel_windows_use_two_suitable_models_with_shared_capacity(self):
        from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
        from tests.test_direct_scheduling import binding

        barrier = threading.Barrier(4)

        class WindowClient(PlannerClient):
            def generate(self, request):
                if "assigned_periods" in loads(request.user):
                    barrier.wait(timeout=3)
                return super().generate(request)

        first, second = WindowClient(), WindowClient()
        pool = ScheduledLLMClient(
            (
                binding("first", parallel=2, client=first),
                binding("second", 90, parallel=2, client=second),
            ),
            ModelCapacity(),
        )
        result = ParallelPlanningGenerator(pool, Control()).propose(long_snapshot())
        counts = [
            sum("assigned_periods" in loads(r.user) for r in client.calls)
            for client in (first, second)
        ]
        self.assertEqual(counts, [2, 2])
        self.assertEqual(len(result.baseline.actions), 16)

    def test_isolated_http_sixteen_periods_native_tools_and_fee_repair_fit_eight_calls(self):
        from tests.test_web import WebTests

        case = WebTests("test_upload_read_revise_solve_and_isolation")
        planner = PlannerClient()
        calls = []

        def native_generate(request):
            response = planner.generate(request)
            data = loads(response.content)
            payload = loads(request.user)
            cash = next(
                f["id"] for f in request.tool_context["facts"] if f["key"] == "initial_cash"
            )
            if payload.get("assigned_periods", [None])[0] == 1:
                if "diagnostics" not in payload:
                    data["periods"]["2"][0].update(
                        key="fee02", details={"target_entity_id": "company"}
                    )
                else:
                    data["periods"] = {"2": [decision(2)]}
            data = loads(dumps(data).replace('"cash"', dumps(cash)))
            model = ToolChatModel(replies=[tool_reply(request.schema_name, data)])
            calls.append(model)
            return LangChainLLMClient(
                Settings(llm_model="test", llm_api_key="fake"), lambda _: model
            ).generate(request)

        with patch("erp_agent.api.server.Handler.log_message"):
            case.setUp()
            try:
                project, reading = case.setup_problem()
                base = f"/api/projects/{project}"
                saved = reading["snapshot"]["reading"]
                facts = saved["problem"]["facts"]
                for fact in facts:
                    if fact["key"] in {"years", "periods_per_year"}:
                        fact.update(value="4", explanation="四年，每年四期的受控情景")
                revised = case.request(
                    base + "/revisions",
                    {
                        "expected_version": 1,
                        "reading": {"title": "分段测试", "facts": facts, "rules": [], "issues": []},
                        "note": "设为十六期",
                    },
                )
                with patch.object(case.llm, "generate", side_effect=native_generate):
                    job = case.request(base + "/solve", {"version": revised["version"]})
                    result = case.wait_job(project, job["id"])
                self.assertEqual(result["status"], "succeeded", result.get("error"))
                plan = case.request(base + "/solutions")[0]["plan"]
                self.assertEqual({a["period"] for a in plan["actions"]}, set(range(1, 17)))
                self.assertEqual(sum(m.calls for m in calls), 8)
                self.assertEqual(plan["preparation"]["version"], "solver-20")
            finally:
                case.tearDown()

    def test_four_window_requests_overlap_and_no_request_outputs_all_sixteen_periods(self):
        barrier = threading.Barrier(4)

        class ParallelClient(PlannerClient):
            def generate(self, request):
                if "assigned_periods" in loads(request.user):
                    barrier.wait(timeout=3)
                return super().generate(request)

        client, control = ParallelClient(), Control()
        plan = ParallelPlanningGenerator(client, control).propose(long_snapshot()).baseline
        windows = [r for r in client.calls if "assigned_periods" in loads(r.user)]
        self.assertEqual(len(windows), 4)
        self.assertEqual(len(client.calls), 7)
        self.assertEqual(task_budget(control, TaskContext("solver", "Solver", "", "")).calls, 7)
        self.assertEqual({a.period for a in plan.actions}, set(range(1, 17)))
        for index, request in enumerate(sorted(windows, key=lambda r: r.task.id)):
            material, schema = loads(request.user), loads(request.schema_json)
            self.assertEqual(
                material["assigned_periods"], list(range(index * 4 + 1, index * 4 + 5))
            )
            self.assertEqual(
                set(schema["properties"]["periods"]["properties"]),
                set(map(str, material["assigned_periods"])),
            )
            self.assertNotIn("findings", material)
            self.assertNotIn("previous_plan", material)
            self.assertLessEqual(len(dumps(material["context"])), 22000)
            self.assertLessEqual(request.max_output_tokens, 8000)
            self.assertTrue(request.distribute)
        route = next(r for r in client.calls if r.schema_name == "erp_planning_route")
        self.assertNotIn("periods", loads(route.schema_json)["properties"])
        self.assertEqual(plan.preparation.version, "solver-20")

    def test_partition_uses_actual_horizon(self):
        for horizon in (1, 2, 5, 10, 15, 16, 24):
            groups = period_windows(horizon)
            self.assertEqual([p for group in groups for p in group], list(range(1, horizon + 1)))
            self.assertLessEqual(len(groups), 4)
        plan = (
            ParallelPlanningGenerator(PlannerClient(), Control())
            .propose(long_snapshot(2, 5))
            .baseline
        )
        self.assertEqual({a.period for a in plan.actions}, set(range(1, 11)))

    def test_missing_nullable_payment_obligation_is_normalized_without_model_repair(self):
        data = decisions(PlanningKnowledge(snapshot()).hash)
        data["periods"]["2"][0].update(key="fee02", details={"finance_entity_id": "company"})
        plan = compile_decisions(data, snapshot(), data["context_hash"], ())
        self.assertIsNone(plan.actions[1].details.obligation_action_id)
        self.assertEqual(plan.actions[1].amount, Decimal("10"))

    def test_schema_pairs_payment_with_finance_details(self):
        schema = decision_schema(16)["$defs"]["decision"]
        payment = next(v for v in schema["anyOf"] if "payment" in v["properties"]["kind"]["enum"])
        self.assertEqual(
            set(payment["properties"]["details"]["properties"]),
            {"finance_entity_id", "obligation_action_id"},
        )
        self.assertFalse(payment["properties"]["details"]["additionalProperties"])

    def test_wrong_fee_details_only_repairs_affected_period_in_one_window(self):
        class FeeClient(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                payload = loads(request.user)
                if payload.get("assigned_periods", [None])[0] == 1:
                    data = loads(response.content)
                    if "diagnostics" not in payload:
                        data["periods"]["2"][0].update(
                            key="fee02", details={"target_entity_id": "company"}
                        )
                    else:
                        self_test.assertIn("fee02.details", payload["diagnostics"])
                        self_test.assertIn("finance_entity_id", payload["diagnostics"])
                        self_test.assertEqual(payload["allowed_periods"], ["2"])
                        data["periods"] = {"2": [decision(2)]}
                    return replace(response, content=dumps(data))
                return response

        self_test = self
        client = FeeClient()
        plan = ParallelPlanningGenerator(client, Control()).propose(long_snapshot()).baseline
        calls = Counter(r.task.id for r in client.calls)
        self.assertEqual(len(client.calls), 8)
        self.assertEqual(calls["solver:synthesis:window:1"], 2)
        self.assertTrue(all(calls[f"solver:synthesis:window:{n}"] == 1 for n in (2, 3, 4)))
        self.assertTrue(all(a.amount == Decimal("10") for a in plan.actions))

    def test_repair_cannot_change_other_period_in_same_window(self):
        class BadRepair(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                payload = loads(request.user)
                if payload.get("assigned_periods", [None])[0] == 1:
                    data = loads(response.content)
                    if "diagnostics" not in payload:
                        data["periods"]["2"] = []
                    else:
                        data["periods"]["1"][0]["amount"] = "99"
                    return replace(response, content=dumps(data))
                return response

        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(BadRepair(), Control()).propose(long_snapshot())
        self.assertEqual(caught.exception.code, "planning.repair_scope")

    def test_shared_borrow_and_repayment_are_inserted_once_and_dependencies_survive(self):
        class Shared(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                data = loads(response.content)
                if request.schema_name == "erp_planning_route":
                    borrow = {
                        **decision(1, "100"),
                        "key": "shared-loan",
                        "kind": "borrow",
                        "period": 1,
                        "amount_meaning": "financing_principal",
                    }
                    repay = {
                        **decision(16, "100"),
                        "key": "shared-repay",
                        "kind": "repay",
                        "period": 16,
                        "amount_meaning": "repayment_principal",
                        "depends_on": ["shared-loan"],
                        "details": {
                            "finance_entity_id": "company",
                            "obligation_action_id": "shared-loan",
                        },
                    }
                    data["shared_actions"] = [borrow, repay]
                elif loads(request.user).get("assigned_periods", [None])[0] == 13:
                    data["periods"]["13"][0]["depends_on"] = ["shared-loan"]
                return replace(response, content=dumps(data))

        plan = ParallelPlanningGenerator(Shared(), Control()).propose(long_snapshot()).baseline
        loans = [a for a in plan.actions if a.kind == "borrow"]
        repays = [a for a in plan.actions if a.kind == "repay"]
        self.assertEqual(len(loans), 1)
        self.assertEqual(len(repays), 1)
        self.assertEqual(repays[0].details.obligation_action_id, loans[0].action_id)
        self.assertEqual(
            next(a for a in plan.actions if a.period == 13).depends_on, (loans[0].action_id,)
        )

    def test_same_local_key_in_different_windows_is_namespaced(self):
        class SameKeys(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if "assigned_periods" in loads(request.user):
                    data = loads(response.content)
                    start = min(map(int, data["periods"]))
                    data["periods"][str(start)][0]["key"] = "fee"
                    data["periods"][str(start + 1)][0]["depends_on"] = ["fee"]
                    return replace(response, content=dumps(data))
                return response

        plan = ParallelPlanningGenerator(SameKeys(), Control()).propose(long_snapshot()).baseline
        fees = [a for a in plan.actions if a.action_id.endswith("-fee")]
        self.assertEqual(len(fees), 4)
        self.assertEqual(len({a.action_id for a in plan.actions}), 16)

    def test_fabricated_cross_window_dependency_is_not_accepted(self):
        class Fabricated(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if loads(request.user).get("assigned_periods", [None])[0] == 5:
                    data = loads(response.content)
                    data["periods"]["5"][0]["depends_on"] = ["w1-expense-1"]
                    return replace(response, content=dumps(data))
                return response

        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(Fabricated(), Control()).propose(long_snapshot())
        self.assertEqual(caught.exception.code, "planning.window_validation")
        self.assertIn("跨段行动", caught.exception.message)

    def test_global_cash_flow_is_checked_after_parallel_merge(self):
        class Overspend(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if "assigned_periods" in loads(request.user):
                    data = loads(response.content)
                    for values in data["periods"].values():
                        for action in values:
                            action["amount"] = "10000"
                    return replace(response, content=dumps(data))
                return response

        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(Overspend(), Control()).propose(long_snapshot())
        self.assertIn("现金", caught.exception.message)

    def test_window_caps_are_frozen_and_checked_after_merge(self):
        class Cap(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if request.schema_name == "erp_planning_route":
                    data = loads(response.content)
                    data["windows"]["2"]["cash_outflow_limit"] = "20"
                    return replace(response, content=dumps(data))
                return response

        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(Cap(), Control()).propose(long_snapshot())
        self.assertIn("冻结路线限额", caught.exception.message)

    def test_wrong_version_does_not_repair_or_write_success_cache(self):
        class Foreign(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if loads(request.user).get("assigned_periods", [None])[0] == 5:
                    data = loads(response.content)
                    data["context_hash"] = "other-version"
                    return replace(response, content=dumps(data))
                return response

        client, memory = Foreign(), Memory()
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control(), memory).propose(long_snapshot())
        self.assertEqual(caught.exception.code, "planning.context")
        self.assertEqual(len(client.calls), 7)
        self.assertTrue(all("findings" in loads(v["content"]) for v in memory.records.values()))

    def test_successful_window_cache_is_revalidated_without_new_calls(self):
        client, memory, control = PlannerClient(), Memory(), Control()
        first = ParallelPlanningGenerator(client, control, memory).propose(long_snapshot())
        second = ParallelPlanningGenerator(client, control, memory).propose(long_snapshot())
        self.assertEqual(first.baseline, second.baseline)
        self.assertEqual(len(client.calls), 7)
