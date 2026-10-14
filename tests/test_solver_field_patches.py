import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_action, dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import LLMStrategyGenerator
from erp_agent.application.solver_collaboration import CollaborativeStrategyGenerator
from erp_agent.application.solver_context import planning_requirements
from erp_agent.application.solver_repairs import apply_specialist_patch, specialist_patch
from erp_agent.application.solver_specialist import decode_specialist
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
    TemplateInputGroup,
    TemplateReading,
    TemplateSheetReading,
)
from tests.helpers import Control, snapshot, structured_action, structured_proposal_json
from tests.test_solver_dependency_flow import specialist
from tests.test_solver_repair_flow import problem, timed_candidates


class SolverFieldPatchTests(unittest.TestCase):
    def test_dependency_patch_schema_only_returns_dates_and_preserves_condition(self):
        data = specialist("operations")
        data["candidate_actions"] = timed_candidates()
        data["candidate_actions"][1]["condition"] = {
            "kind": "fact",
            "reference_id": "order-size",
            "operator": "exists",
            "value": None,
            "explanation": "仅作为待竞目标，实际赢单后才执行交付。",
        }
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, problem())
        repair = specialist_patch(data, caught.exception)
        variants = repair.schema["properties"]["patches"]["items"]["anyOf"]
        self.assertTrue(
            all(
                set(v["properties"]["replacement"]["properties"])
                == {"period", "completion_period", "payment_period"}
                for v in variants
            )
        )
        updated = apply_specialist_patch(
            data,
            {
                "patches": [
                    {"collection": "candidate_actions", "index": 1, "replacement": {"period": 2}},
                    {"collection": "candidate_actions", "index": 2, "replacement": {"period": 3}},
                ]
            },
            repair,
        )
        decode_specialist(updated, problem())
        self.assertEqual(
            updated["candidate_actions"][1]["condition"], data["candidate_actions"][1]["condition"]
        )
        self.assertEqual(updated["candidate_actions"][0], data["candidate_actions"][0])
        with self.assertRaises(ERPError) as changed:
            apply_specialist_patch(
                data,
                {
                    "patches": [
                        {
                            "collection": "candidate_actions",
                            "index": 1,
                            "replacement": {
                                "period": 2,
                                "condition": structured_action()["condition"],
                            },
                        },
                    ]
                },
                repair,
            )
        self.assertEqual(changed.exception.code, "patch_scope")

    def test_all_errors_in_one_risk_enter_one_sparse_repair(self):
        data = specialist("operations")
        data["risks"] = [
            {
                "id": "risk",
                "periods": [],
                "action_ids": [],
                "gap_ids": [],
                "fact_ids": ["wrong"],
                "rule_ids": [],
                "response": "",
                "condition": {
                    "kind": "fact",
                    "reference_id": "wrong",
                    "operator": "exists",
                    "value": None,
                    "explanation": "核对",
                },
            }
        ]
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, snapshot())
        repair = specialist_patch(data, caught.exception)
        self.assertEqual(repair.allowed_fields[("risks", 0)], {"fact_ids", "condition", "response"})
        updated = apply_specialist_patch(
            data,
            {
                "collection": "risks",
                "index": 0,
                "replacement": {
                    "fact_ids": ["cash"],
                    "condition": structured_action()["condition"],
                    "response": "核对实际资金再执行投入。",
                },
            },
            repair,
        )
        decode_specialist(updated, snapshot())
        self.assertEqual(updated["risks"][0]["id"], "risk")

    def test_malformed_action_does_not_hide_later_condition_error_or_shift_index(self):
        data = specialist("operations")
        bad_structure = structured_action(amount="10")
        bad_structure["details"] = {}
        bad_structure["condition"]["reference_id"] = "wrong"
        bad_condition = structured_action(index=1, amount="10")
        bad_condition["condition"]["reference_id"] = "wrong"
        data["candidate_actions"] = [bad_structure, bad_condition]
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, snapshot())
        repair = specialist_patch(data, caught.exception)
        self.assertEqual(repair.allowed_fields[("candidate_actions", 0)], {"details", "condition"})
        self.assertEqual(repair.allowed_fields[("candidate_actions", 1)], {"condition"})

    def test_coordinator_date_patch_preserves_all_unopened_fields(self):
        raw = loads(structured_proposal_json())
        raw["plan"]["actions"][1]["period"] = 1
        raw["plan"]["actions"][0]["completion_period"] = 2
        raw["plan"]["actions"][1]["depends_on"] = ["action-0"]
        calls = []
        outer = self

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                if request.schema_name == "erp_strategy":
                    return LLMResponse(dumps(raw), "test")
                schema = loads(request.schema_json)
                props = schema["properties"]["patches"]["items"]["anyOf"][0]["properties"][
                    "replacement"
                ]["properties"]
                outer.assertNotIn("condition", props)
                return LLMResponse(
                    dumps(
                        {
                            "patches": [
                                {"action_id": "action-1", "replacement": {"period": 2}},
                            ],
                            "additions": [],
                        }
                    ),
                    "test",
                )

        plan = LLMStrategyGenerator(Client()).propose(snapshot()).baseline
        self.assertEqual(plan.actions[1].period, 2)
        self.assertEqual(
            plan.actions[1].condition, decode_action(raw["plan"]["actions"][1]).condition
        )
        self.assertEqual(calls, ["erp_strategy", "erp_action_patch"])

    def test_planning_parameters_are_compact_and_reach_operations_without_raw_formulas(self):
        group = TemplateInputGroup("采购支出", "B2:B99", "元", "按季度安排", ("来源",))
        reading = TemplateReading(
            "template",
            "hash",
            (
                TemplateSheetReading(
                    "资金", "支出计划", "全周期", (group, replace(group, range="C2:C99"))
                ),
            ),
            reviewed=True,
        )
        value = replace(snapshot(), template_readings=(reading,))
        template = BudgetTemplate(
            SourceDocument("template", "budget.xlsx", "hash", 1, "xlsx"),
            (BudgetSheet("资金", 99, 3, (BudgetCell("A1", "=SUM(B2:B99)", formula=True),)),),
        )
        context = PlanningContext(GenerationConfig(template_ids=("template",)), (template,))
        requirements = planning_requirements(value)
        self.assertEqual(len(requirements[0]["sheets"][0]["required_parameters"]), 1)
        self.assertEqual(
            requirements[0]["sheets"][0]["required_parameters"][0]["depends_on_sheets"], ["来源"]
        )
        outer = self

        class Client:
            configured = True

            def generate(self, request):
                material = loads(request.user)
                outer.assertNotIn("=SUM", request.user)
                outer.assertNotIn("B2:B99", request.user)
                outer.assertEqual(material["template_requirements"], requirements)
                if request.schema_name == "erp_strategy":
                    return LLMResponse(structured_proposal_json(), "test")
                return LLMResponse(dumps(specialist(material["role"])), "test")

        CollaborativeStrategyGenerator(Client(), Control()).propose(value, context)

    def test_finance_added_action_sources_reach_coordinator(self):
        value = snapshot()
        fact = Fact(
            "carbon-fee",
            "carbon",
            "carbon",
            "碳费用",
            "fee",
            "10",
            "number",
            "元",
            KnowledgeState.KNOWN,
            ("carbon-source",),
        )
        value = replace(
            value,
            reading=replace(
                value.reading,
                problem=replace(value.reading.problem, facts=value.reading.problem.facts + (fact,)),
            ),
        )
        finance = structured_action(index=10, amount="10")
        finance["fact_ids"] = ["carbon-fee"]
        outer = self

        class Client:
            configured = True

            def generate(self, request):
                material = loads(request.user)
                if request.schema_name == "erp_strategy":
                    outer.assertIn("carbon-fee", dumps(material["snapshot"]["problem"]))
                    output = loads(structured_proposal_json())
                    output["plan"]["actions"].insert(1, finance)
                    return LLMResponse(dumps(output), "test")
                data = specialist(material["role"])
                if material["role"] == "finance":
                    data.update(status="applicable", candidate_actions=[finance])
                return LLMResponse(dumps(data), "test")

        plan = CollaborativeStrategyGenerator(Client(), Control()).propose(value).baseline
        self.assertIn("action-10", {a.action_id for a in plan.actions})


if __name__ == "__main__":
    unittest.main()
