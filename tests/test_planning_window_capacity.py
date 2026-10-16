"""路线资料聚合超限时分批交接，未读及不可传输的来源仍严格校验。"""

import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from docx import Document
from langchain_core.messages import AIMessage, ToolMessage

from erp_agent.adapters.docx_reports import DocxReportRenderer
from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.codec import solution_bundle
from erp_agent.application.contracts import dumps, loads, to_json
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_handoff import provided_references
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.solver import DraftPlanEvaluator, SolverService
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import ReportData, SolveRequest, SolverMode
from tests.helpers import Control
from tests.test_planning_pipeline import PlannerClient, ToolChatModel, tool_reply
from tests.test_planning_windows import Memory, long_snapshot


def crowded_snapshot(count=30):
    snap = long_snapshot()
    base = snap.reading.problem.facts[0]
    opening = tuple(
        replace(
            base,
            id=f"initial-{i}",
            key=f"opening-{i}",
            value="背景" * 1700,
            value_type="text",
        )
        for i in range(6)
    )
    selected = tuple(
        replace(
            base,
            id=f"fact_{i:020}",
            category="material",
            entity_id=f"resource-{i}",
            key="price",
            value="12.5",
            explanation="来源说明" * 150,
            evidence_ids=(f"source-{i}",),
        )
        for i in range(count)
    )
    snap = replace(
        snap,
        reading=replace(
            snap.reading,
            problem=replace(snap.reading.problem, facts=(base,) + opening + selected),
        ),
    )
    return snap, tuple(f.id for f in selected)


class CapacityClient(PlannerClient):
    def __init__(self, references, native=False, read=True):
        super().__init__()
        self.references, self.native, self.read = references, native, read
        self.models, self.responses = [], []

    def generate(self, request):
        response = super().generate(request)
        payload, data = loads(request.user), loads(response.content)
        if request.schema_name == "erp_planning_route":
            data["windows"]["1"]["references"] = list(self.references)
        elif payload.get("assigned_periods", [None])[0] == 1:
            evidence = payload["reference_evidence"]["facts"]
            pending = payload["context"]["unloaded_reference_ids"]
            used = pending if pending else [evidence[0]["id"]]
            if "previous_decisions" in payload:
                data = {"periods": data["periods"]}
            data["periods"]["1"][0]["references"] = used
            if self.native:
                replies = []
                if self.read and "diagnostics" not in payload and pending:
                    replies.append(
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "planning_lookup",
                                    "args": {"kind": "facts", "ids": pending[i : i + 7]},
                                    "id": f"lookup-{i}",
                                }
                                for i in range(0, len(pending), 7)
                            ],
                            response_metadata={"finish_reason": "tool_calls"},
                        )
                    )
                replies.append(tool_reply(request.schema_name, data))
                model = ToolChatModel(replies=replies)
                self.models.append(model)
                result = LangChainLLMClient(
                    Settings(llm_model="test", llm_api_key="fake"), lambda _: model
                ).generate(request)
                self.responses.append(result)
                return result
        return replace(response, content=dumps(data))


