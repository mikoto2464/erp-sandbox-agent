import threading
import unittest
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from docx import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from erp_agent.adapters.docx_reports import DocxReportRenderer
from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.codec import solution_bundle
from erp_agent.application.contracts import decode_plan, dumps, loads, to_json
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_decisions import compile_decisions
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.planning_tools import PlanningTools
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.application.prepared_budget import prepared_budget
from erp_agent.application.solver import DraftPlanEvaluator, SolverService
from erp_agent.application.task_budget import TaskCallBudget, model_call
from erp_agent.config import Settings
from erp_agent.domain.calculations import ERPCalculator
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Fact,
    KnowledgeState,
    PlanningContext,
    ReportData,
    SolveRequest,
    SolverMode,
)
from tests.helpers import Control, snapshot, structured_action


def decision(period, amount="10"):
    raw = structured_action(index=period, period=period, amount=amount, description="支付经营费用")
    for key in (
        "action_id",
        "schema_version",
        "fact_ids",
        "rule_ids",
        "gap_ids",
        "missing_reason",
        "period",
    ):
        raw.pop(key)
    raw.update(
        key=f"expense-{period}",
        references=["cash"],
        amount_calculation_id=None,
        quantity_calculation_id=None,
        payment_period=period,
    )
    return raw


def decisions(context_hash, horizon=2):
    return {
        "context_hash": context_hash,
        "title": "经营行动草稿",
        "summary": "按现金约束分期经营。",
        "assumptions": [],
        "risks": [],
        "periods": {str(p): [decision(p)] for p in range(1, horizon + 1)},
    }


class PlannerClient:
    configured = True

    def __init__(self, barrier=None):
        self.calls = []
        self.barrier = barrier

    def generate(self, request):
        self.calls.append(request)
        material = loads(request.user)
        context = material["context"]
        if request.schema_name == "erp_planning_analysis":
            if self.barrier:
                self.barrier.wait(timeout=3)
            return LLMResponse(
                dumps(
                    {
                        "context_hash": context["context_hash"],
                        "summary": "根据现金和经营周期安排投入。",
                        "findings": [],
                    }
                ),
                "fake",
            )
        if request.schema_name == "erp_planning_route":
            return LLMResponse(
                dumps(
                    {
                        "context_hash": context["context_hash"],
                        "title": "经营行动草稿",
                        "summary": "按现金约束分期经营。",
                        "assumptions": [],
                        "risks": [],
                        "shared_actions": [],
                        "windows": {
                            str(i + 1): {
                                "references": ["cash"],
                                "constraints": [],
                                "cash_outflow_limit": None,
                                "cash_unit": "元",
                            }
                            for i in range(len(material["planning_windows"]))
                        },
                    }
                ),
                "fake",
            )
        horizon = context["years"] * context["periods_per_year"]
        data = decisions(context["context_hash"], horizon)
        if "assigned_periods" in material:
            data = {
                "context_hash": context["context_hash"],
                "periods": {str(p): data["periods"][str(p)] for p in material["assigned_periods"]},
            }
        return LLMResponse(dumps(data), "fake")


class ToolChatModel(BaseChatModel):
    replies: list = Field(default_factory=list)
    histories: list = Field(default_factory=list)
    calls: int = 0

    @property
    def _llm_type(self):
        return "controlled-tool-model"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.histories.append(messages)
        result = self.replies[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=result)])


