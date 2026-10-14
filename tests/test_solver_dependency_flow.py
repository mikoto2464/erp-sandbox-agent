import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from erp_agent.adapters.memory import SQLiteMemoryService
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver_collaboration import (
    CollaborativeStrategyGenerator,
    decode_specialist,
)
from erp_agent.application.solver_context import role_material
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetCell,
    BudgetSheet,
    BudgetTemplate,
    Fact,
    GenerationConfig,
    KnowledgeState,
    PlanningContext,
    SourceDocument,
    TemplateReading,
    TemplateSheetReading,
)
from tests.helpers import Control, snapshot, structured_action, structured_proposal_json


def specialist(role):
    return {
        "status": "applicable" if role == "operations" else "not_applicable",
        "reason": "候选行动" if role == "operations" else "本任务暂无额外建议。",
        "candidate_actions": [structured_action(amount="10")] if role == "operations" else [],
        "recommendations": [],
        "dependencies": [],
        "risks": [],
    }


class SolverDependencyFlowTests(unittest.TestCase):
    def test_operations_and_template_independent_finance_waits(self):
        barrier = threading.Barrier(2)
        completed = set()
        calls = []
        template = BudgetTemplate(
            SourceDocument("template", "预算.xlsx", "hash", 1, "xlsx"),
            (BudgetSheet("预算", 2, 2, (BudgetCell("B2", ""),)),),
        )
        problem = replace(
            snapshot(),
            template_readings=(
                TemplateReading(
                    "template",
                    "hash",
                    (TemplateSheetReading("预算", "投入", "第1期"),),
                    reviewed=True,
                ),
            ),
        )
        context = PlanningContext(GenerationConfig(template_ids=("template",)), (template,))
        outer = self

        class Client:
            configured = True

            def generate(self, request):
                payload = loads(request.user)
                role = payload.get("role", "coordinator")
                calls.append(role)
                if role in {"operations", "budget"}:
                    barrier.wait(2)
                    completed.add(role)
                if role == "finance":
                    outer.assertIn("operations", completed)
                    outer.assertEqual(payload["candidate_plan"]["actions"][0][0], "action-0")
                if role == "coordinator":
                    outer.assertEqual(len(payload["collaboration"]), 3)
                    outer.assertNotIn("raw_response", request.user)
                    return LLMResponse(structured_proposal_json(), "coordinator")
                return LLMResponse(dumps(specialist(role)), role)

        result = CollaborativeStrategyGenerator(Client(), Control()).propose(problem, context)
        self.assertEqual(result.baseline.budget_items, ())
        self.assertEqual(calls[-2:], ["finance", "coordinator"])

    def test_empty_applicable_not_cached_and_missing_input_is_actionable(self):
        data = specialist("operations")
        data["candidate_actions"] = []
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, snapshot())
        self.assertEqual(caught.exception.code, "solver_empty")
        data["status"] = "missing_input"
        data["reason"] = "缺少订单交付条件，影响交付行动。"
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, snapshot())
        self.assertIn("影响交付行动", caught.exception.message)

    def test_source_patch_only_receives_invalid_recommendation(self):
        calls = []
        bad = specialist("operations")
        recommendation = {
            "id": "rec-0",
            "entity_id": "company",
            "periods": [1],
            "action_ids": ["action-0"],
            "fact_ids": ["invented"],
            "rule_ids": [],
            "gap_ids": [],
            "explanation": "先安排投入。",
        }
        bad["recommendations"] = [recommendation]
        outer = self

        class Client:
            configured = True

            def generate(self, request):
                calls.append(request.schema_name)
                data = loads(request.user)
                if request.schema_name == "erp_specialist_patch":
                    outer.assertNotIn("problem", data)
                    outer.assertEqual(data["object"]["collection"], "recommendations")
                    return LLMResponse(
                        dumps(
                            {
                                "collection": "recommendations",
                                "index": 0,
                                "replacement": {**recommendation, "fact_ids": ["cash"]},
                            }
                        ),
                        "same",
                    )
                if request.schema_name == "erp_strategy":
                    return LLMResponse(structured_proposal_json(), "coordinator")
                return LLMResponse(
                    dumps(bad if data["role"] == "operations" else specialist("finance")), "same"
                )

        CollaborativeStrategyGenerator(Client(), Control()).propose(snapshot())
        self.assertEqual(calls.count("erp_specialist_patch"), 1)
        self.assertEqual(calls.count("erp_solver_specialist"), 2)

    def test_successful_operations_resume_after_finance_failure(self):
        calls = []

        class Client:
            configured = True
            failing = True

            def generate(self, request):
                data = loads(request.user)
                role = data.get("role", "coordinator")
                calls.append(role)
                if role == "finance" and self.failing:
                    raise ERPError("llm_gateway_timeout", "HTTP 504", 504)
                if role == "coordinator":
                    return LLMResponse(structured_proposal_json(), "coordinator")
                return LLMResponse(dumps(specialist(role)), role)

        with (
            TemporaryDirectory() as directory,
            patch("erp_agent.application.llm_recovery.time.sleep"),
            patch(
                "erp_agent.application.llm_recovery.wait_random_exponential",
                return_value=lambda state: 0,
            ),
        ):
            path = Path(directory) / "memory.db"
            SQLiteRepository(path)
            memory = SQLiteMemoryService(path)
            try:
                problem = snapshot()
                scope = memory.scope(problem.project_id, problem.version, "flow")
                client = Client()
                with self.assertRaises(ERPError):
                    CollaborativeStrategyGenerator(client, Control(), scope).propose(problem)
                client.failing = False
                result = CollaborativeStrategyGenerator(client, Control(), scope).propose(problem)
                self.assertEqual(result.model, "coordinator")
                self.assertEqual(calls.count("operations"), 1)
                self.assertEqual(calls.count("finance"), 3)
            finally:
                memory.close()

    def test_finance_excludes_order_details_preserves_reference(self):
        problem = snapshot()
        fact = Fact(
            "order-quantity",
            "order_pool",
            "order",
            "订单",
            "quantity",
            "10",
            "integer",
            "件",
            KnowledgeState.KNOWN,
            ("source",),
        )
        problem = replace(
            problem,
            reading=replace(
                problem.reading,
                problem=replace(
                    problem.reading.problem, facts=problem.reading.problem.facts + (fact,)
                ),
            ),
        )
        material = role_material(problem, "finance")
        self.assertNotIn("order-quantity", dumps(material))
        operations = role_material(problem, "operations")
        self.assertIn("order-quantity", dumps(operations))
        self.assertIn("source", dumps(operations))

    def test_independent_rule_provenance_is_preserved_without_fact_overlap(self):
        from erp_agent.domain.models import Rule

        problem = snapshot()
        rule = Rule(
            "payment-order",
            "费用支付顺序",
            "先付工资再开产",
            KnowledgeState.KNOWN,
            ("independent-paragraph",),
        )
        problem = replace(
            problem,
            reading=replace(problem.reading, rules=replace(problem.reading.rules, rules=(rule,))),
        )
        for role in ("operations", "finance", "coordinator"):
            self.assertIn("payment-order", dumps(role_material(problem, role)))
