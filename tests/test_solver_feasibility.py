import unittest
from dataclasses import replace
from decimal import Decimal

from erp_agent.application.contracts import decode_proposal, dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import DraftPlanEvaluator, LLMStrategyGenerator
from erp_agent.domain.action_validation import (
    normalize_action,
    reference_catalogs,
    validate_cash_flow,
)
from erp_agent.domain.models import Action, Issue, KnowledgeState, Plan, Rule
from tests.helpers import snapshot, structured_action, structured_proposal_json


def payment_action(action_id, period, amount, *, meaning="payment"):
    return Action(
        period,
        "payment",
        "company",
        Decimal(amount),
        None,
        "测试现金支出",
        action_id=action_id,
        amount_unit="元",
        amount_meaning=meaning,
        schema_version="2",
    )


def cataloged_snapshot():
    value = snapshot()
    return replace(
        value,
        reading=replace(
            value.reading,
            rules=replace(
                value.reading.rules,
                rules=(Rule("rule-x", "规则", "e", KnowledgeState.KNOWN, ()),),
            ),
            issues=(Issue("gap.y", "warning", "t", "缺口", ()),),
        ),
    )


class SolverFeasibilityTests(unittest.TestCase):
    def test_review_does_not_cover_a_period(self):
        raw = loads(structured_proposal_json())
        raw["plan"]["actions"] = [
            structured_action(index=0, period=1, amount="10"),
            structured_action(index=1, period=2),
        ]
        plan = decode_proposal(dumps(raw), "same", allow_legacy=False).baseline
        evaluation = DraftPlanEvaluator().evaluate(snapshot(), plan)
        self.assertFalse(evaluation.feasible)
        coverage = next(
            i for i in evaluation.issues if i.code == "plan.period_coverage"
        )
        self.assertIn("2（第1年第2期）", coverage.message)

    def test_cash_flow_fold_flags_insufficient_cash(self):
        plan = Plan("方案", "草稿", (payment_action("pay-1", 1, "120000"),), (), ())
        issues = validate_cash_flow(snapshot(), plan)
        self.assertTrue(any(i.code == "plan.cash_flow" for i in issues))

    def test_cash_flow_fold_counts_financing_inflow(self):
        borrow = Action(
            1,
            "borrow",
            "company",
            Decimal("50000"),
            None,
            "借款",
            action_id="borrow-1",
            amount_unit="元",
            amount_meaning="financing_principal",
            schema_version="2",
        )
        plan = Plan("方案", "草稿", (borrow, payment_action("pay-1", 1, "120000")), (), ())
        self.assertEqual(validate_cash_flow(snapshot(), plan), ())

    def test_cash_flow_fold_skips_without_known_initial_cash(self):
        value = replace(
            snapshot(),
            reading=replace(
                snapshot().reading,
                problem=replace(snapshot().reading.problem, facts=()),
            ),
        )
        plan = Plan("方案", "草稿", (payment_action("pay-1", 1, "120000"),), (), ())
        self.assertEqual(validate_cash_flow(value, plan), ())

    def test_evaluator_marks_cash_shortfall_infeasible(self):
        raw = loads(structured_proposal_json())
        raw["plan"]["actions"][0]["amount"] = "120000"
        plan = decode_proposal(dumps(raw), "same", allow_legacy=False).baseline
        evaluation = DraftPlanEvaluator().evaluate(snapshot(), plan)
        self.assertFalse(evaluation.feasible)
        self.assertIn("plan.cash_flow", {i.code for i in evaluation.issues})

    def test_normalize_action_relocates_misplaced_references(self):
        facts, rules, gaps = reference_catalogs(cataloged_snapshot())
        action = Action(
            1,
            "payment",
            "company",
            Decimal("10"),
            None,
            "付款",
            action_id="a",
            amount_unit="元",
            amount_meaning="payment",
            fact_ids=("cash",),
            rule_ids=("cash", "rule-x"),
            gap_ids=("cash", "gap.y"),
            schema_version="2",
        )
        normalized = normalize_action(action, facts, rules, gaps)
        self.assertEqual(normalized.fact_ids, ("cash",))
        self.assertEqual(normalized.rule_ids, ("rule-x",))
        self.assertEqual(normalized.gap_ids, ("gap.y",))

    def test_normalize_action_preserves_unknown_reference_for_validation(self):
        facts, rules, gaps = reference_catalogs(cataloged_snapshot())
        action = payment_action("a", 1, "10")
        action = replace(action, fact_ids=("invented",))
        normalized = normalize_action(action, facts, rules, gaps)
        # 未知引用必须保留在原字段，交由校验继续报错，不能静默吞掉。
        self.assertEqual(normalized.fact_ids, ("invented",))

    def test_normalize_action_fills_missing_reason(self):
        facts, rules, gaps = reference_catalogs(cataloged_snapshot())
        action = replace(payment_action("a", 1, "10"), missing_reason="")
        normalized = normalize_action(action, facts, rules, gaps)
        self.assertTrue(normalized.missing_reason.strip())

    def test_propose_survives_misplaced_references_and_empty_reason(self):
        raw = loads(structured_proposal_json())
        raw["plan"]["actions"][0]["fact_ids"] = []
        raw["plan"]["actions"][0]["rule_ids"] = ["cash"]
        raw["plan"]["actions"][0]["gap_ids"] = ["cash"]
        raw["plan"]["actions"][0]["missing_reason"] = ""
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                return LLMResponse(dumps(raw), "controlled")

        plan = LLMStrategyGenerator(Client()).propose(snapshot()).baseline
        self.assertEqual(calls, ["erp_strategy"])
        self.assertEqual(plan.actions[0].fact_ids, ("cash",))
        self.assertEqual(plan.actions[0].rule_ids, ())
        self.assertEqual(plan.actions[0].gap_ids, ())
        self.assertTrue(plan.actions[0].missing_reason.strip())


if __name__ == "__main__":
    unittest.main()
