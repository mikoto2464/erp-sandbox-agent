import unittest
from dataclasses import replace

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import CollaborativeStrategyGenerator, LLMStrategyGenerator
from tests.helpers import Control, proposal_json, snapshot


class SolverScheduleTests(unittest.TestCase):
    def test_nonstandard_periods_use_same_calendar_across_collaboration_and_synthesis(self):
        problem = snapshot()
        problem = replace(
            problem,
            reading=replace(
                problem.reading,
                problem=replace(problem.reading.problem, years=2, periods_per_year=3),
            ),
        )
        calls = []
        proposal = loads(proposal_json())
        prototype = proposal["plan"]["actions"][1]
        proposal["plan"]["actions"] = [{**prototype, "period": period} for period in range(1, 7)]
        proposal["parameters"] = []

        class Client:
            def generate(self, request):
                material = loads(request.user)
                facts = material.get("problem", material.get("snapshot", {}).get("problem"))
                calls.append(facts["calendar"])
                if request.schema_name == "erp_solver_specialist":
                    return LLMResponse(
                        dumps({"recommendations": [], "dependencies": [], "risks": []}),
                        "specialist",
                    )
                return LLMResponse(dumps(proposal), "coordinator")

        result = CollaborativeStrategyGenerator(Client(), Control()).propose(problem)
        self.assertEqual(len(calls), 3)
        self.assertTrue(
            all(
                calendar
                == {
                    "total_periods": 6,
                    "final_period": 6,
                    "year_ranges": [[1, 1, 3], [2, 4, 6]],
                }
                for calendar in calls
            )
        )
        self.assertEqual(result.baseline.actions[-1].period, 6)

    def test_chronology_error_is_repaired_without_silently_reordering_actions(self):
        proposal = loads(proposal_json())
        calls = []

        class Client:
            def generate(self, request):
                calls.append(request)
                if len(calls) == 1:
                    malformed = {**proposal, "parameters": []}
                    malformed["plan"] = {
                        **proposal["plan"],
                        "actions": list(reversed(proposal["plan"]["actions"])),
                    }
                    return LLMResponse(dumps(malformed), "coordinator")
                return LLMResponse(dumps(proposal), "coordinator")

        result = LLMStrategyGenerator(Client()).propose(snapshot())
        self.assertEqual([a.period for a in result.baseline.actions], [1, 2])
        self.assertEqual(len(calls), 2)
        repair = loads(calls[1].user)
        self.assertIn("执行次序", repair["error"])
        self.assertEqual(loads(repair["previous_output"])["plan"]["actions"][0]["period"], 2)


if __name__ == "__main__":
    unittest.main()
