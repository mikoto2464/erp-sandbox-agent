import unittest
from dataclasses import replace

from erp_agent.application.codec import solution_bundle
from erp_agent.application.contracts import (
    apply_action_patch,
    decode_action,
    decode_proposal,
    dumps,
    loads,
)
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import DraftPlanEvaluator, LLMStrategyGenerator, SolverService
from erp_agent.domain.action_validation import validate_actions
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import SolveRequest, SolverMode
from tests.helpers import (
    Control,
    FakeLLM,
    proposal_json,
    snapshot,
    structured_action,
    structured_proposal_json,
)


class ActionContractTests(unittest.TestCase):
    def test_legacy_snapshot_ids_are_stable_without_inferred_facts(self):
        old = decode_proposal(proposal_json(), "old").baseline
        reread = decode_proposal(dumps({"plan": old, "parameters": []}), "old").baseline
        self.assertEqual(old, reread)
        self.assertEqual(old.actions[0].kind, "reserve")
        self.assertEqual(old.actions[0].fact_ids, ())
        self.assertIsNone(old.actions[0].completion_period)
        with self.assertRaises(ERPError):
            decode_proposal(proposal_json(), "new", allow_legacy=False)

    def test_details_follow_finite_type_and_money_role(self):
        raw = structured_action(amount="10")
        raw["kind"] = "delivery"
        with self.assertRaises(ERPError):
            decode_action(raw)
        raw["details"] = {"order_entity_id": "company", "product_entity_id": None}
        plan = decode_proposal(structured_proposal_json(), "new").baseline
        plan = replace(plan, actions=(decode_action(raw), plan.actions[1]))
        errors = validate_actions(snapshot(), plan)
        self.assertIn("plan.cash_meaning", {i.code for i in errors})
        self.assertTrue(
            any("action-0" in i.message and "amount_meaning" in i.target for i in errors)
        )

    def test_known_dependency_time_and_duplicate_id_located(self):
        plan = decode_proposal(structured_proposal_json(), "new").baseline
        earlier = replace(plan.actions[0], completion_period=2)
        later = replace(plan.actions[1], period=1, depends_on=(earlier.action_id,))
        errors = validate_actions(
            snapshot(), replace(plan, actions=(earlier, later)), coverage=False
        )
        self.assertIn("plan.dependency_time", {i.code for i in errors})
        repeated = replace(plan.actions[1], action_id=earlier.action_id)
        self.assertIn(
            "plan.duplicate_id",
            {
                i.code
                for i in validate_actions(snapshot(), replace(plan, actions=(earlier, repeated)))
            },
        )

    def test_local_patch_changes_only_allowed_stable_action(self):
        plan = decode_proposal(structured_proposal_json(), "new").baseline
        replacement = structured_action(amount="9")
        patched = apply_action_patch(
            plan, {"patches": [{"action_id": "action-0", "replacement": replacement}]}, {"action-0"}
        )
        self.assertEqual(patched.actions[1], plan.actions[1])
        self.assertEqual(str(patched.actions[0].amount), "9")
        with self.assertRaises(ERPError):
            apply_action_patch(
                plan,
                {"patches": [{"action_id": "action-0", "replacement": replacement}]},
                {"action-1"},
            )
        self.assertEqual(str(plan.actions[0].amount), "10")

    def test_solver_repairs_only_error_action_and_revalidates(self):
        payload = loads(structured_proposal_json())
        payload["plan"]["actions"][0]["entity_id"] = "unknown"
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request)
                if request.schema_name == "erp_action_patch":
                    data = loads(request.user)
                    self_outer.assertEqual([o["action_id"] for o in data["objects"]], ["action-0"])
                    return LLMResponse(
                        dumps(
                            {
                                "patches": [
                                    {
                                        "action_id": "action-0",
                                        "replacement": structured_action(amount="10"),
                                    }
                                ]
                            }
                        ),
                        "same",
                    )
                return LLMResponse(dumps(payload), "same")

        self_outer = self
        result = LLMStrategyGenerator(Client()).propose(snapshot())
        self.assertEqual([r.schema_name for r in calls], ["erp_strategy", "erp_action_patch"])
        self.assertEqual(result.baseline.actions[0].entity_id, "company")
        self.assertEqual(result.baseline.actions[1].action_id, "action-1")

    def test_actions_save_before_budget_and_round_trip(self):
        from erp_agent.domain.models import (
            BudgetCell,
            BudgetSheet,
            BudgetTemplate,
            GenerationConfig,
            PlanningContext,
            SourceDocument,
        )

        template = BudgetTemplate(
            SourceDocument("template", "budget.xlsx", "hash", 1, "xlsx"),
            (BudgetSheet("预算", 2, 2, (BudgetCell("A2", "投入"), BudgetCell("B2", ""))),),
        )
        llm = FakeLLM(structured_proposal_json())
        result = SolverService(LLMStrategyGenerator(llm), DraftPlanEvaluator()).solve(
            SolveRequest(
                snapshot(),
                SolverMode.LLM_ONLY,
                PlanningContext(GenerationConfig(template_ids=("template",)), (template,)),
            ),
            Control(),
        )
        self.assertEqual(result.plan.budget_items, ())
        self.assertTrue(result.evaluation.feasible)
        self.assertFalse(result.evaluation.verified)
        reread = solution_bundle(loads(dumps(result)))
        self.assertEqual(reread.plan.actions, result.plan.actions)

    def test_invalid_details_repair_uses_only_one_action(self):
        payload = loads(structured_proposal_json())
        payload["plan"]["actions"][0]["details"] = {"target_entity_id": "company"}
        calls = []
        outer = self

        class Client:
            def generate(self, request):
                calls.append(request)
                if request.schema_name == "erp_action_patch":
                    material = loads(request.user)
                    outer.assertNotIn("plan", material)
                    outer.assertEqual(material["action_id"], "action-0")
                    return LLMResponse(
                        dumps(
                            {
                                "patches": [
                                    {
                                        "action_id": "action-0",
                                        "replacement": structured_action(amount="10"),
                                    }
                                ]
                            }
                        ),
                        "same",
                    )
                return LLMResponse(dumps(payload), "same")

        plan = LLMStrategyGenerator(Client()).propose(snapshot()).baseline
        self.assertEqual(plan.actions[1].action_id, "action-1")
        self.assertEqual([r.schema_name for r in calls], ["erp_strategy", "erp_action_patch"])

    def test_failed_patch_never_escalates_to_full_strategy_regeneration(self):
        payload = loads(structured_proposal_json())
        payload["plan"]["actions"][0]["entity_id"] = "unknown"
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                if request.schema_name == "erp_action_patch":
                    return LLMResponse(
                        dumps(
                            {
                                "patches": [
                                    {
                                        "action_id": "action-1",
                                        "replacement": structured_action(index=1, period=2),
                                    }
                                ]
                            }
                        ),
                        "same",
                    )
                return LLMResponse(dumps(payload), "same")

        with self.assertRaises(ERPError):
            LLMStrategyGenerator(Client()).propose(snapshot())
        self.assertEqual(calls, ["erp_strategy", "erp_action_patch"])

    def test_scoped_patch_cannot_change_unrelated_money(self):
        plan = decode_proposal(structured_proposal_json(), "new").baseline
        patch = {
            "patches": [{"action_id": "action-0", "replacement": structured_action(amount="999")}]
        }
        with self.assertRaises(ERPError):
            apply_action_patch(plan, patch, {"action-0"}, {"action-0": {"entity_id"}})
        self.assertEqual(str(plan.actions[0].amount), "10")