def tool_reply(name, args, finish="tool_calls"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call"}],
        response_metadata={"finish_reason": finish},
        usage_metadata={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
    )


class PlanningPipelineTests(unittest.TestCase):
    def test_roles_really_overlap_and_parameters_are_frozen_before_export(self):
        client = PlannerClient(threading.Barrier(2))
        proposal = ParallelPlanningGenerator(client, Control()).propose(snapshot())
        self.assertEqual(len(client.calls), 3)
        self.assertIsNotNone(proposal.baseline.preparation)
        self.assertTrue(all(r.distribute for r in client.calls[:2]))
        self.assertTrue(DraftPlanEvaluator().evaluate(snapshot(), proposal.baseline).feasible)

    def test_complete_sixteen_periods_sqlite_roundtrip_and_word_reopen(self):
        problem = snapshot()
        problem = replace(
            problem,
            reading=replace(
                problem.reading,
                problem=replace(problem.reading.problem, years=4, periods_per_year=4),
            ),
        )
        client = PlannerClient()
        result = SolverService(
            ParallelPlanningGenerator(client, Control()), DraftPlanEvaluator()
        ).solve(SolveRequest(problem, SolverMode.LLM_ONLY), Control())
        self.assertEqual({a.period for a in result.plan.actions}, set(range(1, 17)))
        with TemporaryDirectory() as directory:
            repository = SQLiteRepository(Path(directory) / "isolated.db")
            repository.put("solution", problem.project_id, result.id, result)
            restored = solution_bundle(repository.get("solution", problem.project_id, result.id))
            self.assertEqual(restored.plan.preparation, result.plan.preparation)
            report = ReportData(problem, restored, outline=restored.plan.preparation.outline)
            artifact = DocxReportRenderer().render(report)
            doc = Document(BytesIO(artifact.content))
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("第 4 年 · 第 4 季", text)
            self.assertEqual(len(client.calls), 7)

    def test_wrong_context_fails_without_repair_or_success_cache(self):
        class Wrong(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                data = loads(response.content)
                data["context_hash"] = "another-project"
                return replace(response, content=dumps(data))

        client = Wrong()
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control()).propose(snapshot())
        self.assertEqual(caught.exception.code, "planning.context")
        self.assertLessEqual(len(client.calls), 2)

    def test_bounded_repair_on_malformed_decisions(self):
        class BrokenOnce(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if (
                    request.schema_name == "erp_planning_decisions"
                    and "diagnostics" not in request.user
                ):
                    return replace(response, content='{"bad":true}')
                return response

        client = BrokenOnce()
        result = ParallelPlanningGenerator(client, Control()).propose(snapshot())
        self.assertEqual(len(result.baseline.actions), 2)
        self.assertEqual(len(client.calls), 4)

    def test_invalid_period_is_repaired_once_and_unrelated_values_survive(self):
        class BrokenOnce(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if (
                    request.schema_name == "erp_planning_decisions"
                    and "diagnostics" not in request.user
                ):
                    data = loads(response.content)
                    data["periods"]["2"] = []
                    return replace(response, content=dumps(data))
                return response

        client = BrokenOnce()
        proposal = ParallelPlanningGenerator(client, Control()).propose(snapshot())
        self.assertEqual([a.amount for a in proposal.baseline.actions], [Decimal(10)] * 2)
        self.assertEqual(len(client.calls), 4)

    def test_context_is_scoped_bounded_and_hash_changes_with_content(self):
        problem = snapshot()
        pool = tuple(
            Fact(
                f"order-{i}",
                "order_pool",
                f"order-{i}",
                f"订单{i}",
                "quantity",
                "100",
                "integer",
                "件",
                KnowledgeState.KNOWN,
                (),
            )
            for i in range(300)
        )
        product = replace(
            problem,
            reading=replace(
                problem.reading,
                problem=replace(
                    problem.reading.problem, facts=problem.reading.problem.facts + pool
                ),
            ),
        )
        context = PlanningKnowledge(product)
        finance = context.project("finance")
        self.assertNotIn("order-299", dumps(finance))
        self.assertGreater(finance["omitted_fact_count"], 250)
        self.assertLess(len(dumps(context.project("operations"))), 22000)
        self.assertNotEqual(context.hash, PlanningKnowledge(problem).hash)
        found = PlanningTools(context.data).invoke(
            "planning_lookup", {"context_hash": context.hash, "kind": "facts", "ids": ["order-299"]}
        )
        self.assertEqual(found["items"][0]["id"], "order-299")
        self.assertNotIn("order-298", dumps(found))

    def test_calculation_results_are_bound_by_python_and_roundtrip(self):
        problem = snapshot()
        knowledge = PlanningKnowledge(problem)
        tools = PlanningTools(knowledge.data)
        result = tools.invoke(
            "erp_calculate",
            {
                "context_hash": knowledge.hash,
                "id": "synthesis-cost",
                "formula": "multiply",
                "inputs": [
                    {"name": "quantity", "value": "3", "unit": "件", "reason": "拟定采购"},
                    {"name": "unit_price", "value": "2.5", "unit": "元/件", "reason": "价格情景"},
                ],
                "unit": "元",
                "assumption": "采购情景",
            },
        )
        self.assertEqual(result["value"], "7.5")
        data = decisions(knowledge.hash)
        data["periods"]["1"][0].update(amount=None, amount_calculation_id="synthesis-cost")
        plan = compile_decisions(data, problem, knowledge.hash, tuple(tools.records.values()))
        self.assertEqual(plan.actions[0].amount, Decimal("7.5"))
        self.assertIn("采购情景", " ".join(plan.assumptions))
        self.assertEqual(decode_plan(to_json(plan)), plan)
        data["periods"]["1"][0]["amount"] = "999"
        with self.assertRaises(ERPError):
            compile_decisions(data, problem, knowledge.hash, tuple(tools.records.values()))

    def test_tool_loop_uses_native_calls_and_counts_every_round(self):
        knowledge = PlanningKnowledge(snapshot())
        model = ToolChatModel(
            replies=[
                tool_reply(
                    "planning_lookup",
                    {"context_hash": knowledge.hash, "kind": "facts", "ids": ["cash"]},
                ),
                tool_reply("result", {"value": "ok"}),
            ]
        )
        settings = Settings(llm_model="test", llm_api_key="fake", llm_format="json_schema")
        client = LangChainLLMClient(settings, lambda _: model)
        request = LLMRequest(
            "根据题目输出",
            "职责材料",
            "result",
            dumps(
                {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                }
            ),
            stream=True,
            tool_context=knowledge.data,
        )
        budget = TaskCallBudget(3)
        with model_call(budget):
            response = client.generate(request)
        self.assertEqual(loads(response.content), {"value": "ok"})
        self.assertEqual(budget.calls, 2)
        self.assertEqual(response.output_tokens, 10)
        self.assertEqual(model.histories[1][-1].type, "tool")
        self.assertIn("100000", model.histories[1][-1].content)

    def test_tool_loop_respects_shared_call_limit_and_cross_context_rejection(self):
        knowledge = PlanningKnowledge(snapshot())
        model = ToolChatModel(
            replies=[
                tool_reply(
                    "planning_lookup",
                    {"context_hash": knowledge.hash, "kind": "facts", "ids": ["cash"]},
                )
            ]
        )
        client = LangChainLLMClient(Settings(llm_model="test", llm_api_key="fake"), lambda _: model)
        request = LLMRequest(
            "解题", "现金", "result", dumps({"type": "object"}), tool_context=knowledge.data
        )
        with self.assertRaises(ERPError) as caught, model_call(TaskCallBudget(1)):
            client.generate(request)
        self.assertEqual(caught.exception.code, "task_call_limit")
        with self.assertRaises(ERPError):
            PlanningTools(knowledge.data).invoke(
                "planning_lookup", {"context_hash": "wrong", "kind": "facts"}
            )

    def test_unknown_and_conflicted_inputs_cannot_be_calculated(self):
        knowledge = PlanningKnowledge(snapshot())
        knowledge.data["facts"][0]["state"] = "missing"
        with self.assertRaises(ERPError):
            PlanningTools(knowledge.data).invoke(
                "erp_calculate",
                {
                    "context_hash": knowledge.hash,
                    "id": "bad",
                    "formula": "multiply",
                    "inputs": [
                        {"name": "quantity", "fact_id": "cash", "unit": "元"},
                        {"name": "unit_price", "value": "1", "unit": "1", "reason": "情景"},
                    ],
                    "unit": "元",
                    "assumption": "情景",
                },
            )

    def test_native_openai_sdk_tool_roundtrip_hides_full_server_context(self):
        import json
        from contextlib import ExitStack
        from importlib import import_module

        import httpx

        knowledge = PlanningKnowledge(snapshot())
        args = {
            "context_hash": knowledge.hash,
            "id": "cost",
            "formula": "multiply",
            "unit": "元",
            "assumption": "采购情景",
            "period": 1,
            "inputs": [
                {"name": "quantity", "value": "3", "unit": "件", "reason": "拟定"},
                {"name": "unit_price", "value": "2.5", "unit": "元/件", "reason": "价格情景"},
            ],
        }
        seen = []

        def send(client, request, _module=httpx, **kwargs):
            seen.append(json.loads(request.content))
            name, arguments = (
                ("erp_calculate", args) if len(seen) == 1 else ("result", {"value": "ok"})
            )
            response = {
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{len(seen)}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            }
            reply = _module.Response(200, json=response, request=request)
            reply.read()
            return reply

        with ExitStack() as stack:
            for module in (httpx, import_module("httpx2")):

                def module_send(client, request, _module=module, **kwargs):
                    return send(client, request, _module, **kwargs)

                stack.enter_context(patch.object(module.Client, "send", module_send))
            request = LLMRequest(
                "按需计算后提交",
                "职责材料",
                "result",
                dumps(
                    {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    }
                ),
                tool_context=knowledge.data,
            )
            result = LangChainLLMClient(
                Settings(llm_model="test", llm_api_key="fake", llm_retries=0)
            ).generate(request)
        self.assertEqual(loads(result.content), {"value": "ok"})
        self.assertEqual(result.calculations[0].value, "7.5")
        self.assertEqual(result.calculations[0].period, 1)
        self.assertEqual(len(seen), 2)
        self.assertNotIn("100000", dumps(seen[0]))
        self.assertEqual(seen[1]["messages"][-1]["role"], "tool")

    def test_cancelled_tool_loop_stops_before_another_model_call(self):
        from erp_agent.application.task_budget import run_control
        from erp_agent.domain.errors import Cancelled

        class CancelAfterTransport(Control):
            def checkpoint(self, stage, completed=0):
                super().checkpoint(stage, completed)
                if len(self.stages) == 2:
                    raise Cancelled()

        knowledge = PlanningKnowledge(snapshot())
        model = ToolChatModel(
            replies=[
                tool_reply(
                    "planning_lookup",
                    {"context_hash": knowledge.hash, "kind": "facts", "ids": ["cash"]},
                )
            ]
        )
        client = LangChainLLMClient(Settings(llm_model="test", llm_api_key="fake"), lambda _: model)
        request = LLMRequest(
            "解题", "材料", "result", dumps({"type": "object"}), tool_context=knowledge.data
        )
        with self.assertRaises(Cancelled), run_control(CancelAfterTransport()):
            client.generate(request)
        self.assertEqual(model.calls, 1)

    def test_two_models_share_parallel_roles_and_keep_one_coordinator(self):
        from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
        from tests.test_direct_scheduling import binding

        barrier = threading.Barrier(2)
        first, second = PlannerClient(barrier), PlannerClient(barrier)
        pool = ScheduledLLMClient(
            (
                binding("first", 100, parallel=2, client=first),
                binding("second", 90, parallel=2, client=second),
            ),
            ModelCapacity(),
        )
        result = ParallelPlanningGenerator(pool, Control()).propose(snapshot())
        self.assertEqual(len(first.calls), 2)
        self.assertEqual(len(second.calls), 1)
        self.assertIsNotNone(result.baseline.preparation)

    def test_success_cache_is_validated_and_other_context_does_not_reuse_it(self):
        class Memory:
            def __init__(self):
                self.records = {}

            def get(self, key):
                return self.records.get(key)

            def put(self, key, value):
                self.records[key] = value

        memory, client = Memory(), PlannerClient()
        first = ParallelPlanningGenerator(client, Control(), memory).propose(snapshot())
        second = ParallelPlanningGenerator(client, Control(), memory).propose(snapshot())
        self.assertEqual(first.baseline, second.baseline)
        self.assertEqual(len(client.calls), 3)
        other = replace(snapshot(), project_id="different-project")
        ParallelPlanningGenerator(client, Control(), memory).propose(other)
        self.assertEqual(len(client.calls), 6)

    def test_budget_is_generated_before_save_and_export_mapping_uses_no_model(self):
        from erp_agent.application.workbook_mapper import WorkbookMapper
        from erp_agent.domain.models import GenerationConfig
        from tests.test_budget_execution import fixture, mapping

        problem, _, template = fixture()

        class Client(PlannerClient):
            def generate(self, request):
                if request.schema_name == "erp_solver_budget":
                    self.calls.append(request)
                    payload = loads(request.user)
                    ref = "action:" + payload["actions"][0]["action_id"] + ":amount"
                    return LLMResponse(
                        dumps(
                            {"status": "applicable", "mappings": [mapping(refs=(ref,))], "gaps": []}
                        ),
                        "fake",
                    )
                response = super().generate(request)
                if request.schema_name == "erp_planning_decisions":
                    data = loads(response.content)
                    data["periods"]["1"][0].update(
                        kind="borrow", amount_meaning="financing_principal"
                    )
                    return replace(response, content=dumps(data))
                return response

        client, control = Client(), Control()
        context = PlanningContext(GenerationConfig(template_ids=(template.source.id,)), (template,))
        result = SolverService(
            ParallelPlanningGenerator(client, control), DraftPlanEvaluator()
        ).solve(SolveRequest(problem, SolverMode.LLM_ONLY, context), control)
        self.assertEqual(result.plan.budget_items[0].value, "10")
        initial_calls = len(client.calls)
        published = prepared_budget(result, problem, (template,))
        self.assertEqual(published.items, result.plan.budget_items)
        mapped = WorkbookMapper().analyze(result, template, client, control, problem)
        self.assertEqual(mapped.entries[0].source_ids, ("budget:0",))
        self.assertEqual(len(client.calls), initial_calls)
        changed = replace(template, source=replace(template.source, sha256="different"))
        with self.assertRaises(ERPError):
            prepared_budget(result, problem, (changed,))
        changed_problem = replace(
            problem,
            reading=replace(
                problem.reading, problem=replace(problem.reading.problem, title="同版本内容被替换")
            ),
        )
        with self.assertRaises(ERPError):
            prepared_budget(result, changed_problem, (template,))

    def test_partial_budget_failure_preserves_actions_and_explicit_failed_sheets(self):
        from erp_agent.application.solution_stages import plan_hash
        from erp_agent.domain.budget import BudgetResult
        from erp_agent.domain.models import GenerationConfig
        from tests.test_budget_execution import fixture

        problem, _, template = fixture()

        def fail(planner, snapshot, context, plan, **kwargs):
            partial = BudgetResult(
                (),
                (),
                "failed",
                "",
                snapshot.version,
                ((template.source.id, template.source.sha256),),
                plan_hash(plan),
                (),
                ((template.source.id, template.sheets[0].name, "来源匹配失败"),),
            )
            kwargs["on_sheet"](partial)
            raise ERPError("budget.stage_failed", "来源匹配失败")

        with patch("erp_agent.application.solver_budget.PlanBudgetPlanner.generate", fail):
            result = ParallelPlanningGenerator(PlannerClient(), Control()).propose(
                problem,
                PlanningContext(GenerationConfig(template_ids=(template.source.id,)), (template,)),
            )
        self.assertEqual(len(result.baseline.actions), 2)
        self.assertEqual(len(result.baseline.preparation.failed_sheets), 1)

    def test_repair_cannot_modify_a_valid_period(self):
        class BadRepair(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if request.schema_name == "erp_planning_decisions":
                    data = loads(response.content)
                    if "diagnostics" not in request.user:
                        data["periods"]["2"] = []
                    else:
                        data["periods"]["1"][0]["amount"] = "100"
                    return replace(response, content=dumps(data))
                return response

        with self.assertRaises(ERPError) as error:
            ParallelPlanningGenerator(BadRepair(), Control()).propose(snapshot())
        self.assertEqual(error.exception.code, "planning.repair_scope")

    def test_unread_fact_reference_requires_lookup(self):
        problem = snapshot()
        knowledge = PlanningKnowledge(problem)
        with self.assertRaises(ERPError) as error:
            compile_decisions(decisions(knowledge.hash), problem, knowledge.hash, (), set())
        self.assertEqual(error.exception.code, "planning.not_loaded")

    def test_formula_unit_mismatch_and_duplicate_inputs_fail(self):
        knowledge = PlanningKnowledge(snapshot())
        args = {
            "context_hash": knowledge.hash,
            "id": "bad",
            "formula": "multiply",
            "unit": "元",
            "assumption": "显式情景",
            "inputs": [
                {"name": "quantity", "value": "3", "unit": "件", "reason": "拟定"},
                {"name": "unit_price", "value": "2", "unit": "万元/件", "reason": "价格"},
            ],
        }
        with self.assertRaises(ERPError) as error:
            PlanningTools(knowledge.data).invoke("erp_calculate", args)
        self.assertEqual(error.exception.code, "calculation.unit")
        args["inputs"][1]["name"] = "quantity"
        with self.assertRaises(ERPError):
            PlanningTools(knowledge.data).invoke("erp_calculate", args)


class FormulaTests(unittest.TestCase):
    def check(self, name, values, expected):
        result = ERPCalculator().calculate(name, {k: Decimal(str(v)) for k, v in values.items()})
        self.assertEqual(result, Decimal(str(expected)))

    def test_production_wages_wip_and_depreciation_hand_calculations(self):
        self.check(
            "production",
            dict(
                base_capacity=1000,
                manual_efficiency_sum="1.5",
                senior_efficiency_sum="0.5",
                shift_multiplier=1,
            ),
            1875,
        )
        self.check(
            "piece_wages",
            dict(
                quantity=10,
                manual_piece_rate="0.2",
                manual_count=3,
                senior_piece_rate="0.5",
                senior_count=1,
            ),
            11,
        )
        self.check(
            "wip_cost",
            dict(
                materials=100,
                monthly_wages=20,
                production_months=6,
                piece_wages=11,
                startup_cost=10,
            ),
            241,
        )
        self.check("depreciation", dict(original=1000, residual=100, years=3), 300)
        self.check("dismissal", dict(service_years="0.2", monthly_salary=100), 200)

    def test_rounding_rates_and_negative_values(self):
        self.check("interest", dict(principal=101, term_rate="0.025", quantum=1), 3)
        self.check("half_up", dict(value="2.5", quantum=1), 3)
        self.check("half_up", dict(value="-2.5", quantum=1), -3)
        self.check("floor", dict(value="-2.1", quantum=1), -3)

    def test_carbon_zero_division_and_fractional_workers_fail(self):
        for formula, values in [
            ("carbon_quota", dict(total_emissions=0, own_emissions=0, team_count=2)),
            ("carbon_quota", dict(total_emissions=100, own_emissions=10, team_count=1)),
            (
                "piece_wages",
                dict(
                    quantity=1,
                    manual_piece_rate=1,
                    manual_count="1.5",
                    senior_piece_rate=1,
                    senior_count=0,
                ),
            ),
        ]:
            with self.subTest(formula=formula), self.assertRaises(ERPError):
                ERPCalculator().calculate(formula, {k: Decimal(str(v)) for k, v in values.items()})
