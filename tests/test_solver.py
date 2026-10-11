import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_proposal
from erp_agent.application.solver import DraftPlanEvaluator, LLMStrategyGenerator, SolverService
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import SearchConfig, SolveRequest, SolverMode
from tests.helpers import Control, FakeLLM, QuadraticEvaluator, proposal_json, snapshot


class SolverTests(unittest.TestCase):
    def test_llm_only_one_generation_and_one_candidate(self):
        llm = FakeLLM(proposal_json())
        result = SolverService(LLMStrategyGenerator(llm), DraftPlanEvaluator()).solve(
            SolveRequest(snapshot(), SolverMode.LLM_ONLY), Control())
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result.evaluated_candidates, 1)
        self.assertFalse(result.evaluation.verified)
        self.assertIsNone(result.evaluation.score)

    def test_monte_carlo_seed_improves_baseline(self):
        solver = SolverService(LLMStrategyGenerator(FakeLLM(proposal_json())), QuadraticEvaluator())
        request = SolveRequest(snapshot(), SolverMode.LLM_MONTE_CARLO, SearchConfig(42, 100))
        result = solver.solve(request, Control())
        replay = solver.solve(request, Control())
        self.assertEqual(result.plan.actions[0].amount, 16)
        self.assertEqual(result.evaluation.score, -1)
        self.assertEqual(result.baseline_score, -25)
        self.assertEqual(result.candidate_plans, replay.candidate_plans)
        self.assertEqual(result.evaluated_candidates, 100)
        self.assertLess(result.feasible_candidates, 100)

    def test_monte_carlo_requires_scoring_before_llm_call(self):
        llm = FakeLLM(proposal_json())
        with self.assertRaises(ERPError) as caught:
            SolverService(LLMStrategyGenerator(llm), DraftPlanEvaluator()).solve(
                SolveRequest(snapshot(), SolverMode.LLM_MONTE_CARLO), Control())
        self.assertEqual(caught.exception.code, "simulation_not_ready")
        self.assertFalse(llm.calls)

    def test_periods_and_unknown_entities_rejected(self):
        plan = decode_proposal(proposal_json(), "fake").baseline
        broken = replace(plan, actions=(replace(plan.actions[0], entity_id="unknown"),))
        evaluation = DraftPlanEvaluator().evaluate(snapshot(), broken)
        self.assertFalse(evaluation.feasible)
        self.assertEqual({i.code for i in evaluation.issues if i.severity == "error"},
                         {"plan.period_coverage", "plan.entity"})

    def test_mode_switch_preserves_input_and_baseline(self):
        solver = SolverService(LLMStrategyGenerator(FakeLLM(proposal_json())), QuadraticEvaluator())
        problem = snapshot()
        plain = solver.solve(SolveRequest(problem, SolverMode.LLM_ONLY), Control())
        sampled = solver.solve(SolveRequest(problem, SolverMode.LLM_MONTE_CARLO,
                                            SearchConfig(1, 1)), Control())
        self.assertEqual(plain.plan, sampled.plan)
        self.assertEqual(plain.problem_version, sampled.problem_version)
        self.assertEqual(plain.mode, SolverMode.LLM_ONLY)
        self.assertEqual(sampled.mode, SolverMode.LLM_MONTE_CARLO)
