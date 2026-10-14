import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_action, dumps, loads
from erp_agent.application.model_dispatch import task_responsibility
from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
from erp_agent.application.ports import LLMResponse, ModelBinding
from erp_agent.application.solver_collaboration import CollaborativeStrategyGenerator
from erp_agent.application.solver_context import role_material
from erp_agent.application.solver_repairs import apply_specialist_patch, specialist_patch
from erp_agent.application.solver_specialist import decode_specialist
from erp_agent.application.task_suitability import classify_task
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
from tests.test_solver_dependency_flow import specialist


def problem():
    value = snapshot()
    facts = value.reading.problem.facts + (
        Fact(
            "research-period",
            "product",
            "product-alpha",
            "产品甲",
            "rd_period",
            "2",
            "integer",
            "期",
            KnowledgeState.KNOWN,
            ("research-source",),
        ),
        Fact(
            "order-size",
            "order_pool",
            "order-alpha",
            "待竞订单甲",
            "quantity",
            "10",
            "integer",
            "件",
            KnowledgeState.KNOWN,
            ("order-source",),
        ),
    )
    return replace(
        value,
        reading=replace(
            value.reading,
            problem=replace(
                value.reading.problem,
                periods_per_year=4,
                facts=facts,
            ),
        ),
    )


def timed_candidates():
    research = structured_action()
    research.update(
        action_id="research",
        kind="research",
        entity_id="product-alpha",
        completion_period=2,
        amount_meaning="unknown",
        fact_ids=["research-period"],
        details={"target_entity_id": "product-alpha"},
    )
    bid = structured_action(index=1)
    bid.update(
        action_id="bid",
        kind="bid",
        entity_id="order-alpha",
        depends_on=["research"],
        amount_meaning="unknown",
        fact_ids=["order-size"],
        details={"order_entity_id": "order-alpha", "product_entity_id": "product-alpha"},
    )
    delivery = {**bid, "action_id": "delivery", "kind": "delivery", "depends_on": ["bid"]}
    return [research, bid, delivery]


def recommendation(index, entity="company"):
    return {
        "id": f"rec-{index}",
        "entity_id": entity,
        "periods": [],
        "action_ids": [],
        "fact_ids": ["cash"],
        "rule_ids": [],
        "gap_ids": [],
        "explanation": "核对现有输入与来源。",
    }


