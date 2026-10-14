import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_proposal, dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import LLMStrategyGenerator
from erp_agent.application.solver_collaboration import CollaborativeStrategyGenerator
from erp_agent.application.solver_period_output import (
    flatten_period_proposal,
    period_proposal_schema,
)
from erp_agent.domain.action_validation import validate_actions
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import GenerationConfig, PlanningContext, PlanTone
from tests.helpers import Control, snapshot, structured_action, structured_proposal_json
from tests.test_solver_dependency_flow import specialist
from tests.test_solver_repair_flow import problem


def period_output(horizon):
    data = loads(structured_proposal_json())
    actions = {}
    for period in range(1, horizon + 1):
        action = structured_action(
            index=period, period=period, amount="10", description="安排本期费用支付并保留资金约束。"
        )
        action.pop("period")
        actions[str(period)] = [action]
    data["plan"].pop("actions")
    data["plan"]["period_actions"] = actions
    return data


class AutonomousPlanningTests(unittest.TestCase):
    def test_full_cycle_groups_expand_once_and_review_never_has_construction_dates(self):
        value = replace(
            snapshot(),
            reading=replace(
                snapshot().reading,
                problem=replace(snapshot().reading.problem, years=4, periods_per_year=4),
            ),
        )
        data = period_output(16)
        review = structured_action(index=70, period=7, description="盘点本期库存与债务台账。")
        review.update(completion_period=2, payment_period=99)
        review.pop("period")
        data["plan"]["period_actions"]["7"].append(review)
        calls = []
        outer = self

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                schema = loads(request.schema_json)
                periods = schema["properties"]["plan"]["properties"]["period_actions"]
                outer.assertEqual(set(periods["required"]), {str(i) for i in range(1, 17)})
                return LLMResponse(dumps(data), "controlled")

        plan = LLMStrategyGenerator(Client()).propose(value).baseline
        self.assertEqual({a.period for a in plan.actions}, set(range(1, 17)))
        checked = next(a for a in plan.actions if a.action_id == "action-70")
        self.assertIsNone(checked.completion_period)
        self.assertIsNone(checked.payment_period)
        self.assertEqual(calls, ["erp_strategy"])
        self.assertEqual(data["plan"]["period_actions"]["7"][1]["completion_period"], 2)

    def test_missing_middle_groups_empty_groups_and_wrong_period_cannot_succeed(self):
        for change in ("missing", "empty", "outside", "wrong_period"):
            data = period_output(4)
            if change == "missing":
                data["plan"]["period_actions"].pop("2")
            if change == "empty":
                data["plan"]["period_actions"]["3"] = []
            if change == "outside":
                data["plan"]["period_actions"]["5"] = []
            if change == "wrong_period":
                data["plan"]["period_actions"]["2"][0]["period"] = 7
            with self.subTest(change=change), self.assertRaises(ERPError):
                flatten_period_proposal(data, 4)

    def test_real_business_completion_and_sources_still_validate(self):
        data = period_output(2)
        action = data["plan"]["period_actions"]["2"][0]
        action.update(
            kind="research",
            amount=None,
            amount_meaning="unknown",
            amount_unit="",
            completion_period=1,
            details={"target_entity_id": "company"},
        )
        flattened = flatten_period_proposal(data, 2)
        self.assertEqual(flattened["plan"]["actions"][1]["completion_period"], 1)
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                if request.schema_name == "erp_strategy":
                    return LLMResponse(dumps(data), "controlled")
                return LLMResponse(
                    dumps(
                        {
                            "patches": [
                                {"action_id": "action-2", "replacement": {"completion_period": 2}}
                            ]
                        }
                    ),
                    "controlled",
                )

        plan = LLMStrategyGenerator(Client()).propose(snapshot()).baseline
        self.assertEqual(plan.actions[1].completion_period, 2)
        self.assertEqual(calls, ["erp_strategy", "erp_action_patch"])
        invalid = period_output(2)
        invalid["plan"]["period_actions"]["1"][0]["fact_ids"] = ["invented"]
        plan = decode_proposal(
            dumps(flatten_period_proposal(invalid, 2)), "controlled", allow_legacy=False
        ).baseline
        self.assertTrue(
            any(
                issue.code == "plan.reference" and issue.target == "actions.0.fact_ids"
                for issue in validate_actions(snapshot(), plan)
            )
        )

    def test_clarification_text_gets_one_bounded_rewrite_into_assumptions(self):
        data = period_output(2)
        data["plan"]["assumptions"] = ["请用户确认初始资源。"]
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                if len(calls) == 1:
                    return LLMResponse(dumps(data), "controlled")
                correction = period_output(2)
                correction["plan"]["assumptions"] = ["本规划仅采用拟新增资源，不计入未知初始存量。"]
                return LLMResponse(dumps(correction), "controlled")

        plan = LLMStrategyGenerator(Client()).propose(snapshot()).baseline
        self.assertNotIn("确认", str(plan.assumptions))
        self.assertEqual(len(calls), 2)

    def test_empty_specialist_analysis_does_not_block_autonomous_choice_for_any_tone(self):
        value = problem()
        outer = self
        for tone in PlanTone:
            calls = []
            updates = []

            class Reporting(Control):
                def report_task(self, key, metadata, target=updates):
                    target.append(metadata)

            class Client:
                configured = True

                def generate(self, request, log=calls, expected=tone):
                    data = loads(request.user)
                    role = data.get("role", "coordinator")
                    log.append(role)
                    outer.assertEqual(data["generation"]["tone"], expected.value)
                    if role == "coordinator":
                        outer.assertIn("research-period", request.user)
                        outer.assertIn("order-size", request.user)
                        outer.assertIn("自主", data["planning_policy"]["interaction"])
                        return LLMResponse(dumps(period_output(4)), "controlled")
                    limited = specialist("finance")
                    limited.update(status="missing_input", reason="该角色未能选择唯一产品路线。")
                    return LLMResponse(dumps(limited), "controlled")

            with self.subTest(tone=tone):
                config = GenerationConfig(
                    tone=tone, custom_prompt="按既有资金优先安排" if tone == PlanTone.CUSTOM else ""
                )
                result = CollaborativeStrategyGenerator(Client(), Reporting()).propose(
                    value, PlanningContext(config)
                )
                self.assertEqual({a.period for a in result.baseline.actions}, {1, 2, 3, 4})
                self.assertEqual(calls, ["operations", "finance", "coordinator"])
                self.assertFalse(any(t.get("status") == "needs_input" for t in updates))
                self.assertNotIn("未能选择唯一", str(result.baseline.risks))

    def test_schema_reuses_action_definition_and_does_not_embed_one_per_quarter(self):
        schema = period_proposal_schema(16)
        self.assertLess(len(dumps(schema)), 18000)
        self.assertEqual(schema["properties"]["parameters"]["maxItems"], 0)
        self.assertEqual(schema["properties"]["plan"]["properties"]["budget_items"]["maxItems"], 0)


if __name__ == "__main__":
    unittest.main()
