import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_proposal, dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import DraftPlanEvaluator, LLMStrategyGenerator
from erp_agent.application.solver_plan_repairs import apply_plan_repair
from erp_agent.domain.errors import ERPError
from tests.helpers import snapshot, structured_action, structured_proposal_json


class SolverPeriodRepairTests(unittest.TestCase):
    def test_missing_last_two_periods_are_added_once_without_rewriting_existing_actions(self):
        problem = snapshot()
        problem = replace(
            problem,
            reading=replace(
                problem.reading,
                problem=replace(
                    problem.reading.problem,
                    years=4,
                    periods_per_year=4,
                ),
            ),
        )
        raw = loads(structured_proposal_json())
        existing = [structured_action(index=i, period=i, amount="10") for i in range(1, 15)]
        raw["plan"]["actions"] = existing
        calls = []
        outer = self

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                if request.schema_name == "erp_strategy":
                    return LLMResponse(dumps(raw), "same")
                data = loads(request.user)
                outer.assertEqual(data["period_coverage"]["missing_periods"], [15, 16])
                outer.assertEqual(data["objects"], [])
                outer.assertEqual(len(data["related_actions"]["actions"]), 14)
                return LLMResponse(
                    dumps(
                        {
                            "patches": [],
                            "additions": [
                                structured_action(
                                    index=15,
                                    period=15,
                                    amount="10",
                                    description="核对末期交付与待收款依赖",
                                ),
                                structured_action(
                                    index=16,
                                    period=16,
                                    amount="10",
                                    description="核对终局债务及费用结算",
                                ),
                            ],
                        }
                    ),
                    "same",
                )

        result = LLMStrategyGenerator(Client()).propose(problem)
        self.assertEqual(calls, ["erp_strategy", "erp_action_patch"])
        self.assertEqual([a.period for a in result.baseline.actions], list(range(1, 17)))
        before = decode_proposal(dumps(raw), "same", allow_legacy=False).baseline
        self.assertEqual(result.baseline.actions[:14], before.actions)
        self.assertEqual(result.baseline.summary, before.summary)
        self.assertFalse(DraftPlanEvaluator().evaluate(problem, result.baseline).verified)

    def test_time_repair_can_fill_a_period_vacated_by_its_replacements(self):
        raw = loads(structured_proposal_json())
        preceding = structured_action(index=0, period=2, amount="10")
        dependent = structured_action(index=1, period=1, amount="10")
        dependent["depends_on"] = ["action-0"]
        raw["plan"]["actions"] = [preceding, dependent]
        outer = self
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                if request.schema_name == "erp_strategy":
                    return LLMResponse(dumps(raw), "same")
                data = loads(request.user)
                outer.assertEqual(data["period_coverage"]["missing_periods"], [])
                outer.assertIn(1, data["period_coverage"]["potentially_vacated_periods"])
                return LLMResponse(
                    dumps(
                        {
                            "patches": [
                                {"action_id": "action-1", "replacement": {**dependent, "period": 2}}
                            ],
                            "additions": [
                                structured_action(
                                    index=2,
                                    period=1,
                                    amount="10",
                                    description="核对前置安排与初始资源条件",
                                ),
                            ],
                        }
                    ),
                    "same",
                )

        result = LLMStrategyGenerator(Client()).propose(snapshot()).baseline
        self.assertEqual(
            [a.action_id for a in result.actions], ["action-2", "action-0", "action-1"]
        )
        self.assertEqual(result.actions[1].period, 2)
        self.assertEqual(result.actions[2].depends_on, ("action-0",))
        self.assertEqual(len(calls), 2)

    def test_additions_reject_covered_period_duplicate_id_outside_and_extra_fields(self):
        raw = loads(structured_proposal_json())
        raw["plan"]["actions"] = [structured_action()]
        plan = decode_proposal(dumps(raw), "same", allow_legacy=False).baseline
        for addition in (
            structured_action(index=2, period=1),
            structured_action(index=0, period=2),
            structured_action(index=3, period=3),
        ):
            with self.subTest(addition=addition), self.assertRaises(ERPError):
                apply_plan_repair(
                    plan,
                    {"patches": [], "additions": [addition]},
                    set(),
                    {},
                    horizon=2,
                    allow_additions=True,
                )
        with self.assertRaises(ERPError):
            apply_plan_repair(
                plan,
                {"patches": [], "additions": [], "summary": "改写"},
                set(),
                {},
                horizon=2,
                allow_additions=True,
            )
        with self.assertRaises(ERPError):
            apply_plan_repair(
                plan,
                {"patches": [], "additions": [structured_action(index=1, period=2)]},
                set(),
                {},
                horizon=2,
                allow_additions=False,
            )

    def test_new_action_sources_and_incomplete_coverage_are_still_validated(self):
        raw = loads(structured_proposal_json())
        raw["plan"]["actions"] = [structured_action()]
        for additions in ([], [{**structured_action(index=1, period=2), "fact_ids": ["invented"]}]):
            calls = []

            class Client:
                def generate(self, request):
                    calls.append(request.schema_name)
                    value = (
                        raw
                        if request.schema_name == "erp_strategy"
                        else {"patches": [], "additions": additions}
                    )
                    return LLMResponse(dumps(value), "same")

            with self.subTest(additions=additions), self.assertRaises(ERPError):
                LLMStrategyGenerator(Client()).propose(snapshot())
            self.assertEqual(calls, ["erp_strategy", "erp_action_patch"])

    def test_coverage_diagnostics_name_missing_and_outside_periods(self):
        raw = loads(structured_proposal_json())
        raw["plan"]["actions"] = [structured_action(period=3)]
        plan = decode_proposal(dumps(raw), "same", allow_legacy=False).baseline
        evaluation = DraftPlanEvaluator().evaluate(snapshot(), plan)
        coverage = [i for i in evaluation.issues if i.code == "plan.period_coverage"]
        self.assertEqual(len(coverage), 1)
        self.assertIn("1（第1年第1期）", coverage[0].message)
        self.assertIn("2（第1年第2期）", coverage[0].message)
        self.assertIn("action-0=3", coverage[0].message)

    def test_additions_and_replacements_share_a_bounded_scope(self):
        plan = decode_proposal(structured_proposal_json(), "same", allow_legacy=False).baseline
        additions = [structured_action(index=i, period=i) for i in range(3, 28)]
        with self.assertRaises(ERPError) as caught:
            apply_plan_repair(
                plan,
                {"patches": [], "additions": additions},
                set(),
                {},
                horizon=30,
                allow_additions=True,
            )
        self.assertEqual(caught.exception.code, "solver_repair_limit")
        with self.assertRaises(ERPError) as combined:
            apply_plan_repair(
                plan,
                {
                    "patches": [
                        {"action_id": "action-0", "replacement": structured_action(amount="10")}
                    ],
                    "additions": additions[:24],
                },
                {"action-0"},
                {},
                horizon=30,
                allow_additions=True,
            )
        self.assertEqual(combined.exception.code, "solver_repair_limit")
