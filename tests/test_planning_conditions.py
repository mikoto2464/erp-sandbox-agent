import unittest
from dataclasses import replace

from erp_agent.adapters.planning_tool_loop import _reference_schema
from erp_agent.application.contracts import decode_plan, dumps, loads, to_json
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_decisions import compile_decisions, decision_schema
from erp_agent.application.planning_tools import PlanningTools
from erp_agent.application.ports import LLMRequest
from erp_agent.domain.action_validation import validate_actions, validate_cash_flow
from erp_agent.domain.errors import ERPError
from tests.helpers import snapshot
from tests.test_planning_pipeline import decisions
from tests.test_planning_route_references import source_snapshot


class PlanningConditionTests(unittest.TestCase):
    def test_payment_labeled_cost_still_consumes_cash(self):
        snap = snapshot()
        key = PlanningKnowledge(snap).hash
        data = decisions(key)
        data["periods"]["1"][0].update(amount="100000000", amount_meaning="cost")
        plan = compile_decisions(data, snap, key, (), {"cash"})
        self.assertEqual(plan.actions[0].amount_meaning, "payment")
        self.assertTrue(any(i.code == "plan.cash_flow" for i in validate_cash_flow(snap, plan)))

    def test_omitted_nullable_condition_fields_keep_meaning_and_action_reference(self):
        snap = snapshot()
        key = PlanningKnowledge(snap).hash
        data = decisions(key)
        data["periods"]["1"][0]["condition"] = {
            "kind": "scenario",
            "explanation": "取得实际订单后执行。",
        }
        data["periods"]["2"][0]["condition"] = {
            "kind": "action",
            "reference_id": "expense-1",
            "explanation": "前项完成后执行。",
        }
        plan = compile_decisions(data, snap, key, (), {"cash"})
        self.assertEqual(plan.actions[0].condition.kind, "scenario")
        self.assertIsNone(plan.actions[0].condition.operator)
        self.assertEqual(plan.actions[1].condition.reference_id, plan.actions[0].action_id)
        self.assertFalse([i for i in validate_actions(snap, plan) if i.severity == "error"])
        data["periods"]["1"][0]["condition"]["operator"] = "eq"
        bad = compile_decisions(data, snap, key, (), {"cash"})
        self.assertTrue(any(i.code == "plan.condition" for i in validate_actions(snap, bad)))

    def test_unreferenced_gap_explanation_becomes_explicit_scenario_and_roundtrips(self):
        snap = snapshot()
        key = PlanningKnowledge(snap).hash
        data = decisions(key)
        data["periods"]["1"][0]["condition"] = {
            "kind": "gap",
            "reference_id": None,
            "operator": None,
            "value": None,
            "explanation": "仅在实际合同成立且资源到位时执行，尚未验证此情景。",
        }
        plan = compile_decisions(data, snap, key, (), {"cash"})
        self.assertEqual(plan.actions[0].condition.kind, "scenario")
        self.assertIsNone(plan.actions[0].condition.reference_id)
        self.assertIn("尚未验证此情景", plan.assumptions[0])
        self.assertFalse([i for i in validate_actions(snap, plan) if i.severity == "error"])
        self.assertEqual(decode_plan(to_json(plan)), plan)

    def test_explicit_unknown_gap_is_never_converted_or_dropped(self):
        snap = snapshot()
        key = PlanningKnowledge(snap).hash
        data = decisions(key)
        data["periods"]["1"][0]["condition"].update(
            kind="gap", reference_id="invented-gap", explanation="未知前提"
        )
        with self.assertRaises(ERPError) as caught:
            compile_decisions(data, snap, key, (), {"cash"})
        self.assertEqual(caught.exception.code, "planning.references")
        self.assertIn("invented-gap", caught.exception.message)
        self.assertIn("periods.1", caught.exception.message)

    def test_scenario_financing_cannot_fund_confirmed_expenses(self):
        snap = snapshot()
        snap = replace(
            snap,
            reading=replace(
                snap.reading,
                problem=replace(
                    snap.reading.problem, facts=(replace(snap.reading.problem.facts[0], value="0"),)
                ),
            ),
        )
        key = PlanningKnowledge(snap).hash
        data = decisions(key)
        action = data["periods"]["1"][0]
        action.update(kind="borrow", amount="20", amount_meaning="financing_principal")
        action["condition"].update(kind="scenario", explanation="贷款获批时放款，尚未批准")
        plan = compile_decisions(data, snap, key, (), {"cash"})
        self.assertTrue(any(i.code == "plan.cash_flow" for i in validate_cash_flow(snap, plan)))
        bad = replace(
            plan,
            actions=(
                replace(
                    plan.actions[0], condition=replace(plan.actions[0].condition, explanation="")
                ),
                *plan.actions[1:],
            ),
        )
        self.assertTrue(any(i.code == "plan.condition" for i in validate_actions(snap, bad)))

    def test_all_independent_action_reference_errors_have_period_and_key(self):
        snap = snapshot()
        key = PlanningKnowledge(snap).hash
        data = decisions(key)
        data["periods"]["1"][0]["references"] = ["invented-first"]
        data["periods"]["2"][0]["references"] = ["invented-second"]
        with self.assertRaises(ERPError) as caught:
            compile_decisions(data, snap, key, (), {"cash"})
        for value in (
            "periods.1",
            "periods.2",
            "expense-1",
            "expense-2",
            "invented-first",
            "invented-second",
        ):
            self.assertIn(value, caught.exception.message)

    def test_tool_submission_enum_includes_only_visible_or_requested_sources(self):
        knowledge = PlanningKnowledge(source_snapshot())
        tools = PlanningTools({**knowledge.data, "accessed_reference_ids": ["cash"]})
        request = LLMRequest(
            "职责",
            dumps({"context": {"unloaded_reference_ids": []}}),
            "result",
            dumps(decision_schema(16)),
            tool_context=knowledge.data,
        )
        first = _reference_schema(loads(request.schema_json), request, tools)
        self.assertEqual(first["$defs"]["planning_reference"]["enum"], ["cash"])
        tools.invoke("planning_lookup", {"kind": "facts", "ids": ["required-fee"]})
        second = _reference_schema(loads(request.schema_json), request, tools)
        self.assertEqual(second["$defs"]["planning_reference"]["enum"], ["cash", "required-fee"])
        self.assertNotIn("detail-0", dumps(second))
