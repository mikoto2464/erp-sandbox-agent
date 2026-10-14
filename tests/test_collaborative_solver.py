import unittest

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse, ModelBinding
from erp_agent.application.solver_collaboration import CollaborativeStrategyGenerator
from erp_agent.domain.errors import ERPError
from tests.helpers import Control, snapshot, structured_proposal_json
from tests.test_solver_dependency_flow import specialist


class CollaborativeTests(unittest.TestCase):
    def test_unknown_references_fail_before_coordinator(self):
        bad = specialist("operations")
        bad["candidate_actions"][0]["fact_ids"] = ["invented"]

        class Client:
            configured = True

            def generate(self, request):
                assert request.schema_name != "erp_strategy"
                return LLMResponse(dumps(bad), "test")

        with self.assertRaises(ERPError) as caught:
            CollaborativeStrategyGenerator(Client(), Control()).propose(snapshot())
        self.assertIn(caught.exception.code, {"solver_action", "invalid_contract"})


    def test_regular_specialists_and_coordinator_skip_llm_model_dispatch(self):
        from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient

        calls = []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                calls.append((self.name, request))
                role = loads(request.user).get("role")
                if request.schema_name == "erp_solver_specialist":
                    return LLMResponse(dumps(specialist(role)), self.name)
                return LLMResponse(structured_proposal_json(), self.name)

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {"model": name, "priority": priority, "max_parallel": 1, "max_input_chars": 100000},
                Client(name),
            )
            for name, priority in [("main", 100), ("helper", 90)]
        )
        pool = ScheduledLLMClient(models, ModelCapacity())
        proposal = CollaborativeStrategyGenerator(pool, Control()).propose(snapshot())
        self.assertEqual(proposal.model, "main")
        self.assertEqual(
            [request.schema_name for _, request in calls],
            ["erp_solver_specialist", "erp_solver_specialist", "erp_strategy"],
        )
        self.assertFalse(any("dispatch" in request.schema_name for _, request in calls))
        self.assertTrue(all(value == 0 for value in pool.capacity.used.values()))