class PlanningWindowCapacityTests(unittest.TestCase):
    def test_handoff_retains_full_records_and_only_marks_sent_sources_read(self):
        snap, selected = crowded_snapshot()
        knowledge = PlanningKnowledge(snap)
        handoff = knowledge.project_handoff("coordinator", selected + selected)
        context, evidence = handoff["context"], handoff["reference_evidence"]
        self.assertLessEqual(len(dumps(context)), 22000)
        self.assertLessEqual(len(dumps(evidence)), 12000)
        self.assertIn("cash", [f["id"] for f in context["facts"]])
        self.assertTrue(evidence["facts"])
        self.assertTrue(context["unloaded_reference_ids"])
        supplied = provided_references(handoff)
        self.assertEqual(set(context["unloaded_reference_ids"]), set(selected) - supplied)
        self.assertTrue(supplied.isdisjoint(context["unloaded_reference_ids"]))
        actual = context["facts"] + evidence["facts"]
        catalog = {f.id: to_json(f) for f in snap.reading.problem.facts}
        self.assertEqual(len(actual), len({f["id"] for f in actual}))
        for record in actual:
            self.assertEqual(record, catalog[record["id"]])

    def test_spill_packet_allows_action_without_query_or_repair(self):
        snap, selected = crowded_snapshot(count=3)
        client = CapacityClient(selected)
        plan = ParallelPlanningGenerator(client, Control()).propose(snap).baseline
        self.assertEqual(len(client.calls), 7)
        window = next(r for r in client.calls if r.task.id == "solver:synthesis:window:1")
        payload = loads(window.user)
        self.assertFalse(payload["context"]["unloaded_reference_ids"])
        self.assertTrue(payload["reference_evidence"]["facts"])
        self.assertIn(plan.actions[0].fact_ids[0], provided_references(payload))
        self.assertEqual({a.period for a in plan.actions}, set(range(1, 17)))

    def test_native_lookup_completes_full_horizon_and_cache_replays_sources(self):
        snap, selected = crowded_snapshot()
        client, memory = CapacityClient(selected, native=True), Memory()
        result = SolverService(
            ParallelPlanningGenerator(client, Control(), memory), DraftPlanEvaluator()
        ).solve(SolveRequest(snap, SolverMode.LLM_ONLY), Control())
        first = result.plan
        second = ParallelPlanningGenerator(client, Control(), memory).propose(snap).baseline
        self.assertEqual(first, second)
        self.assertEqual(len(client.calls), 7)
        self.assertEqual(sum(model.calls for model in client.models), 2)
        self.assertEqual(len(first.actions), 16)
        window = next(r for r in client.calls if r.task.id == "solver:synthesis:window:1")
        payload = loads(window.user)
        pending = set(payload["context"]["unloaded_reference_ids"])
        self.assertTrue(pending)
        self.assertTrue(pending.isdisjoint(window.tool_context["accessed_reference_ids"]))
        self.assertTrue(pending <= set(client.responses[0].accessed_reference_ids))
        self.assertTrue(pending <= set(first.actions[0].fact_ids))
        packets = [
            loads(m.content) for m in client.models[0].histories[-1] if isinstance(m, ToolMessage)
        ]
        self.assertEqual({f["id"] for p in packets for f in p["items"]}, pending)
        self.assertTrue(all(len(dumps(p["items"])) <= 12000 for p in packets))
        for packet in packets:
            self.assertEqual(packet["context_hash"], first.preparation.context_hash)
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteRepository(Path(directory) / "isolated.sqlite3")
            repository.put("solution", snap.project_id, result.id, result)
            restored = solution_bundle(repository.get("solution", snap.project_id, result.id))
            self.assertEqual(first, restored.plan)
            artifact = DocxReportRenderer().render(
                ReportData(snap, restored, outline=restored.plan.preparation.outline)
            )
            doc = Document(BytesIO(artifact.content))
            self.assertIn("第 4 年 · 第 4 季", "\n".join(p.text for p in doc.paragraphs))

    def test_unread_pending_sources_cannot_pass_by_catalog_presence(self):
        snap, selected = crowded_snapshot()
        client = CapacityClient(selected, native=True, read=False)
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control()).propose(snap)
        self.assertEqual(caught.exception.code, "planning.window_validation")
        self.assertIn("尚未读取", caught.exception.message)
        self.assertIn("periods.1", caught.exception.message)
        self.assertEqual(len(client.models), 2)  # 唯一修正仍不能将未发送的来源标为已读。

    def test_untransportable_record_keeps_explicit_capacity_diagnostic(self):
        snap, selected = crowded_snapshot(count=1)
        snap = replace(
            snap,
            reading=replace(
                snap.reading,
                problem=replace(
                    snap.reading.problem,
                    facts=tuple(
                        replace(f, explanation="超大来源" * 15000) if f.id == selected[0] else f
                        for f in snap.reading.problem.facts
                    ),
                ),
            ),
        )
        client = CapacityClient(selected)
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control()).propose(snap)
        self.assertEqual(caught.exception.code, "planning.context_limit")
        self.assertIn("windows.1.references", caught.exception.message)
        self.assertIn(selected[0], caught.exception.message)
        self.assertEqual(len(client.calls), 3)
