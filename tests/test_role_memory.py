import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from erp_agent.adapters.memory import SQLiteMemoryService
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.input_sources import text_source
from erp_agent.application.memory_client import MemoryLLMClient
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.application.reader import StructuredReader
from erp_agent.application.reading.roles import ExtractionRole, ReaderLimits
from erp_agent.application.solver import DraftPlanEvaluator, LLMStrategyGenerator, SolverService
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import SolveRequest, SolverMode, TemplateReading, TemplateSheetReading
from tests.helpers import Control, FakeLLM, snapshot


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "erp.sqlite3"
        self.repository = SQLiteRepository(self.path)
        self.memory = SQLiteMemoryService(self.path)

    def tearDown(self):
        self.memory.close()
        self.directory.cleanup()

    def test_memory_is_persistent_project_and_version_scoped_without_raw_responses(self):
        problem = snapshot()
        fact = replace(problem.reading.problem.facts[0], evidence_ids=("cash-source",))
        problem = replace(
            problem,
            reading=replace(
                problem.reading,
                problem=replace(problem.reading.problem, facts=(fact,)),
                raw_response="secret raw response",
            ),
        )
        self.memory.remember(problem)
        scope = self.memory.scope(problem.project_id, 1, "run")
        recalled = scope.recall("公司 initial_cash")
        self.assertIn("100000", dumps(recalled))
        self.assertNotIn("secret raw response", dumps(recalled))
        self.assertEqual(self.memory.scope("other", 1, "run").recall("initial_cash").entries, ())
        changed = replace(
            problem,
            version=2,
            reading=replace(
                problem.reading,
                problem=replace(problem.reading.problem, facts=(replace(fact, value="200000"),)),
            ),
        )
        self.memory.remember(changed)
        self.assertIn("100000", dumps(scope.recall("initial_cash")))
        self.assertIn(
            "200000", dumps(self.memory.scope(problem.project_id, 2, "run").recall("initial_cash"))
        )
        self.memory.close()
        self.memory = SQLiteMemoryService(self.path)
        self.assertIn(
            "100000", dumps(self.memory.scope(problem.project_id, 1, "run").recall("initial_cash"))
        )
        self.memory.forget(problem.project_id)
        self.assertEqual(
            self.memory.scope(problem.project_id, 1, "run").recall("initial_cash").entries, ()
        )

    def test_successful_batches_survive_failed_role_and_restart(self):
        document = text_source("赛题", "初始资金100000元", "manual_")

        class Client:
            configured = True
            fail = True
            calls = []

            def generate(self, request):
                self.calls.append(request)
                payload = loads(request.user)
                if payload["role"] == "rules" and self.fail:
                    raise ERPError("llm_gateway_timeout", "HTTP 504", 504)
                return LLMResponse(
                    dumps({"title": "题目", "facts": [], "rules": [], "issues": []}), "test"
                )

        client = Client()
        reader = StructuredReader(client, limits=ReaderLimits(12000, 4, 1), facts_role=ExtractionRole("facts"), rules_role=ExtractionRole("rules"))
        scope = self.memory.scope("project", 0, "same-input")
        with self.assertRaises(ERPError):
            reader.read((document,), Control(), memory=scope)
        self.assertEqual(sum(loads(r.user)["role"] == "facts" for r in client.calls), 1)
        self.memory.close()
        self.memory = SQLiteMemoryService(self.path)
        client.fail = False
        reader.read((document,), Control(), memory=self.memory.scope("project", 0, "same-input"))

        self.assertEqual(sum(loads(r.user)["role"] == "facts" for r in client.calls), 1)
        reader.read((document,), Control(), memory=self.memory.scope("project", 0, "changed-input"))
        self.assertEqual(sum(loads(r.user)["role"] == "facts" for r in client.calls), 2)

    def test_reader_solver_mapper_share_alignment_and_solution_with_version_isolation(self):
        problem = replace(snapshot(), template_readings=(TemplateReading('template', 'hash',
            (TemplateSheetReading('资金预算', '现金输入', '第1期'),), reviewed=True),))
        self.memory.remember(problem)
        alignment = self.memory.scope(problem.project_id, 1, 'reader').alignment()
        self.assertEqual(alignment, self.memory.scope(problem.project_id, 1, 'solver').alignment())
        self.assertEqual(alignment, self.memory.scope(problem.project_id, 1, 'mapper').alignment())
        self.assertEqual(alignment['templates'][0]['sha256'], 'hash')
        self.assertIn('资金预算', dumps(self.memory.scope(problem.project_id, 1, 'solver')
                                      .recall('资金预算')))
        from tests.helpers import proposal_json
        solution = SolverService(LLMStrategyGenerator(FakeLLM(proposal_json())),
                                 DraftPlanEvaluator()).solve(
            SolveRequest(problem, SolverMode.LLM_ONLY), Control())
        self.memory.remember_solution(solution)
        saved = self.memory.scope(problem.project_id, 1, 'mapper').get('solution:' + solution.id)
        self.assertEqual(saved['plan']['actions'][0]['amount'], '10')
        self.assertNotIn('raw_response', saved)
        self.assertIsNone(self.memory.scope(problem.project_id, 2, 'mapper').get('solution:' + solution.id))

    def test_deleted_upload_and_project_cannot_receive_late_memory(self):
        scope = self.memory.scope("project", 0, "run", ("doc",))
        with self.repository._connection() as connection:
            connection.execute("INSERT INTO deleted_uploads VALUES('project','document','doc')")
        with self.assertRaises(ERPError):
            scope.put("batch", {"reading": {}})
        with self.repository._connection() as connection:
            connection.execute("INSERT INTO deleted_projects VALUES('project')")
        with self.assertRaises(ERPError):
            scope.get("batch")

    def test_solver_and_mapper_receive_same_version_memory_and_task_requirements(self):
        problem = snapshot()
        self.memory.remember(problem)
        outputs = []
        for role in ("solver", "mapper"):
            scope = self.memory.scope(problem.project_id, 1, role)
            scope.put("requirements", {"preference": "少借款"})
            client = FakeLLM("{}")
            MemoryLLMClient(client, scope, "initial_cash").generate(
                LLMRequest("规则", "{}", "test", "{}")
            )
            outputs.append(loads(client.calls[0].user))
        self.assertEqual(outputs[0]["shared_memory"], outputs[1]["shared_memory"])
        self.assertEqual(outputs[0]["task_memory"], {"preference": "少借款"})


if __name__ == "__main__":
    unittest.main()
