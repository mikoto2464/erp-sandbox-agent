import unittest
from dataclasses import replace

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import CollaborativeStrategyGenerator, LLMStrategyGenerator
from tests.helpers import Control, structured_action, structured_proposal_json, snapshot
from tests.test_solver_dependency_flow import specialist


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
        proposal = loads(structured_proposal_json())
        proposal["plan"]["actions"] = [
            structured_action(index=period, period=period) for period in range(1, 7)
        ]
        proposal["parameters"] = []

        class Client:
            def generate(self, request):
                material = loads(request.user)
                facts = material.get("problem", material.get("snapshot", {}).get("problem"))
                calls.append(facts["calendar"])
                if request.schema_name == "erp_solver_specialist":
                    return LLMResponse(
                        dumps(specialist(material["role"])),
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
        proposal = loads(structured_proposal_json())
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
                payload = loads(request.user)
                self_outer.assertEqual(request.schema_name, "erp_action_patch")
                self_outer.assertEqual(
                    {o["action_id"] for o in payload["objects"]}, {"action-0", "action-1"}
                )
                return LLMResponse(
                    dumps(
                        {
                            "patches": [
                                {
                                    "action_id": "action-1",
                                    "replacement": structured_action(index=1, period=1),
                                },
                                {
                                    "action_id": "action-0",
                                    "replacement": structured_action(
                                        index=0, period=2, amount="10"
                                    ),
                                },
                            ]
                        }
                    ),
                    "coordinator",
                )

        self_outer = self
        result = LLMStrategyGenerator(Client()).propose(snapshot())
        self.assertEqual([a.period for a in result.baseline.actions], [1, 2])
        self.assertEqual(len(calls), 2)
        repair = loads(calls[1].user)
        self.assertEqual([a.action_id for a in result.baseline.actions], ["action-1", "action-0"])
        self.assertIn("allowed_fields", repair)


if __name__ == "__main__":
    unittest.main()
