import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_proposal, loads
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMResponse
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

    def test_fenced_json_is_unwrapped_without_relaxing_contract(self):
        content = '```json\n' + proposal_json() + '\n```'
        llm = FakeLLM(content)
        result = LLMStrategyGenerator(llm).propose(snapshot())
        self.assertEqual(result.raw_response, content)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(result.baseline.actions), 2)

    def test_strategy_repairs_once_and_excludes_historical_raw_output(self):
        problem = snapshot()
        problem = replace(problem, reading=replace(problem.reading, raw_response='PRIVATE_HISTORY'))
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request)
                return LLMResponse('invalid' if len(calls) == 1 else proposal_json(), 'same-model')

        repairs = []
        result = LLMStrategyGenerator(Client(), lambda: repairs.append(True),
                                      search_parameters=False).propose(problem)
        self.assertEqual(result.model, 'same-model')
        self.assertEqual(len(calls), 2)
        self.assertEqual(repairs, [True])
        for call in calls:
            self.assertNotIn('PRIVATE_HISTORY', call.user)
            self.assertNotIn('raw_response', loads(call.user)['snapshot'])
            self.assertIn('parameters 必须为空数组', loads(call.user)['mode_instruction'])
        self.assertEqual(loads(calls[1].user)['previous_output'], 'invalid')

    def test_sdk_structure_error_enters_same_bounded_repair(self):
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request)
                if len(calls) == 1:
                    raise ERPError('llm_structured_response', '字段类型不符合 Schema', 502)
                return LLMResponse(proposal_json(), 'same-model')

        result = LLMStrategyGenerator(Client()).propose(snapshot())
        self.assertEqual(result.model, 'same-model')
        self.assertEqual(len(calls), 2)
        self.assertIsNone(loads(calls[1].user)['previous_output'])

    def test_second_format_failure_is_actionable_and_transport_not_repaired(self):
        llm = FakeLLM('{"plan":')
        with self.assertRaises(ERPError) as caught:
            LLMStrategyGenerator(llm).propose(snapshot())
        self.assertEqual(caught.exception.code, 'strategy_output_invalid')
        self.assertEqual(len(llm.calls), 2)
        for code in ('llm_timeout','llm_incomplete','llm_http_error'):
            calls = []

            class Client:
                def generate(self, request):
                    calls.append(request)
                    raise ERPError(code, '接口错误', 502)

            with self.subTest(code=code), self.assertRaises(ERPError) as caught:
                LLMStrategyGenerator(Client()).propose(snapshot())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(calls), 1)

    def test_no_partial_or_surrounding_json_is_accepted(self):
        for content in ('```json\n' + proposal_json() + '\n```\n解释', proposal_json() + ' {}',
                        proposal_json()[:-2], '{"plan":{},"plan":{},"parameters":[]}'):
            with self.subTest(content=content[:30]), self.assertRaises(ERPError):
                decode_proposal(normalize_model_json(content), 'model')
