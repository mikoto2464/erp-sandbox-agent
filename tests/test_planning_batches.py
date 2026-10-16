"""分批计算与职责修正的真实工具/流程回归，不调用外部模型。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from pydantic import ValidationError

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.application.contracts import dumps, loads, to_json
from erp_agent.application.planning_analysis import validate_analysis
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.planning_tools import PlanningTools
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.application.task_budget import TaskCallBudget, model_call, run_control
from erp_agent.config import Settings
from erp_agent.domain.errors import Cancelled, ERPError
from tests.helpers import Control, snapshot
from tests.test_planning_pipeline import PlannerClient, ToolChatModel, tool_reply


def cost(identifier="operations-cost", *, quantity="3", price="2.5"):
    return {
        "id": identifier,
        "formula": "multiply",
        "unit": "元",
        "period": 1,
        "assumption": "采购成本情景",
        "inputs": [
            {"name": "quantity", "value": quantity, "unit": "件", "reason": "拟采购量"},
            {"name": "unit_price", "value": price, "unit": "元/件", "reason": "候选报价"},
        ],
    }


def finding(context_hash, references, *, statement="核对业务成本。"):
    return {
        "context_hash": context_hash,
        "summary": "按资金边界安排采购。",
        "findings": [
            {"statement": statement, "references": references, "implication": "分期投入。"}
        ],
    }


class PlanningBatchTests(unittest.TestCase):
    def setUp(self):
        self.knowledge = PlanningKnowledge(snapshot())
        self.tools = PlanningTools(self.knowledge.data)

    def batch(self, *items):
        return self.tools.invoke("erp_calculate_batch", {"calculations": list(items)})["results"]

    def test_formula_argument_diagnostic_can_repair_failed_item_without_catalog_query(self):
        bad = cost("fee")
        bad["inputs"][0]["name"] = "months"
        bad["inputs"][1]["name"] = "monthly_fee"
        first = self.batch(bad, cost("valid"))
        message = first[0]["message"]
        for name in ("multiply", "quantity", "unit_price", "months", "monthly_fee"):
            self.assertIn(name, message)
        self.assertEqual(first[1]["value"], "7.5")
        fixed = cost("fee")
        self.assertEqual(self.batch(fixed)[0]["value"], "7.5")
        self.assertEqual(set(self.tools.records), {"fee", "valid"})
        schema = next(s for s in PlanningTools.schemas() if s["name"] == "erp_calculate_batch")
        self.assertIn(
            "multiply(quantity,unit_price)",
            schema["parameters"]["properties"]["calculations"]["items"]["properties"]["formula"][
                "description"
            ],
        )

    def test_native_multiple_batches_compute_four_results_in_three_model_requests(self):
        # 3件×2.5元/件=7.5元；7件×0.1元/件=0.7元；9件×0.2元/件=1.8元。
        replies = [
            tool_reply(
                "erp_calculate_batch",
                {"calculations": [cost(), cost("operations-b", quantity="7", price="0.1")]},
            ),
            tool_reply(
                "erp_calculate_batch",
                {
                    "calculations": [
                        cost("operations-c", quantity="9", price="0.2"),
                        cost("operations-d", quantity="4", price="1.25"),
                    ]
                },
            ),
            tool_reply("result", {"summary": "计算完成"}),
        ]
        model = ToolChatModel(replies=replies)
        request = LLMRequest(
            "分批计算后提交",
            "当前职责材料",
            "result",
            dumps(
                {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                }
            ),
            tool_context=self.knowledge.data,
        )
        budget = TaskCallBudget(3)
        with model_call(budget):
            result = LangChainLLMClient(
                Settings(llm_model="test", llm_api_key="fake"), lambda _: model
            ).generate(request)
        self.assertEqual(model.calls, 3)
        self.assertEqual(budget.calls, 3)
        self.assertEqual([r.value for r in result.calculations], ["7.5", "0.7", "1.8", "5.00"])
        self.assertTrue(all(r.context_hash == self.knowledge.hash for r in result.calculations))
        self.assertEqual(len(loads(model.histories[1][-1].content)["results"]), 2)

    def test_dependencies_can_use_previous_item_and_previous_batch(self):
        def interest(identifier, previous):
            return {
                "id": identifier,
                "formula": "interest",
                "unit": "元",
                "assumption": "完整期限总息情景",
                "inputs": [
                    {"name": "principal", "calculation_id": previous, "unit": "元"},
                    {
                        "name": "term_rate",
                        "value": "0.1",
                        "unit": "比例",
                        "reason": "假设完整期限利率",
                    },
                    {"name": "quantum", "value": "1", "unit": "元", "reason": "按元取整情景"},
                ],
            }

        first = self.batch(cost(), interest("operations-interest", "operations-cost"))
        self.assertEqual(first[1]["value"], "1")  # ceil(7.5×0.1)
        second = self.batch(interest("operations-next", "operations-interest"))
        self.assertEqual(second[0]["value"], "1")

    def test_partial_batch_can_be_retried_after_dependency_is_available(self):
        pending = {
            "id": "rounded",
            "formula": "floor",
            "unit": "元",
            "assumption": "取整情景",
            "inputs": [
                {"name": "value", "calculation_id": "operations-cost", "unit": "元"},
                {"name": "quantum", "value": "1", "unit": "元", "reason": "按元取整"},
            ],
        }
        batch = tool_reply("erp_calculate_batch", {"calculations": [pending, cost()]})
        model = ToolChatModel(replies=[batch, batch, tool_reply("result", {"value": "完成"})])
        request = LLMRequest(
            "分批计算后提交",
            "职责材料",
            "result",
            dumps(
                {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                }
            ),
            tool_context=self.knowledge.data,
        )
        result = LangChainLLMClient(
            Settings(llm_model="test", llm_api_key="fake"), lambda _: model
        ).generate(request)
        self.assertEqual(
            loads(model.histories[1][-1].content)["results"][0]["error"], "calculation.source"
        )
        self.assertEqual(loads(model.histories[2][-1].content)["results"][0]["value"], "7")
        self.assertEqual([r.value for r in result.calculations], ["7.5", "7"])

    def test_invalid_item_and_its_dependent_do_not_discard_other_results(self):
        bad = {**cost("bad"), "inputs": []}
        dependent = cost("dependent")
        dependent["inputs"][1] = {"name": "unit_price", "calculation_id": "bad", "unit": "元/件"}
        result = self.batch(bad, dependent, "invalid item", cost("good"))
        self.assertEqual(result[0]["fields"][0]["path"], "inputs")
        self.assertEqual(result[1]["error"], "calculation.source")
        self.assertEqual(result[2]["error"], "invalid_contract")
        self.assertEqual(result[3]["value"], "7.5")
        self.assertEqual(list(self.tools.records), ["good"])
        self.assertEqual(self.batch(cost("bad"))[0]["value"], "7.5")

    def test_repeating_successful_item_in_different_batch_reuses_evidence(self):
        self.batch(cost())
        result = self.batch(cost(), cost("new"))
        self.assertEqual(result[0]["value"], "7.5")
        changed = self.batch(cost(quantity="6"))[0]
        self.assertEqual(changed["error"], "calculation.id")
        self.assertEqual(self.tools.records["operations-cost"].value, "7.5")
        self.assertEqual(len(self.tools.records), 2)

    def test_batch_binding_rejects_wrong_context_before_executing(self):
        with self.assertRaises(ERPError):
            self.tools.invoke(
                "erp_calculate_batch", {"context_hash": "foreign", "calculations": [cost()]}
            )
        self.assertFalse(self.tools.records)
        result = self.batch({**cost("wrong"), "context_hash": "foreign"}, cost())
        self.assertEqual(result[0]["error"], "planning.context")
        self.assertEqual(result[1]["value"], "7.5")

    def test_batch_and_loop_limits_are_bounded(self):
        with self.assertRaises(ValidationError):
            self.batch(*(cost(str(i)) for i in range(25)))
        for wave in range(4):
            result = self.batch(*(cost(str(wave * 24 + i)) for i in range(24)))
            self.assertTrue(all("value" in item for item in result))
        self.assertEqual(self.batch(cost("too-many"))[0]["error"], "calculation.limit")

    def test_cancellation_between_items_stops_batch(self):
        class StopSecond(Control):
            def checkpoint(self, stage, completed=0):
                super().checkpoint(stage, completed)
                if len(self.stages) == 2:
                    raise Cancelled()

        with self.assertRaises(Cancelled), run_control(StopSecond()):
            self.batch(cost("first"), cost("second"), cost("third"))
        self.assertEqual(list(self.tools.records), ["first"])

    def test_deadline_is_checked_between_items(self):
        budget = TaskCallBudget(1)
        original = self.tools.calculate

        def calculate(request):
            result = original(request)
            budget.deadline = 0
            return result

        with model_call(budget), patch.object(self.tools, "calculate", calculate):
            with self.assertRaises(ERPError) as caught:
                self.batch(cost("first"), cost("second"))
        self.assertEqual(caught.exception.code, "task_deadline")
        self.assertEqual(list(self.tools.records), ["first"])

    def test_seeded_records_retain_binding_and_support_later_batches(self):
        self.batch(cost())
        context = {
            **self.knowledge.data,
            "calculation_records": to_json(tuple(self.tools.records.values())),
            "accessed_reference_ids": ["cash"],
        }
        seeded = PlanningTools(context)
        self.assertEqual(seeded.accessed_reference_ids, {"cash"})
        self.assertEqual(
            seeded.invoke("erp_calculate_batch", {"calculations": [cost()]})["results"][0]["value"],
            "7.5",
        )
        context["calculation_records"][0]["context_hash"] = "foreign"
        with self.assertRaises(ERPError):
            PlanningTools(context)


class PlanningAnalysisRecoveryTests(unittest.TestCase):
    def test_isolated_application_recovers_analysis_after_two_native_calculation_batches(self):
        from tests.test_planning_pipeline import decisions
        from tests.test_web import WebTests

        case = WebTests(methodName="test_upload_read_revise_solve_and_isolation")
        models = []
        repairs = []

        def native_generate(request):
            context = request.tool_context
            cash = next(f["id"] for f in context["facts"] if f["key"] == "initial_cash")
            if request.task.id == "solver:operations":
                if "diagnostics" not in loads(request.user):
                    replies = [
                        tool_reply(
                            "erp_calculate_batch",
                            {
                                "calculations": [
                                    cost(),
                                    cost("operations-other", quantity="2", price="0.25"),
                                ]
                            },
                        ),
                        tool_reply(
                            "erp_calculate_batch",
                            {"calculations": [cost("operations-next", quantity="5", price="0.15")]},
                        ),
                        tool_reply(
                            "erp_planning_analysis",
                            finding(context["context_hash"], [cash], statement=""),
                        ),
                    ]
                else:
                    repairs.append(request)
                    self.assertEqual(len(context["calculation_records"]), 3)
                    replies = [
                        tool_reply(
                            "erp_planning_analysis",
                            finding(context["context_hash"], [cash, "operations-cost"]),
                        )
                    ]
            elif request.schema_name == "erp_planning_analysis":
                replies = [
                    tool_reply(
                        "erp_planning_analysis", {"summary": "按资金边界规划。", "findings": []}
                    )
                ]
            else:
                data = decisions(
                    context["context_hash"], context["years"] * context["periods_per_year"]
                )
                for actions in data["periods"].values():
                    for action in actions:
                        action["references"] = [cash]
                data["periods"]["1"][0].update(amount=None, amount_calculation_id="operations-cost")
                replies = [tool_reply(request.schema_name, data)]
            model = ToolChatModel(replies=replies)
            models.append(model)
            return LangChainLLMClient(
                Settings(llm_model="test", llm_api_key="fake"), lambda _: model
            ).generate(request)

        with patch("erp_agent.api.server.Handler.log_message"):
            case.setUp()
            try:
                project, reading = case.setup_problem()
                base = f"/api/projects/{project}"
                with patch.object(case.llm, "generate", side_effect=native_generate):
                    job = case.request(base + "/solve", {"version": reading["snapshot"]["version"]})
                    result = case.wait_job(project, job["id"])
                self.assertEqual(result["status"], "succeeded", result.get("error"))
                plan = case.request(base + "/solutions")[0]["plan"]
                self.assertEqual(len(repairs), 1)
                self.assertEqual(sum(m.calls for m in models), 6)
                self.assertEqual(plan["actions"][0]["amount"], "7.5")
                self.assertEqual(
                    [r["value"] for r in plan["preparation"]["calculations"]],
                    ["7.5", "0.50", "0.75"],
                )
            finally:
                case.tearDown()

    def test_calculation_reference_is_classified_without_extra_model_request(self):
        tools = PlanningTools(PlanningKnowledge(snapshot()).data)
        tools.invoke("erp_calculate", cost())
        result = validate_analysis(
            finding(tools.context["context_hash"], ["operations-cost"]),
            tools.context["context_hash"],
            {"cash"},
            {"cash"},
            tools.records,
        )
        self.assertEqual(result["findings"][0]["calculation_ids"], ["operations-cost"])
        self.assertEqual(result["findings"][0]["references"], [])

    def test_unknown_and_unread_references_have_distinct_field_diagnostics(self):
        for ref, allowed, code in [
            ("foreign", {"cash"}, "planning.reference"),
            ("cash", {"cash"}, "planning.not_loaded"),
        ]:
            with self.subTest(ref=ref), self.assertRaises(ERPError) as caught:
                validate_analysis(finding("hash", [ref]), "hash", allowed, set(), {})
            self.assertEqual(caught.exception.code, code)
            self.assertIn("findings[0].references", caught.exception.message)
            self.assertIn(ref, caught.exception.message)

    def test_one_repair_receives_all_independent_diagnostics(self):
        data = finding("hash", ["unknown"], statement="")
        data["summary"] = ""
        data["findings"].append(
            {
                "statement": "判断",
                "references": ["unread"],
                "implication": 1,
                "calculation_ids": ["unknown-calculation"],
            }
        )
        with self.assertRaises(ERPError) as caught:
            validate_analysis(data, "hash", {"unread"}, set(), {})
        for path in (
            "summary",
            "findings[0].statement",
            "findings[0].references",
            "findings[1].implication",
            "findings[1].calculation_ids",
            "findings[1].references",
        ):
            self.assertIn(path, caught.exception.message)

    def test_analysis_repair_keeps_lookup_and_calculations_for_coordinator(self):
        snap = snapshot()
        # 该事实不在原产销职责投影中；修正后不重复工具查询。
        hidden = replace(
            snap.reading.problem.facts[0],
            id="hidden-price",
            category="finance",
            entity_id="loan",
            key="fee",
            value="2.5",
            unit="元/件",
        )
        snap = replace(
            snap,
            reading=replace(
                snap.reading,
                problem=replace(snap.reading.problem, facts=snap.reading.problem.facts + (hidden,)),
            ),
        )
        client = PlannerClient()
        original = client.generate
        attempts = []

        def generate(request):
            if request.task.id != "solver:operations":
                return original(request)
            attempts.append(request)
            context_hash = request.tool_context["context_hash"]
            if len(attempts) == 1:
                tools = PlanningTools(request.tool_context)
                tools.invoke("planning_lookup", {"kind": "facts", "ids": ["hidden-price"]})
                args = cost()
                args["inputs"][1] = {
                    "name": "unit_price",
                    "fact_id": "hidden-price",
                    "unit": "元/件",
                }
                tools.invoke("erp_calculate_batch", {"calculations": [args]})
                return LLMResponse(
                    dumps(finding(context_hash, ["hidden-price"], statement="")),
                    "fake",
                    calculations=tuple(tools.records.values()),
                    accessed_reference_ids=tuple(tools.accessed_reference_ids),
                )
            self.assertIn("hidden-price", request.tool_context["accessed_reference_ids"])
            self.assertEqual(request.tool_context["calculation_records"][0]["value"], "7.5")
            self.assertIn("findings[0].statement", loads(request.user)["diagnostics"])
            return LLMResponse(
                dumps(finding(context_hash, ["hidden-price", "operations-cost"])), "fake"
            )

        client.generate = generate
        proposal = ParallelPlanningGenerator(client, Control()).propose(snap)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(proposal.baseline.preparation.calculations[0].value, "7.5")
        synthesis = next(r for r in client.calls if r.schema_name == "erp_planning_decisions")
        findings = loads(synthesis.user)["findings"][0]["findings"][0]
        self.assertEqual(findings["calculation_ids"], ["operations-cost"])
        self.assertEqual(findings["references"], ["hidden-price"])

    def test_failed_repair_reports_role_field_and_unknown_id(self):
        client = PlannerClient()
        original = client.generate
        attempts = []

        def generate(request):
            if request.task.id == "solver:operations":
                attempts.append(request)
                return LLMResponse(
                    dumps(finding(request.tool_context["context_hash"], ["invented-fact"])), "fake"
                )
            return original(request)

        client.generate = generate
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control()).propose(snapshot())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(caught.exception.code, "planning.reference")
        for text in ("产销与资源分析", "findings[0].references", "invented-fact"):
            self.assertIn(text, caught.exception.message)
        self.assertFalse(any(r.schema_name == "erp_planning_decisions" for r in client.calls))
