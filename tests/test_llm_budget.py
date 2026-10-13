import unittest
from dataclasses import replace

from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.ports import LLMRequest, LLMResponse, TaskContext
from erp_agent.application.solver_context import problem_material
from erp_agent.domain.errors import Cancelled, ModelOutputTruncated
from erp_agent.domain.models import Fact, KnowledgeState
from tests.helpers import Control, snapshot


class BudgetTests(unittest.TestCase):
    def test_reasoning_truncation_expands_once_and_respects_preset_limit(self):
        class Client:
            def __init__(self, maximum, successful=True):
                self.calls = []
                self.maximum, self.successful = maximum, successful

            def generate(self, request):
                self.calls.append(request)
                limit = request.max_output_tokens + request.reasoning_reserve_tokens
                if len(self.calls) == 2 and self.successful:
                    return LLMResponse("{}", "test", limit, 9000, 8000, 1.0)
                raise ModelOutputTruncated(
                    "length", min(limit, self.maximum), self.maximum, limit, limit
                )

        request = LLMRequest(
            "JSON",
            "{}",
            "solver",
            "{}",
            4000,
            task=TaskContext("solver:ops", "Solver", "产销", "题目"),
            reasoning_reserve_tokens=8192,
        )
        client = Client(20000)
        self.assertEqual(recovery_call(client, request, Control()).model, "test")
        self.assertEqual(client.calls[1].max_output_tokens, 20000)
        self.assertEqual(client.calls[1].reasoning_reserve_tokens, 0)
        for maximum, count in [(12192, 1), (30000, 2)]:
            client = Client(maximum, successful=False)
            with self.assertRaises(ModelOutputTruncated):
                recovery_call(client, request, Control())
            self.assertEqual(len(client.calls), count)

        class CancelledControl(Control):
            def checkpoint(self, *args):
                raise Cancelled()

        client = Client(30000)
        with self.assertRaises(Cancelled):
            recovery_call(client, request, CancelledControl())
        self.assertFalse(client.calls)

    def test_repeated_fact_tables_preserve_every_value_id_and_source(self):
        facts = tuple(
            Fact(
                f"fact-{row}-{column}",
                "order_pool",
                f"order-{row}",
                f"订单{row}",
                key,
                str(row * 10 + column),
                "number",
                "个",
                KnowledgeState.KNOWN,
                (f"source-{row}",),
                "percent_to_ratio" if row == 3 else "identity",
                "用户确认" if row == 2 else "",
            )
            for row in range(4)
            for column, key in enumerate(("year", "quarter", "price", "quantity"))
        )
        source = snapshot()
        source = replace(
            source,
            reading=replace(
                source.reading,
                problem=replace(source.reading.problem, facts=source.reading.problem.facts + facts),
            ),
        )
        material = problem_material(source)
        self.assertEqual(len(material["facts"]), 1)
        table = material["fact_tables"][0]
        restored = {}
        for row in table["rows"]:
            entity, name, values, ids, evidence, overrides = row
            for index, key in enumerate(table["columns"]):
                metadata = {**table["column_defaults"][index], **overrides.get(str(index), {})}
                restored[ids[index]] = (
                    entity,
                    name,
                    key,
                    values[index],
                    metadata,
                    tuple(material["table_evidence_index"][ref] for ref in evidence[index]),
                )
        for fact in facts:
            entity, name, key, value, metadata, evidence = restored[fact.id]
            self.assertEqual(
                (entity, name, key, value, evidence),
                (fact.entity_id, fact.entity_name, fact.key, fact.value, fact.evidence_ids),
            )
            self.assertEqual(metadata["conversion"], fact.conversion)
            self.assertEqual(metadata.get("explanation", ""), fact.explanation)
        reduced = problem_material(source, order_details=False)
        self.assertEqual(reduced["facts"], material["facts"])
        self.assertEqual(reduced["fact_tables"], [])
        self.assertEqual(reduced["deferred_order_pool"]["entities"], 4)
