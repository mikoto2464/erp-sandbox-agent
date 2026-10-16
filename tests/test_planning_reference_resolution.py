"""失效工资来源只交给系统核对；用户无需填写内部编号。"""

import unittest
from dataclasses import replace

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.human_resolution import HumanInputRequired, PlanningValidationError
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_decisions import compile_decisions
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.planning_prompts import planning_prompt
from tests.helpers import Control
from tests.test_planning_pipeline import PlannerClient, decisions
from tests.test_planning_windows import long_snapshot

BROKEN = "fact_7a1114af2fbc26e28af6d"
SALARY = "fact_7a1114af2fbc8e37c02a"


def wage_snapshot():
    snap = long_snapshot()
    salary = replace(
        snap.reading.problem.facts[0],
        id=SALARY,
        category="worker",
        entity_id="worker_b6f03e3ddfe26e28af6d",
        entity_name="高级技工",
        key="monthly_salary",
        value="9800",
        unit="元/月",
    )
    return replace(
        snap,
        reading=replace(
            snap.reading,
            problem=replace(snap.reading.problem, facts=snap.reading.problem.facts + (salary,)),
        ),
    )


class ReferenceClient(PlannerClient):
    def __init__(self, stubborn=False):
        super().__init__()
        self.stubborn = stubborn

    def generate(self, request):
        result = super().generate(request)
        payload, data = loads(request.user), loads(result.content)
        if payload.get("assigned_periods", [0])[0] == 5:
            reference = (
                SALARY
                if payload.get("human_guidance")
                or (payload.get("diagnostics") and not self.stubborn)
                else BROKEN
            )
            for period in ("6", "8"):
                data["periods"][period][0].update(
                    key=f"w2-p{period}-senior-wage-payment",
                    description="发放高级技工季度工资",
                    references=[reference],
                )
            if "allowed_periods" in payload:
                data["periods"] = {p: data["periods"][p] for p in payload["allowed_periods"]}
        return replace(result, content=dumps(data))


class QuestionControl(Control):
    answer = None

    def request_resolution(self, question):
        if self.answer:
            return self.answer
        raise HumanInputRequired(question)


class PlanningReferenceResolutionTests(unittest.TestCase):
    def test_error_preserves_business_locations_without_replacing_unknown_id(self):
        snap = wage_snapshot()
        knowledge = PlanningKnowledge(snap)
        data = decisions(knowledge.hash, 16)
        for period in ("6", "8"):
            data["periods"][period][0].update(
                references=[BROKEN], description="发放高级技工季度工资"
            )
        with self.assertRaises(PlanningValidationError) as caught:
            compile_decisions(data, snap, knowledge.hash, ())
        issues = caught.exception.issues
        self.assertEqual([i["period"] for i in issues], [6, 8])
        self.assertEqual(issues[0]["period_label"], "第 2 年 · 第 2 季")
        self.assertEqual(issues[1]["period_label"], "第 2 年 · 第 4 季")
        self.assertEqual(issues[0]["unknown_ids"], [BROKEN])
        self.assertEqual(data["periods"]["6"][0]["references"], [BROKEN])

    def test_single_automatic_repair_gets_actual_worker_record_by_business_object(self):
        snap, client = wage_snapshot(), ReferenceClient()
        plan = ParallelPlanningGenerator(client, Control()).propose(snap).baseline
        repair = next(r for r in client.calls if "validation_issues" in loads(r.user))
        payload = loads(repair.user)
        facts = payload["reference_evidence"]["facts"]
        self.assertTrue(any(f["id"] == SALARY and f["value"] == "9800" for f in facts))
        self.assertFalse(any(f["id"] == BROKEN for f in facts))
        self.assertEqual(len(client.calls), 8)
        for action in plan.actions:
            if action.period in (6, 8):
                self.assertIn(SALARY, action.fact_ids)

    def test_question_names_wage_actions_and_empty_answer_only_retries_failed_segment(self):
        snap, client, control = wage_snapshot(), ReferenceClient(stubborn=True), QuestionControl()
        planner = ParallelPlanningGenerator(client, control)
        with self.assertRaises(HumanInputRequired) as caught:
            planner.propose(snap)
        question = caught.exception.question
        self.assertEqual(question["task_id"], "solver:synthesis:window:2")
        self.assertNotIn(BROKEN, question["message"])
        self.assertIn("不需要查找或填写", question["help_text"])
        self.assertIn(BROKEN, question["technical_details"])
        self.assertEqual(len(question["details"]["affected_actions"]), 2)
        self.assertEqual(
            question["details"]["affected_actions"][0]["description"], "发放高级技工季度工资"
        )
        retry = next(o for o in question["options"] if o["id"] == "retry")
        self.assertIn("重新核对题目依据", retry["label"])
        self.assertEqual(len(client.calls), 8)
        control.answer = {"option": "retry", "text": ""}
        plan = planner.propose(snap).baseline
        self.assertEqual(len(client.calls), 9)
        self.assertEqual(client.calls[-1].task.id, question["task_id"])
        self.assertEqual({a.period for a in plan.actions}, set(range(1, 17)))
        self.assertNotIn(BROKEN, {f.id for f in snap.reading.problem.facts})

    def test_stage_guidance_is_bounded_and_never_injects_example_facts(self):
        snap = wage_snapshot()
        before = PlanningKnowledge(snap).data
        operations, finance = planning_prompt("operations"), planning_prompt("finance")
        self.assertIn("30%", operations)
        self.assertIn("90%", operations)
        self.assertIn("70%", operations)
        self.assertNotIn("70%", finance)
        self.assertIn("预算", finance)
        self.assertIn("不考核财务报表", finance)
        self.assertIn("40%", finance)
        self.assertIn("20%", finance)
        for stage in ("operations", "finance", "route", "window", "synthesis"):
            prompt = planning_prompt(stage)
            self.assertLess(len(prompt), 5000)
            self.assertNotIn("400000", prompt)
            self.assertNotIn("小羊", prompt)
            self.assertNotIn("/sample/", prompt)
            self.assertIn("不能伪装已确认事实", prompt)
        self.assertEqual(PlanningKnowledge(snap).data, before)
