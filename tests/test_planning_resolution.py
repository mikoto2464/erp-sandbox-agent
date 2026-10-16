"""现金单位、人工处理、继续任务及后台执行资源回归。"""

import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from erp_agent.adapters.jobs import ThreadJobRunner
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.ports import TaskContext
from erp_agent.application.task_budget import task_budget
from erp_agent.domain.action_validation import validate_cash_flow
from erp_agent.domain.errors import ERPError
from erp_agent.domain.money import money_value
from tests.helpers import Control
from tests.test_planning_pipeline import PlannerClient
from tests.test_planning_windows import long_snapshot


class UnitClient(PlannerClient):
    def __init__(self, unit="元/季", amount="10"):
        super().__init__()
        self.unit, self.amount = unit, amount

    def generate(self, request):
        result = super().generate(request)
        data, user = loads(result.content), loads(request.user)
        if request.schema_name == "erp_planning_route":
            for limit in data["windows"].values():
                limit.update(cash_outflow_limit="100", cash_unit="元")
        elif user.get("assigned_periods", [0])[0] == 5 and not user.get("human_guidance"):
            data["periods"]["5"][0].update(amount=self.amount, amount_unit=self.unit)
        return replace(result, content=dumps(data))


class ResolutionTests(unittest.TestCase):
    def test_isolated_http_question_answer_saves_full_plan_without_more_model_calls(self):
        from tests.test_web import WebTests

        case = WebTests("test_upload_read_revise_solve_and_isolation")
        planner = UnitClient()
        with patch("erp_agent.api.server.Handler.log_message"):
            case.setUp()
            try:
                project, reading = case.setup_problem()
                base = f"/api/projects/{project}"
                facts = reading["snapshot"]["reading"]["problem"]["facts"]
                for fact in facts:
                    if fact["key"] in {"years", "periods_per_year"}:
                        fact.update(value="4", explanation="四年四季的测试情景")
                revised = case.request(
                    base + "/revisions",
                    {
                        "expected_version": 1,
                        "reading": {"title": "人工恢复", "facts": facts, "rules": [], "issues": []},
                        "note": "16期测试",
                    },
                )

                def generate(request):
                    result = planner.generate(request)
                    cash = next(
                        f["id"] for f in request.tool_context["facts"] if f["key"] == "initial_cash"
                    )
                    return replace(result, content=result.content.replace('"cash"', dumps(cash)))

                with patch.object(case.llm, "generate", side_effect=generate):
                    job = case.request(base + "/solve", {"version": revised["version"]})
                    pending = case.wait_job(project, job["id"])
                    self.assertEqual(pending["status"], "needs_input", pending.get("error"))
                    self.assertEqual(case.request(base + "/solutions"), [])
                    question = pending["intervention"]
                    self.assertEqual(len(planner.calls), 8)
                    with self.assertRaises(HTTPError) as caught:
                        case.request(base + "/solve", {"version": revised["version"]})
                    self.assertEqual(caught.exception.code, 409)
                    answer = {
                        "question_id": question["id"],
                        "option": "use_unit",
                        "text": "列表中为季度总额，以元计量。",
                    }
                    case.request(f"/api/jobs/{job['id']}/answer", answer)
                    done = case.wait_job(project, job["id"])
                    self.assertEqual(done["status"], "succeeded", done.get("error"))
                    self.assertTrue(
                        all(
                            not t.get("error")
                            for t in done.get("tasks", [])
                            if t["status"] in {"succeeded", "cached"}
                        )
                    )
                    self.assertEqual(len(planner.calls), 8)
                    solutions = case.request(base + "/solutions")
                    self.assertEqual(
                        {a["period"] for a in solutions[0]["plan"]["actions"]}, set(range(1, 17))
                    )
                    self.assertIn("用户确认金额单位", str(solutions[0]["plan"]["assumptions"]))
                    with self.assertRaises(HTTPError) as caught:
                        case.request(f"/api/jobs/{job['id']}/answer", answer)
                    self.assertEqual(caught.exception.code, 409)
            finally:
                case.tearDown()

    def wait(self, repo, job_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            job = repo.get("job", "a" * 32, job_id)
            if job["status"] not in {"running", "queued"}:
                return job
            time.sleep(0.005)
        self.fail("任务没有结束当前执行阶段")

    def test_decimal_money_conversion_rejects_rates_and_different_currency(self):
        self.assertEqual(money_value(Decimal("0.001"), "万元", "元"), Decimal("10"))
        self.assertEqual(money_value(Decimal("10"), "人民币元", "RMB"), Decimal("10"))
        self.assertEqual(money_value(Decimal("10"), "元", "万元"), Decimal("0.001"))
        for source, target in (("元/月", "元"), ("万", "元"), ("USD", "元"), ("", "元")):
            with self.assertRaises(ERPError):
                money_value(Decimal(10), source, target)

    def test_window_compares_equivalent_units_without_repair(self):
        client = UnitClient("万元", "0.001")
        plan = ParallelPlanningGenerator(client, Control()).propose(long_snapshot()).baseline
        self.assertEqual(len(client.calls), 7)
        action = next(a for a in plan.actions if a.period == 5)
        self.assertEqual(action.amount, Decimal("0.001"))
        self.assertEqual(action.amount_unit, "万元")

    def test_converted_amount_cannot_hide_exceeded_limit(self):
        client = UnitClient("万元", "0.02")
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control()).propose(long_snapshot())
        self.assertIn("超过冻结路线限额", caught.exception.message)
        self.assertEqual(len(client.calls), 8)

    def test_cash_fold_includes_convertible_units_instead_of_skipping_payment(self):
        snap = long_snapshot()
        client = PlannerClient()
        plan = ParallelPlanningGenerator(client, Control()).propose(snap).baseline
        cash = replace(snap.reading.problem.facts[0], value="100", unit="元")
        snap = replace(
            snap,
            reading=replace(snap.reading, problem=replace(snap.reading.problem, facts=(cash,))),
        )
        plan = replace(
            plan, actions=(replace(plan.actions[0], amount=Decimal("0.02"), amount_unit="万元"),)
        )
        self.assertTrue(any(i.code == "plan.cash_flow" for i in validate_cash_flow(snap, plan)))

    def exercise_resume(self, option):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteRepository(Path(tmp) / "isolated.db")
            runner = ThreadJobRunner(repo, workers=1)
            client = UnitClient()
            planner = None

            def operation(control):
                nonlocal planner
                planner = planner or ParallelPlanningGenerator(client, control)
                plan = planner.propose(long_snapshot()).baseline
                return {"actions": len(plan.actions), "assumptions": list(plan.assumptions)}

            try:
                job = runner.submit("a" * 32, "solve", operation)
                pending = self.wait(repo, job["id"])
                self.assertEqual(pending["status"], "needs_input", pending.get("error"))
                self.assertIsNone(pending["result"])
                question = pending["intervention"]
                self.assertEqual(question["task_id"], "solver:synthesis:window:2")
                self.assertEqual(question["details"]["actions"][0]["unit"], "元/季")
                self.assertEqual(len(client.calls), 8)
                independent = runner.submit("a" * 32, "test", lambda _: {"ok": True})
                self.assertEqual(self.wait(repo, independent["id"])["status"], "succeeded")
                with self.assertRaises(ERPError):
                    runner.answer("a" * 32, job["id"], {"question_id": "stale", "option": option})
                with self.assertRaises(ERPError) as caught:
                    runner.answer(
                        "a" * 32, job["id"], {"question_id": question["id"], "option": []}
                    )
                self.assertEqual(caught.exception.code, "resolution_option")
                control = runner.paused[job["id"]][0]
                budget = task_budget(control, TaskContext("solver", "Solver", "", ""))
                before = budget.calls
                runner.answer(
                    "a" * 32,
                    job["id"],
                    {
                        "question_id": question["id"],
                        "option": option,
                        "text": "这些数值是季度总付款，以元表示，保留其它段。",
                    },
                )
                result = self.wait(repo, job["id"])
                self.assertEqual(result["status"], "succeeded", result.get("error"))
                self.assertEqual(result["result"]["actions"], 16)
                self.assertEqual(len(result["resolution_history"]), 1)
                if option == "use_unit":
                    self.assertEqual(len(client.calls), 8)
                    self.assertEqual(budget.calls, before)
                    self.assertIn("用户确认金额单位", str(result["result"]["assumptions"]))
                else:
                    self.assertEqual(len(client.calls), 9)
                    self.assertEqual(budget.calls, before + 1)
                    self.assertIn("human_guidance", loads(client.calls[-1].user))
                with self.assertRaises(ERPError):
                    runner.answer(
                        "a" * 32, job["id"], {"question_id": question["id"], "option": option}
                    )
            finally:
                runner.close()

    def test_confirm_unit_resumes_without_any_model_request(self):
        self.exercise_resume("use_unit")

    def test_custom_guidance_only_retries_failed_window_with_existing_budget(self):
        self.exercise_resume("retry")

    def test_pending_job_can_be_cancelled_and_restart_does_not_leave_orphan_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteRepository(Path(tmp) / "isolated.db")
            runner = ThreadJobRunner(repo)

            def operation(control):
                return ParallelPlanningGenerator(UnitClient(), control).propose(long_snapshot())

            try:
                job = runner.submit("a" * 32, "solve", operation)
                pending = self.wait(repo, job["id"])
                self.assertEqual(pending["status"], "needs_input")
                runner.cancel("a" * 32, job["id"])
                cancelled = repo.get("job", "a" * 32, job["id"])
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertNotIn("intervention", cancelled)
                another = runner.submit("a" * 32, "solve", operation)
                self.assertEqual(self.wait(repo, another["id"])["status"], "needs_input")
            finally:
                runner.close()
            reopened = SQLiteRepository(Path(tmp) / "isolated.db")
            job = reopened.get("job", "a" * 32, another["id"])
            self.assertEqual(job["error"]["code"], "server_restarted")
            self.assertNotIn("intervention", job)
