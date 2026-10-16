import unittest
from copy import deepcopy
from dataclasses import replace

from erp_agent.application.contracts import SPECIALIST_SCHEMA, dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solver import LLMStrategyGenerator
from erp_agent.application.solver_collaboration import CollaborativeStrategyGenerator
from erp_agent.application.solver_context import role_material
from erp_agent.application.solver_references import reference_catalog, reference_schema
from erp_agent.application.solver_specialist import decode_specialist
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Issue
from tests.helpers import Control, snapshot
from tests.test_solver_autonomous_planning import period_output
from tests.test_solver_dependency_flow import specialist


def risk(identifier="risk-receipt", gap="new-receipt-gap"):
    return {
        "id": identifier,
        "periods": [1],
        "action_ids": ["action-0"],
        "gap_ids": [gap],
        "fact_ids": ["cash"],
        "rule_ids": [],
        "condition": {
            "kind": "always",
            "reference_id": None,
            "operator": None,
            "value": None,
            "explanation": "收款时点尚未定义，持续核查结算条件。",
        },
        "response": "不把销售收入当作即时现金；按真实回款条件安排支出。",
    }


class SolverReferenceFlowTests(unittest.TestCase):
    def test_reference_catalog_and_schema_share_all_gap_ids_including_information(self):
        value = snapshot()
        value = replace(
            value,
            reading=replace(
                value.reading,
                issues=(
                    Issue("rule.pending", "warning", "receipt", "收款时点未知。"),
                    Issue("reading.assumption", "info", "term", "保留暂定口径。"),
                ),
            ),
        )
        material = role_material(value, "operations")
        self.assertEqual(
            set(material["reference_catalog"]["gap_ids"]),
            {gap["gap_id"] for gap in material["gaps"]},
        )
        schema = reference_schema(SPECIALIST_SCHEMA, material["reference_catalog"])
        gap_enum = schema["$defs"]["solver_gap_ids"]["enum"]
        self.assertEqual(set(gap_enum), {"rule.pending:receipt", "reading.assumption:term"})
        data = specialist("operations")
        data["risks"] = [risk(gap="reading.assumption:term")]
        decode_specialist(data, value)
        # 每种来源只有一份枚举，所有对象引用它；不改变公共静态契约。
        self.assertEqual(dumps(schema).count('"enum": ["cash"]'), 1)
        self.assertNotIn("$defs", SPECIALIST_SCHEMA)

    def test_empty_gap_catalog_forbids_ids_and_gap_conditions_in_all_collections(self):
        schema = reference_schema(SPECIALIST_SCHEMA, reference_catalog(snapshot()))
        for collection in ("candidate_actions", "recommendations", "dependencies", "risks"):
            with self.subTest(collection=collection):
                gap = schema["properties"][collection]["items"]["properties"]["gap_ids"]
                self.assertEqual(gap["maxItems"], 0)
        kinds = {
            v["properties"]["kind"]["const"] for v in schema["$defs"]["solver_condition"]["anyOf"]
        }
        self.assertNotIn("gap", kinds)
        self.assertNotIn("rule", kinds)

    def test_local_gap_repair_is_sparse_and_risk_reaches_finance_and_coordinator(self):
        data = specialist("operations")
        data["risks"] = [risk()]
        calls = []
        outer = self

        class Client:
            configured = True

            def generate(self, request):
                calls.append(request.schema_name)
                material = loads(request.user)
                if request.schema_name == "erp_specialist_patch":
                    replacement = loads(request.schema_json)["properties"]["replacement"]
                    outer.assertEqual(set(replacement["properties"]), {"gap_ids", "response"})
                    outer.assertEqual(replacement["properties"]["gap_ids"]["maxItems"], 0)
                    outer.assertNotIn("$defs", loads(request.schema_json))
                    return LLMResponse(
                        dumps(
                            {
                                "collection": "risks",
                                "index": 0,
                                "replacement": {
                                    "gap_ids": [],
                                    "response": data["risks"][0]["response"],
                                },
                            }
                        ),
                        "same",
                    )
                if request.schema_name == "erp_strategy":
                    outer.assertEqual(material["collaboration"][0]["risks"][0]["gap_ids"], [])
                    outer.assertIn(
                        "收款", material["collaboration"][0]["risks"][0]["condition"]["explanation"]
                    )
                    output = period_output(2)
                    output["plan"]["risks"] = [data["risks"][0]["response"]]
                    return LLMResponse(dumps(output), "same")
                if material["role"] == "finance":
                    outer.assertEqual(material["operations_analysis"]["risks"][0]["gap_ids"], [])
                    return LLMResponse(dumps(specialist("finance")), "same")
                return LLMResponse(dumps(data), "same")

        result = CollaborativeStrategyGenerator(Client(), Control()).propose(snapshot())
        self.assertIn("不把销售收入", result.baseline.risks[0])
        self.assertEqual(
            calls,
            [
                "erp_solver_specialist",
                "erp_specialist_patch",
                "erp_solver_specialist",
                "erp_strategy",
            ],
        )
        self.assertEqual(data["risks"][0]["gap_ids"], ["new-receipt-gap"])

    def test_repeated_invalid_advice_fails_instead_of_being_isolated(self):
        value = snapshot()
        value = replace(
            value,
            reading=replace(
                value.reading, problem=replace(value.reading.problem, years=4, periods_per_year=4)
            ),
        )
        bad = specialist("operations")
        bad["risks"] = [risk(gap="gap_receipt_timing_unknown")]
        original = deepcopy(bad)
        calls = []

        class Client:
            configured = True

            def generate(self, request):
                calls.append(request.schema_name)
                material = loads(request.user)
                if request.schema_name == "erp_specialist_patch":
                    # 模拟供应商忽略约束，修复后仍返回同一未知引用。
                    return LLMResponse(
                        dumps(
                            {
                                "collection": "risks",
                                "index": 0,
                                "replacement": {
                                    "gap_ids": bad["risks"][0]["gap_ids"],
                                    "response": bad["risks"][0]["response"],
                                },
                            }
                        ),
                        "same",
                    )
                if material["role"] == "finance":
                    return LLMResponse(dumps(specialist("finance")), "same")
                return LLMResponse(dumps(bad), "same")

        with self.assertRaises(ERPError) as caught:
            CollaborativeStrategyGenerator(Client(), Control()).propose(value)
        # 不再静默隔离：引用失效的分析记录导致角色失败，而不是被删除后继续统筹。
        self.assertIn(caught.exception.code, {"solver_source", "solver_action"})
        self.assertEqual(calls, ["erp_solver_specialist", "erp_specialist_patch"])
        self.assertEqual(bad, original)
        self.assertEqual(value.reading.issues, ())

    def test_invalid_dependency_reference_is_never_isolated(self):
        data = specialist("operations")
        data["dependencies"] = [
            {
                "action_id": "action-0",
                "requires_action_id": "missing-upstream",
                "required_period": None,
                "fact_ids": ["cash"],
                "rule_ids": [],
                "gap_ids": [],
            }
        ]
        with self.assertRaises(ERPError) as caught:
            decode_specialist(data, snapshot())
        self.assertEqual(caught.exception.code, "solver_source")

    def test_final_action_gap_repair_preserves_unknown_and_uses_closed_catalog(self):
        data = period_output(2)
        action = data["plan"]["period_actions"]["2"][0]
        action.update(gap_ids=["new_time_gap"], missing_reason="回款规则未定义。")
        calls = []
        outer = self

        class Client:
            def generate(self, request):
                calls.append(request.schema_name)
                if request.schema_name == "erp_strategy":
                    return LLMResponse(dumps(data), "same")
                material = loads(request.user)
                outer.assertEqual(
                    set(material["allowed_fields"]["action-2"]), {"gap_ids", "missing_reason"}
                )
                return LLMResponse(
                    dumps(
                        {
                            "patches": [
                                {
                                    "action_id": "action-2",
                                    "replacement": {
                                        "gap_ids": [],
                                    "missing_reason": "回款规则未定义，"
                                    "付款不依赖拟定销售收入。",
                                    },
                                }
                            ]
                        }
                    ),
                    "same",
                )

        result = LLMStrategyGenerator(Client()).propose(snapshot()).baseline
        self.assertIn("回款规则未定义", result.actions[1].missing_reason)
        self.assertEqual(result.actions[1].gap_ids, ())
        self.assertEqual(result.actions[1].amount, 10)
        self.assertEqual(calls, ["erp_strategy", "erp_action_patch"])


if __name__ == "__main__":
    unittest.main()
