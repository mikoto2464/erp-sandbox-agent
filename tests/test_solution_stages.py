import tempfile
import threading
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from erp_agent.adapters.jobs import ThreadJobRunner
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.codec import solution_bundle
from erp_agent.application.contracts import obj, to_json
from erp_agent.application.solution_stages import plan_hash, public_solution, stage_record
from erp_agent.application.solver import SolverService
from erp_agent.application.workflow import WorkflowService
from erp_agent.domain.budget import BudgetResult
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import BudgetItem
from tests.helpers import FakeLLM
from tests.test_artifacts import report, template_fixture


class SolutionStagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = SQLiteRepository(Path(self.temp.name) / "test.sqlite3")
        self.data = report()
        self.pid = self.data.solution.project_id
        self.repository.put("project", self.pid, self.pid, {"id": self.pid, "name": "测试"})
        self.repository.put("solution", self.pid, self.data.solution.id, self.data.solution)

    def tearDown(self):
        self.temp.cleanup()

    def test_old_snapshot_is_readable_and_solution_cannot_be_replaced(self):
        saved = solution_bundle(self.repository.get("solution", self.pid, self.data.solution.id))
        self.assertEqual(saved, self.data.solution)
        self.assertEqual(
            public_solution(self.repository, saved)["stages"]["budget"]["status"], "not_applicable"
        )
        with self.assertRaises(ERPError) as error:
            self.repository.put(
                "solution",
                self.pid,
                saved.id,
                replace(saved, plan=replace(saved.plan, title="偷偷修改")),
            )
        self.assertEqual(error.exception.code, "solution_immutable")

    def test_partial_failure_keeps_action_boundary_and_old_attempt_cannot_overwrite(self):
        solution = self.data.solution
        stage_record(self.repository, self.pid, solution, "budget", "queued", attempt_id="first")
        budget = BudgetResult(
            (
                BudgetItem(
                    "t",
                    "表",
                    "B2",
                    "已完成",
                    1,
                    "元",
                    Decimal("10"),
                    (),
                    "",
                    source_ids=("fact:cash",),
                ),
            ),
            (),
            "partial",
            solution.id,
            1,
            (),
            plan_hash(solution.plan),
            (("t", "表"),),
        )
        stage_record(
            self.repository,
            self.pid,
            solution,
            "budget",
            "partial",
            result=budget,
            attempt_id="first",
        )
        stage_record(
            self.repository,
            self.pid,
            solution,
            "budget",
            "failed",
            error={"code": "timeout", "message": "上游失败"},
            attempt_id="first",
        )
        value = public_solution(self.repository, solution)
        self.assertEqual(value["stages"]["budget"]["status"], "failed")
        self.assertEqual(value["plan"]["budget_items"][0]["value"], "10")
        self.assertEqual(
            self.repository.get("solution", self.pid, solution.id)["plan"]["budget_items"], []
        )
        stage_record(self.repository, self.pid, solution, "budget", "queued", attempt_id="next")
        with self.assertRaises(ERPError) as error:
            stage_record(
                self.repository,
                self.pid,
                solution,
                "budget",
                "succeeded",
                result=budget,
                attempt_id="first",
            )
        self.assertEqual(error.exception.code, "stage_attempt_changed")

    def test_stage_rejects_wrong_hash_version_and_project(self):
        solution = self.data.solution
        stage_record(self.repository, self.pid, solution, "report", "queued", attempt_id="one")
        stored = self.repository.get("report_result", self.pid, solution.id)
        for changes in ({"plan_hash": "wrong"}, {"problem_version": 2}):
            with self.assertRaises(ERPError) as error:
                self.repository.put("report_result", self.pid, solution.id, {**stored, **changes})
            self.assertEqual(error.exception.code, "stage_binding_changed")
        with self.assertRaises(ERPError):
            stage_record(self.repository, "another", solution, "budget", "queued")
        new_solution = replace(solution, id="new", plan=replace(solution.plan, title="新方案"))
        self.repository.put("solution", self.pid, new_solution.id, new_solution)
        self.assertEqual(public_solution(self.repository, new_solution)["plan"]["budget_items"], [])

    def test_action_is_available_while_budget_runs_and_cancel_preserves_partial(self):
        _, template = template_fixture()
        original = self.data.solution
        solution = replace(
            original,
            id="staged",
            generation=replace(original.generation, template_ids=(template.source.id,)),
        )
        self.repository.put("template", self.pid, template.source.id, template)
        self.repository.put("solution", self.pid, solution.id, solution)
        self.repository.save_snapshot(
            self.pid, obj(to_json(replace(self.data.snapshot, document_ids=())))
        )
        jobs = ThreadJobRunner(self.repository)
        started, proceed = threading.Event(), threading.Event()
        client = FakeLLM("")
        workflow = WorkflowService(
            self.repository,
            jobs,
            None,
            type("Reader", (), {"llm": client})(),
            SolverService(None, None),
        )

        class Planner:
            def __init__(self, llm, control, memory):
                self.control = control

            def generate(self, snapshot, context, plan, *, solution_id, on_sheet):
                partial = BudgetResult(
                    (),
                    (),
                    "partial",
                    solution_id,
                    snapshot.version,
                    ((template.source.id, template.source.sha256),),
                    plan_hash(plan),
                    ((template.source.id, template.sheets[0].name),),
                )
                on_sheet(partial)
                started.set()
                proceed.wait(2)
                self.control.checkpoint("next_budget_sheet")
                return partial

        try:
            with patch("erp_agent.application.workflow.PlanBudgetPlanner", Planner):
                job = workflow.start_budget(self.pid, solution.id)
                self.assertTrue(started.wait(2))
                self.assertEqual(
                    public_solution(self.repository, solution)["stages"]["budget"]["status"],
                    "partial",
                )
                self.assertEqual(
                    self.repository.get("solution", self.pid, solution.id)["plan"]["title"],
                    solution.plan.title,
                )
                jobs.cancel(self.pid, job["id"])
                proceed.set()
                for _ in range(50):
                    current = self.repository.get("job", self.pid, job["id"])
                    if current["status"] == "cancelled":
                        break
                    time.sleep(0.01)
                self.assertEqual(current["status"], "cancelled")
                stage = self.repository.get("budget_result", self.pid, solution.id)
                self.assertEqual(stage["status"], "cancelled")
                self.assertEqual(len(stage["result"]["completed_sheets"]), 1)
        finally:
            proceed.set()
            jobs.close()

    def test_restart_marks_incomplete_stage_failed_and_preserves_success_payload(self):
        solution = self.data.solution
        stage_record(self.repository, self.pid, solution, "report", "queued", attempt_id="restart")
        stage_record(
            self.repository,
            self.pid,
            solution,
            "report",
            "running",
            result={"outline_complete": True},
            attempt_id="restart",
        )
        restarted = SQLiteRepository(self.repository.path)
        value = restarted.get("report_result", self.pid, solution.id)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["result"], {"outline_complete": True})
        self.assertEqual(value["error"]["code"], "server_restarted")