class SolverRepairFlowTests(unittest.TestCase):
    def test_dependency_repair_has_upstream_dates_sources_and_downstream_scope(self):
        data = specialist("operations")
        original = timed_candidates()
        data["candidate_actions"] = original
        outer = self
        calls = []

        class Client:
            configured = True

            def generate(self, request):
                calls.append(request.schema_name)
                material = loads(request.user)
                if request.schema_name == "erp_specialist_patch":
                    outer.assertEqual(
                        [(o["collection"], o["index"]) for o in material["objects"]],
                        [("candidate_actions", 1), ("candidate_actions", 2)],
                    )
                    related = {a[0]: a for a in material["related_actions"]["actions"]}
                    outer.assertEqual(related["research"][11], 2)
                    outer.assertIn("research-source", request.user)
                    outer.assertIn("order-size", request.user)
                    outer.assertNotIn("depends_on", material["objects"][0]["allowed_fields"])
                    return LLMResponse(
                        dumps(
                            {
                                "patches": [
                                    {
                                        "collection": "candidate_actions",
                                        "index": 1,
                                        "replacement": {**original[1], "period": 2},
                                    },
                                    {
                                        "collection": "candidate_actions",
                                        "index": 2,
                                        "replacement": {**original[2], "period": 3},
                                    },
                                ]
                            }
                        ),
                        "same",
                    )
                if request.schema_name == "erp_strategy":
                    output = loads(structured_proposal_json())
                    output["plan"]["actions"] = [
                        original[0],
                        {**original[1], "period": 2},
                        {**original[2], "period": 3},
                        structured_action(index=4, period=4),
                    ]
                    return LLMResponse(dumps(output), "same")
                return LLMResponse(
                    dumps(data if material["role"] == "operations" else specialist("finance")),
                    "same",
                )

        plan = CollaborativeStrategyGenerator(Client(), Control()).propose(problem()).baseline
        self.assertEqual(plan.actions[0], decode_action(original[0]))
        self.assertEqual(plan.actions[1].depends_on, ("research",))
        self.assertEqual(plan.actions[1].period, 2)
        self.assertEqual(plan.actions[2].period, 3)
        self.assertEqual(calls.count("erp_specialist_patch"), 1)

    def test_temporal_patch_cannot_remove_dependency_or_change_unrelated_money(self):
        data = specialist("operations")
        data["candidate_actions"] = timed_candidates()
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, problem())
        repair = specialist_patch(data, caught.exception)
        for changed in ({"depends_on": []}, {"amount": "999", "amount_unit": "元"}):
            with self.subTest(changed=changed), self.assertRaises(ERPError) as failure:
                apply_specialist_patch(
                    data,
                    {
                        "patches": [
                            {
                                "collection": "candidate_actions",
                                "index": 1,
                                "replacement": {
                                    **data["candidate_actions"][1],
                                    "period": 2,
                                    **changed,
                                },
                            }
                        ]
                    },
                    repair,
                )
            self.assertEqual(failure.exception.code, "patch_scope")

    def test_multiple_unknown_entities_are_repaired_together_and_template_role_is_budget(self):
        value = problem()
        template = BudgetTemplate(
            SourceDocument("template-alpha", "budget.xlsx", "h", 1, "xlsx"),
            (BudgetSheet("投入", 1, 1, (BudgetCell("A1", ""),)),),
        )
        value = replace(
            value,
            template_readings=(
                TemplateReading(
                    "template-alpha",
                    "h",
                    (TemplateSheetReading("投入", "计划投入", ""),),
                    reviewed=True,
                ),
            ),
        )
        context = PlanningContext(GenerationConfig(template_ids=("template-alpha",)), (template,))
        bad = specialist("budget")
        bad.update(
            status="applicable",
            recommendations=[
                recommendation(0, "template-alpha"),
                recommendation(1, "input-group-alpha"),
            ],
        )
        calls = []
        outer = self

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                data = loads(request.user)
                calls.append((self.name, request.schema_name))
                if request.task.id == "solver:template_requirements":
                    outer.assertEqual(task_responsibility(request), "budget")
                    outer.assertEqual(self.name, "light")
                    if request.schema_name == "erp_specialist_patch":
                        outer.assertEqual([o["index"] for o in data["objects"]], [0, 1])
                        outer.assertIn("company", {e["id"] for e in data["entity_catalog"]})
                        return LLMResponse(
                            dumps(
                                {
                                    "patches": [
                                        {
                                            "collection": "recommendations",
                                            "index": i,
                                            "replacement": recommendation(i),
                                        }
                                        for i in (0, 1)
                                    ]
                                }
                            ),
                            self.name,
                        )
                    outer.assertEqual(classify_task(request).difficulty, "simple")
                    return LLMResponse(dumps(bad), self.name)
                if request.schema_name == "erp_strategy":
                    result = loads(structured_proposal_json())
                    result["plan"]["actions"] += [
                        structured_action(index=3, period=3),
                        structured_action(index=4, period=4),
                    ]
                    return LLMResponse(dumps(result), self.name)
                return LLMResponse(dumps(specialist(data["role"])), self.name)

        pool = ScheduledLLMClient(
            tuple(
                ModelBinding(
                    n,
                    n,
                    1,
                    {
                        "model": n,
                        "priority": p,
                        "task_tier": t,
                        "max_parallel": 2,
                        "max_input_chars": 100000,
                    },
                    Client(n),
                )
                for n, p, t in (("light", 1000, "light"), ("strong", 1, "strong"))
            ),
            ModelCapacity(),
        )
        CollaborativeStrategyGenerator(pool, Control()).propose(value, context)
        self.assertEqual(calls.count(("light", "erp_specialist_patch")), 1)

    def test_explicit_order_references_survive_finance_and_coordinator_context(self):
        action = decode_action(timed_candidates()[1])
        for role in ("finance", "coordinator"):
            self.assertIn("order-size", dumps(role_material(problem(), role, (action,))))
            self.assertNotIn("order-size", dumps(role_material(problem(), role)))

    def test_patch_stays_invalid_when_not_all_errors_are_fixed(self):
        data = specialist("operations")
        data["recommendations"] = [recommendation(0, "wrong-a"), recommendation(1, "wrong-b")]
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, problem())
        repair = specialist_patch(data, caught.exception)
        updated = apply_specialist_patch(
            data,
            {
                "patches": [
                    {"collection": "recommendations", "index": 0, "replacement": recommendation(0)}
                ]
            },
            repair,
        )
        with self.assertRaises(ERPError):
            decode_specialist(updated, problem())
        self.assertEqual(data["recommendations"][0]["entity_id"], "wrong-a")

    def test_malformed_action_and_unknown_entity_share_one_repair_scope(self):
        data = specialist("operations")
        data["candidate_actions"][0]["details"] = {"target_entity_id": "company"}
        data["recommendations"] = [recommendation(0, "template-id")]
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, problem())
        repair = specialist_patch(data, caught.exception)
        self.assertEqual(
            [(o["collection"], o["index"]) for o in repair.objects],
            [("candidate_actions", 0), ("recommendations", 0)],
        )
        self.assertEqual(repair.allowed_fields[("candidate_actions", 0)], {"details"})
        updated = apply_specialist_patch(
            data,
            {
                "patches": [
                    {
                        "collection": "candidate_actions",
                        "index": 0,
                        "replacement": structured_action(amount="10"),
                    },
                    {"collection": "recommendations", "index": 0, "replacement": recommendation(0)},
                ]
            },
            repair,
        )
        decode_specialist(updated, problem())

    def test_template_role_cannot_generate_business_actions_and_risk_condition_is_checked(self):
        with self.assertRaises(ERPError) as caught:
            decode_specialist(specialist("operations"), problem(), role="budget")
        self.assertEqual(caught.exception.code, "solver_role_scope")
        data = specialist("operations")
        data["risks"] = [
            {
                "id": "risk",
                "periods": [],
                "action_ids": [],
                "gap_ids": [],
                "fact_ids": ["cash"],
                "rule_ids": [],
                "response": "核对资金。",
                "condition": {
                    "kind": "always",
                    "reference_id": "cash",
                    "operator": None,
                    "value": None,
                    "explanation": "",
                },
            }
        ]
        with self.assertRaises(ERPError):
            decode_specialist(data, problem())

    def test_large_downstream_repair_stops_at_scope_limit(self):
        data = specialist("operations")
        data["candidate_actions"] = timed_candidates()
        previous = "delivery"
        for i in range(25):
            action = {
                **data["candidate_actions"][2],
                "action_id": f"follow-{i}",
                "depends_on": [previous],
            }
            data["candidate_actions"].append(action)
            previous = action["action_id"]
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, problem())
        with self.assertRaises(ERPError) as bounded:
            specialist_patch(data, caught.exception)
        self.assertEqual(bounded.exception.code, "solver_repair_limit")
