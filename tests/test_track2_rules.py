"""规程算例的工具计算及来源隔离，不以案例代替当前赛题。"""

import unittest
from dataclasses import replace
from decimal import Decimal

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.application.contracts import dumps, to_json
from erp_agent.application.planning_codec import decode_calculation
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_decisions import compile_decisions
from erp_agent.application.planning_tools import PlanningTools
from erp_agent.application.ports import LLMRequest
from erp_agent.config import Settings
from erp_agent.domain.calculations import ERPCalculator
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import KnowledgeState
from tests.helpers import snapshot
from tests.test_planning_pipeline import ToolChatModel, decisions, tool_reply


def operand(name, value, unit):
    return {"name": name, "value": value, "unit": unit, "reason": "仅用于规程算例回放"}


def research(identifier="synthesis-feature", target="4"):
    return {
        "id": identifier,
        "formula": "feature_research",
        "inputs": [
            operand("current_value", "1", "点"),
            operand("target_value", target, "点"),
            operand("unit_cost", "500", "元/点"),
        ],
        "unit": "元",
        "period": 1,
        "assumption": "特性研发情景，当前值与单位费须由当前赛题确认",
    }


class Track2CalculationTests(unittest.TestCase):
    def test_native_batches_replay_wages_and_wip_without_injecting_example_facts(self):
        knowledge = PlanningKnowledge(snapshot())
        before = dumps(knowledge.data)
        # 规程：74件*(2人*50元/件+1人*100元/件)=14800元。
        wages = {
            "id": "wages",
            "formula": "piece_wages",
            "inputs": [
                operand("quantity", "74", "件"),
                operand("manual_piece_rate", "50", "元/件"),
                operand("manual_count", "2", "人"),
                operand("senior_piece_rate", "100", "元/件"),
                operand("senior_count", "1", "人"),
            ],
            "unit": "元",
            "assumption": "规程计件工资算例，非本题实际产量或雇员",
        }
        # 为验证跨批依赖，取情景原料10000元、月薪合计6600元、2季=6月。
        wip = {
            "id": "wip",
            "formula": "wip_cost",
            "inputs": [
                operand("materials", "10000", "元"),
                operand("monthly_wages", "6600", "元/月"),
                operand("production_months", "6", "月"),
                {"name": "piece_wages", "calculation_id": "wages", "unit": "元"},
                operand("startup_cost", "0", "元"),
            ],
            "unit": "元",
            "assumption": "成本归集算例，已发生支出不再扣现金；本情景无额外开产费",
        }
        model = ToolChatModel(
            replies=[
                tool_reply("erp_calculate_batch", {"calculations": [wages, research()]}),
                tool_reply("erp_calculate_batch", {"calculations": [wip]}),
                tool_reply("result", {"summary": "计算完成"}),
            ]
        )
        result = LangChainLLMClient(
            Settings(llm_model="test", llm_api_key="fake"), lambda _: model
        ).generate(
            LLMRequest(
                "按批计算后提交",
                "只回放情景算例",
                "result",
                dumps({"type": "object", "properties": {"summary": {"type": "string"}}}),
                tool_context=knowledge.data,
            )
        )
        records = {r.id: to_json(r) for r in result.calculations}
        self.assertEqual(
            {k: Decimal(r["value"]) for k, r in records.items()},
            {"wages": Decimal(14800), "synthesis-feature": Decimal(1500), "wip": Decimal(64400)},
        )
        self.assertTrue(all(r["context_hash"] == knowledge.hash for r in records.values()))
        self.assertEqual(model.calls, 3)
        self.assertEqual(dumps(knowledge.data), before)

    def test_research_only_charges_increment_and_python_binds_current_source(self):
        snap = snapshot()
        fact = replace(
            snap.reading.problem.facts[0],
            id="feature-current",
            category="feature",
            key="current_value",
            value="2.5",
            unit="点",
        )
        snap = replace(
            snap,
            reading=replace(
                snap.reading,
                problem=replace(snap.reading.problem, facts=snap.reading.problem.facts + (fact,)),
            ),
        )
        knowledge = PlanningKnowledge(snap)
        tools = PlanningTools(knowledge.data)
        request = research(target="4")
        request["inputs"][0] = {"name": "current_value", "fact_id": "feature-current", "unit": "点"}
        result = tools.invoke("erp_calculate", request)
        self.assertEqual(Decimal(result["value"]), Decimal(750))
        data = decisions(knowledge.hash)
        data["periods"]["1"][0].update(
            amount=None, amount_calculation_id="synthesis-feature", references=["feature-current"]
        )
        plan = compile_decisions(data, snap, knowledge.hash, tuple(tools.records.values()))
        self.assertEqual(plan.actions[0].amount, Decimal(750))
        self.assertIn("feature-current", plan.actions[0].fact_ids)
        record = tools.records["synthesis-feature"]
        self.assertEqual(decode_calculation(to_json(record)), record)
        old = to_json(record)
        old["version"] = "erp-formulas-1"
        self.assertEqual(decode_calculation(old).version, "erp-formulas-1")

    def test_research_target_and_unit_errors_do_not_cancel_successful_batch_items(self):
        tools = PlanningTools(PlanningKnowledge(snapshot()).data)
        bad_unit = research("bad-unit")
        bad_unit["inputs"][2]["unit"] = "元"
        batch = tools.invoke(
            "erp_calculate_batch",
            {"calculations": [research("lower", "0"), bad_unit, research("unchanged", "1")]},
        )["results"]
        self.assertEqual(batch[0]["error"], "calculation.domain")
        self.assertEqual(batch[1]["error"], "calculation.unit")
        self.assertEqual(Decimal(batch[2]["value"]), Decimal(0))
        self.assertEqual(set(tools.records), {"unchanged"})
        repaired = tools.invoke("erp_calculate", research("lower", "2"))
        self.assertEqual(Decimal(repaired["value"]), Decimal(500))

    def test_research_cannot_use_conflicted_unknown_or_other_problem_facts(self):
        for state in (KnowledgeState.CONFLICTED, KnowledgeState.MISSING):
            with self.subTest(state=state):
                knowledge = PlanningKnowledge(snapshot())
                knowledge.data["facts"][0].update(state=state.value, unit="点")
                tools = PlanningTools(knowledge.data)
                for reference in ("cash", "another-problem-feature"):
                    request = research()
                    request["inputs"][0] = {
                        "name": "current_value",
                        "fact_id": reference,
                        "unit": "点",
                    }
                    with self.assertRaises(ERPError) as caught:
                        tools.invoke("erp_calculate", request)
                    self.assertEqual(caught.exception.code, "calculation.source")
                self.assertFalse(tools.records)
        with self.assertRaises(ERPError):
            ERPCalculator().calculate(
                "feature_research",
                {"current_value": Decimal(1), "target_value": Decimal(2), "unit_cost": Decimal(-1)},
            )
