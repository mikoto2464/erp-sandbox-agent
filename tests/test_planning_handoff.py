"""跨角色资料必须实际发送；资料选择和行动依据分别校验。"""

import unittest
from dataclasses import replace
from decimal import Decimal

from erp_agent.application.contracts import dumps, loads, to_json
from erp_agent.application.planning_context import PlanningKnowledge, template_requirement_index
from erp_agent.application.planning_handoff import provided_references
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.planning_tools import PlanningTools
from erp_agent.domain.errors import ERPError
from tests.helpers import Control, snapshot
from tests.test_planning_batches import cost
from tests.test_planning_pipeline import PlannerClient, decision
from tests.test_planning_route_references import source_snapshot
from tests.test_planning_windows import Memory, long_snapshot


class PlanningHandoffTests(unittest.TestCase):
    def test_route_repair_can_select_new_sources_without_another_repair(self):
        """复现一次格式修正后新选 windows.2/3 资料，原实现此时再次 not_loaded。"""

        class Client(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                payload, data = loads(request.user), loads(response.content)
                if request.schema_name == "erp_planning_route":
                    if "diagnostics" not in payload:
                        data["title"] = ""
                    else:
                        for key in ("2", "3"):
                            data["windows"][key]["references"] = ["required-fee"]
                if payload.get("assigned_periods", [None])[0] in (5, 9):
                    fee = next(f for f in payload["context"]["facts"] if f["id"] == "required-fee")
                    check.assertEqual((fee["value"], fee["unit"]), ("12.5", "元"))
                    check.assertIn("required-fee", request.tool_context["accessed_reference_ids"])
                return replace(response, content=dumps(data))

        check, client = self, Client()
        plan = ParallelPlanningGenerator(client, Control()).propose(source_snapshot()).baseline
        self.assertEqual(len(client.calls), 8)
        self.assertEqual({a.period for a in plan.actions}, set(range(1, 17)))

    def test_upstream_read_flags_are_not_inherited_without_content(self):
        class Client(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                payload, data = loads(request.user), loads(response.content)
                if request.schema_name == "erp_planning_route":
                    tools = PlanningTools(request.tool_context)
                    tools.invoke("planning_lookup", {"kind": "facts", "ids": ["required-fee"]})
                    return replace(
                        response, accessed_reference_ids=tuple(tools.accessed_reference_ids)
                    )
                if payload.get("assigned_periods", [None])[0] == 1:
                    if "diagnostics" not in payload:
                        check.assertNotIn(
                            "required-fee", request.tool_context["accessed_reference_ids"]
                        )
                        check.assertNotIn("required-fee", provided_references(payload))
                    else:
                        check.assertIn("required-fee", provided_references(payload))
                        check.assertIn("periods.1", payload["diagnostics"])
                    data["periods"]["1"][0]["references"] = ["required-fee"]
                return replace(response, content=dumps(data))

        check, client, memory = self, Client(), Memory()
        first = ParallelPlanningGenerator(client, Control(), memory).propose(source_snapshot())
        second = ParallelPlanningGenerator(client, Control(), memory).propose(source_snapshot())
        self.assertEqual(first.baseline, second.baseline)
        self.assertEqual(len(client.calls), 8)
        self.assertIn("required-fee", first.baseline.actions[0].fact_ids)

    def test_shared_calculation_is_visible_in_route_and_window_messages(self):
        class Client(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                payload, data = loads(request.user), loads(response.content)
                if request.task.id == "solver:operations":
                    tools = PlanningTools(request.tool_context)
                    tools.invoke("erp_calculate", cost())
                    data["findings"] = [
                        {
                            "statement": "按情景计算投入。",
                            "references": [],
                            "implication": "共用此项投入。",
                            "calculation_ids": ["operations-cost"],
                        }
                    ]
                    return replace(
                        response, content=dumps(data), calculations=tuple(tools.records.values())
                    )
                if request.schema_name == "erp_planning_route" or "assigned_periods" in payload:
                    record = payload["calculation_results"][0]
                    check.assertEqual(
                        (record["id"], record["value"], record["unit"]),
                        ("operations-cost", "7.5", "元"),
                    )
                    check.assertTrue(record["assumption"])
                    check.assertEqual(record["inputs"], [["quantity", "3"], ["unit_price", "2.5"]])
                if request.schema_name == "erp_planning_route":
                    action = decision(1)
                    action.update(
                        key="shared-expense",
                        period=1,
                        amount=None,
                        amount_calculation_id="operations-cost",
                    )
                    data["shared_actions"] = [action]
                elif payload.get("assigned_periods", [None])[0] == 1:
                    data["periods"]["1"] = []
                return replace(response, content=dumps(data))

        check, client = self, Client()
        plan = ParallelPlanningGenerator(client, Control()).propose(long_snapshot()).baseline
        self.assertEqual(plan.actions[0].amount, Decimal("7.5"))
        self.assertEqual(len(client.calls), 7)

    def test_short_plan_repair_keeps_calculations_and_cache(self):
        class Client(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if request.schema_name != "erp_planning_decisions":
                    return response
                payload, data = loads(request.user), loads(response.content)
                tools = PlanningTools(request.tool_context)
                if "diagnostics" not in payload:
                    args = cost()
                    args["id"] = "synthesis-cost"
                    tools.invoke("erp_calculate", args)
                    data["periods"]["1"][0]["details"] = {"target_entity_id": "company"}
                else:
                    check.assertEqual(tools.records["synthesis-cost"].value, "7.5")
                    check.assertEqual(payload["calculation_results"][0]["value"], "7.5")
                data["periods"]["1"][0].update(amount=None, amount_calculation_id="synthesis-cost")
                return replace(
                    response, content=dumps(data), calculations=tuple(tools.records.values())
                )

        check, client, memory = self, Client(), Memory()
        first = ParallelPlanningGenerator(client, Control(), memory).propose(snapshot())
        second = ParallelPlanningGenerator(client, Control(), memory).propose(snapshot())
        self.assertEqual(first.baseline.actions[0].amount, Decimal("7.5"))
        self.assertEqual(first.baseline, second.baseline)
        self.assertEqual(len(client.calls), 4)

    def test_calculation_lookup_returns_values_and_actual_sources(self):
        knowledge = PlanningKnowledge(snapshot())
        tools = PlanningTools(knowledge.data)
        args = cost()
        args["inputs"][1].update(fact_id="cash", unit="元/件")
        args["inputs"][1].pop("value")
        # 创建独立的单价字段，避免用现金余额冒充单价。
        price = {
            **knowledge.data["facts"][0],
            "id": "price",
            "key": "unit_price",
            "unit": "元/件",
            "value": "2.5",
        }
        catalog = {**knowledge.data, "facts": knowledge.data["facts"] + [price]}
        args["inputs"][1]["fact_id"] = "price"
        tools = PlanningTools(catalog)
        tools.invoke("erp_calculate", args)
        downstream = PlanningTools(
            {**catalog, "available_calculation_records": to_json(tuple(tools.records.values()))}
        )
        self.assertFalse(downstream.records)
        result = downstream.invoke(
            "planning_lookup", {"kind": "calculations", "ids": ["operations-cost"]}
        )
        self.assertEqual(result["items"][0]["value"], "7.5")
        self.assertEqual(result["calculation_sources"]["facts"][0], price)
        self.assertEqual(downstream.accessed_reference_ids, {"price"})

    def test_template_index_deduplicates_years_but_preserves_units_and_lookup(self):
        requirements = [
            {
                "template_id": "template",
                "sheets": [
                    {
                        "name": f"年度{year}",
                        "periods": str(year),
                        "purpose": "预算",
                        "required_parameters": [
                            {
                                "label": "采购",
                                "unit": "元",
                                "description": "经确认的输入说明" * 20,
                                "depends_on_sheets": [],
                            },
                            {
                                "label": "采购",
                                "unit": "件",
                                "description": "件数",
                                "depends_on_sheets": [],
                            },
                        ],
                    }
                    for year in range(1, 5)
                ],
            }
        ]
        index = template_requirement_index(requirements)
        self.assertEqual(
            index["parameters"], [["采购", "元", [0, 1, 2, 3]], ["采购", "件", [0, 1, 2, 3]]]
        )
        self.assertLess(len(dumps(index)), len(dumps(requirements)) // 2)
        tools = PlanningTools(
            {**PlanningKnowledge(snapshot()).data, "template_requirements": requirements}
        )
        result = tools.invoke(
            "planning_lookup", {"kind": "template_requirements", "ids": ["template:年度2"]}
        )
        self.assertEqual(
            result["items"][0]["required_parameters"],
            requirements[0]["sheets"][1]["required_parameters"],
        )
        self.assertFalse(tools.accessed_reference_ids)

    def test_different_roles_receive_different_contracts_and_bounded_context(self):
        client = PlannerClient()
        ParallelPlanningGenerator(client, Control()).propose(long_snapshot())
        by_role = {r.task.id: r for r in client.calls}
        ops = by_role["solver:operations"].system
        finance = by_role["solver:finance"].system
        route = by_role["solver:synthesis:route"].system
        window = by_role["solver:synthesis:window:1"].system
        self.assertEqual(len({ops, finance, route, window}), 4)
        self.assertNotIn("行动契约：", ops)
        self.assertNotIn("行动契约：", finance)
        for request in client.calls:
            self.assertLessEqual(len(dumps(loads(request.user)["context"])), 22000)

    def test_large_reference_manifest_stays_inside_context_capacity(self):
        snap = snapshot()
        base = snap.reading.problem.facts[0]
        additional = tuple(
            replace(
                base,
                id=f"source-{i:04}",
                entity_id=f"resource-{i}",
                category="material",
                explanation="必要来源说明" * 35,
            )
            for i in range(160)
        )
        snap = replace(
            snap,
            reading=replace(
                snap.reading,
                problem=replace(
                    snap.reading.problem, facts=snap.reading.problem.facts + additional
                ),
            ),
        )
        context = PlanningKnowledge(snap).project(
            "coordinator", references=tuple(f.id for f in additional)
        )
        self.assertLessEqual(len(dumps(context)), 22000)
        self.assertIn("cash", [f["id"] for f in context["facts"]])
        sent = provided_references({"context": context})
        self.assertEqual(set(context["unloaded_reference_ids"]), {f.id for f in additional} - sent)
        self.assertTrue(context["unloaded_reference_ids"])

    def test_monthly_fee_batch_uses_exact_units_and_deduplicates_evidence(self):
        knowledge = PlanningKnowledge(snapshot())
        fee = {
            **knowledge.data["facts"][0],
            "id": "monthly-fee",
            "key": "management_fee",
            "value": "3000",
            "unit": "元/月",
        }
        tools = PlanningTools({**knowledge.data, "facts": knowledge.data["facts"] + [fee]})
        calculations = [
            {
                "id": f"finance-fee-{p}",
                "formula": "multiply",
                "unit": "元",
                "period": p,
                "assumption": "一季三个月，按月支付并汇总本季",
                "inputs": [
                    {"name": "quantity", "value": "3", "unit": "月", "reason": "一季三个月"},
                    {"name": "unit_price", "fact_id": "monthly-fee", "unit": "元/月"},
                ],
            }
            for p in (1, 2)
        ]
        batch = tools.invoke("erp_calculate_batch", {"calculations": calculations})
        self.assertEqual([r["value"] for r in batch["results"]], ["9000", "9000"])
        self.assertEqual(batch["calculation_sources"]["facts"], [fee])
        self.assertTrue(all("calculation_sources" not in r for r in batch["results"]))
        invalid = {
            **calculations[0],
            "id": "wrong-unit",
            "inputs": [
                {**calculations[0]["inputs"][0], "value": "1", "unit": "季"},
                calculations[0]["inputs"][1],
            ],
        }
        with self.assertRaises(ERPError) as caught:
            tools.invoke("erp_calculate", invalid)
        self.assertEqual(caught.exception.code, "calculation.unit")

    def test_validated_analysis_is_reused_after_downstream_failure(self):
        class Client(PlannerClient):
            fail_route = True

            def generate(self, request):
                response = super().generate(request)
                if self.fail_route and request.schema_name == "erp_planning_route":
                    data = loads(response.content)
                    data["title"] = ""
                    return replace(response, content=dumps(data))
                return response

        client, memory = Client(), Memory()
        with self.assertRaises(ERPError):
            ParallelPlanningGenerator(client, Control(), memory).propose(long_snapshot())
        self.assertEqual(len(memory.records), 2)
        self.assertTrue(all("findings" in loads(v["content"]) for v in memory.records.values()))
        client.fail_route = False
        result = ParallelPlanningGenerator(client, Control(), memory).propose(long_snapshot())
        self.assertEqual(len(client.calls), 9)
        self.assertEqual(len(result.baseline.actions), 16)

    def test_catalog_rejects_ambiguous_duplicate_fact_id(self):
        snap = snapshot()
        facts = snap.reading.problem.facts
        snap = replace(
            snap,
            reading=replace(
                snap.reading,
                problem=replace(
                    snap.reading.problem, facts=facts + (replace(facts[0], value="different"),)
                ),
            ),
        )
        with self.assertRaises(ERPError) as caught:
            PlanningKnowledge(snap)
        self.assertEqual(caught.exception.code, "planning.catalog")
