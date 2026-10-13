import threading
import unittest

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import CollaborativeStrategyGenerator
from erp_agent.domain.errors import ERPError
from tests.helpers import Control, proposal_json, snapshot


class CollaborativeTests(unittest.TestCase):
    def test_specialists_work_concurrently_and_coordinator_receives_their_evidence(self):
        barrier = threading.Barrier(2)
        calls = []

        class Client:
            configured = True

            def generate(self, request):
                calls.append(request)
                if request.schema_name == "erp_solver_specialist":
                    barrier.wait(2)
                    role = loads(request.user)["role"]
                    return LLMResponse(
                        dumps(
                            {
                                "recommendations": [
                                    {
                                        "entity_id": "company",
                                        "periods": "1–2",
                                        "recommendation": role,
                                        "fact_ids": ["cash"],
                                        "rule_ids": [],
                                    }
                                ],
                                "dependencies": ["现金先于采购"],
                                "risks": [],
                            }
                        ),
                        role,
                    )
                material = loads(request.user)
                assert len(material["collaboration"]) == 2
                assert all(
                    item["recommendations"][0]["fact_ids"] == ["cash"]
                    for item in material["collaboration"]
                )
                return LLMResponse(proposal_json(), "coordinator")

        proposal = CollaborativeStrategyGenerator(Client(), Control()).propose(snapshot())
        self.assertEqual(len(calls), 3)
        self.assertEqual(proposal.model, "coordinator")
        self.assertEqual(proposal.parameters, ())

    def test_unknown_references_fail_before_coordinator(self):
        class Client:
            configured = True

            def generate(self, request):
                assert request.schema_name == "erp_solver_specialist"
                return LLMResponse(
                    dumps(
                        {
                            "recommendations": [
                                {
                                    "entity_id": "company",
                                    "periods": "1",
                                    "recommendation": "采购",
                                    "fact_ids": ["invented"],
                                    "rule_ids": [],
                                }
                            ],
                            "dependencies": [],
                            "risks": [],
                        }
                    ),
                    "test",
                )

        with self.assertRaises(ERPError) as caught:
            CollaborativeStrategyGenerator(Client(), Control()).propose(snapshot())
        self.assertEqual(caught.exception.code, "solver_source")
