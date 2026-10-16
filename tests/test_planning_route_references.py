"""路线引用必须真正可读：显式来源优先、按需补取和缓存证据继承。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.application.contracts import dumps, loads, to_json
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_pipeline import ParallelPlanningGenerator
from erp_agent.application.planning_tools import PlanningTools
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import KnowledgeState, Rule
from tests.helpers import Control
from tests.test_planning_pipeline import PlannerClient, ToolChatModel, decision, tool_reply
from tests.test_planning_windows import Memory, long_snapshot


def source_snapshot():
    snap = long_snapshot()
    base = snap.reading.problem.facts[0]
    fee = replace(
        base,
        id="required-fee",
        category="other",
        entity_id="fee-schedule",
        key="fixed_fee",
        value="12.5",
        evidence_ids=("fee-evidence",),
    )
    bulky = tuple(
        replace(
            fee,
            id=f"detail-{n}",
            key=f"unrelated_detail_{n}",
            value="资料" * 1000,
            value_type="text",
        )
        for n in range(18)
    )
    return replace(
        snap,
        reading=replace(
            snap.reading,
            problem=replace(
                snap.reading.problem, facts=snap.reading.problem.facts + (fee,) + bulky
            ),
        ),
    )


class RouteClient(PlannerClient):
    def generate(self, request):
        response = super().generate(request)
        if request.schema_name == "erp_planning_route":
            data = loads(response.content)
            for limit in data["windows"].values():
                limit["references"] = ["required-fee"]
            return replace(response, content=dumps(data))
        return response


class PlanningRouteReferenceTests(unittest.TestCase):
    def test_isolated_http_route_delivers_sources_without_native_resubmission(self):
        from tests.test_web import WebTests

        case = WebTests("test_upload_read_revise_solve_and_isolation")
        client, models, evidence_packets = RouteClient(), [], []

        def native_generate(request):
            payload = loads(request.user)
            if "assigned_periods" in payload:
                evidence_packets.append(payload["context"])
            data = loads(client.generate(request).content)
            cash = next(
                f["id"] for f in request.tool_context["facts"] if f["key"] == "initial_cash"
            )
            data = loads(dumps(data).replace('"cash"', dumps(cash)))
            model = ToolChatModel(replies=[tool_reply(request.schema_name, data)])
            models.append(model)
            return LangChainLLMClient(
                Settings(llm_model="test", llm_api_key="fake"), lambda _: model
            ).generate(request)

        with patch("erp_agent.api.server.Handler.log_message"):
            case.setUp()
            try:
                project, reading = case.setup_problem()
                base = f"/api/projects/{project}"
                facts = reading["snapshot"]["reading"]["problem"]["facts"]
                for fact in facts:
                    if fact["key"] in {"years", "periods_per_year"}:
                        fact.update(value="4", explanation="受控四年四期情景")
                facts.append(
                    {
                        **facts[0],
                        "id": "required-fee",
                        "category": "other",
                        "entity_id": "fee-schedule",
                        "entity_name": "费用情景",
                        "key": "fixed_fee",
                        "value": "12.5",
                        "state": "assumed",
                        "explanation": "受控费用情景，尚非题目确认事实",
                    }
                )
                revised = case.request(
                    base + "/revisions",
                    {
                        "expected_version": 1,
                        "reading": {
                            "title": "路线来源补取",
                            "facts": facts,
                            "rules": [],
                            "issues": [],
                        },
                        "note": "受控情景",
                    },
                )
                with patch.object(case.llm, "generate", side_effect=native_generate):
                    job = case.request(base + "/solve", {"version": revised["version"]})
                    result = case.wait_job(project, job["id"])
                self.assertEqual(result["status"], "succeeded", result.get("error"))
                self.assertEqual(len(evidence_packets), 4)
                self.assertEqual(
                    next(f for f in evidence_packets[0]["facts"] if f["id"] == "required-fee")[
                        "state"
                    ],
                    "assumed",
                )
                self.assertEqual(
                    next(f for f in evidence_packets[0]["facts"] if f["id"] == "required-fee")[
                        "value"
                    ],
                    "12.5",
                )
                self.assertEqual(sum(m.calls for m in models), 7)
                plan = case.request(base + "/solutions")[0]["plan"]
                self.assertEqual({a["period"] for a in plan["actions"]}, set(range(1, 17)))
            finally:
                case.tearDown()

    def test_explicit_field_in_oversized_entity_is_read_without_its_entire_bundle(self):
        knowledge = PlanningKnowledge(source_snapshot())
        material = knowledge.project("coordinator", references=("required-fee",))
        ids = [f["id"] for f in material["facts"]]
        self.assertIn("required-fee", ids)
        self.assertIn("cash", ids)
        self.assertNotIn("detail-0", ids)
        self.assertEqual(ids.count("required-fee"), 1)
        self.assertLessEqual(len(dumps(material)), 22000)
        expected = next(f for f in knowledge.data["facts"] if f["id"] == "required-fee")
        self.assertEqual(next(f for f in material["facts"] if f["id"] == "required-fee"), expected)

    def test_explicit_rule_has_priority_over_unrelated_fact_bundles(self):
        snap = source_snapshot()
        rule = Rule(
            "fee-rule",
            "固定费口径",
            "按题目所列期间支付。",
            KnowledgeState.KNOWN,
            ("fee-evidence",),
            "",
        )
        snap = replace(
            snap, reading=replace(snap.reading, rules=replace(snap.reading.rules, rules=(rule,)))
        )
        material = PlanningKnowledge(snap).project("coordinator", references=("fee-rule",))
        self.assertEqual(material["rules"], [to_json(rule)])
        self.assertLessEqual(len(dumps(material)), 22000)

    def test_validated_analysis_reference_reaches_route_without_extra_repair(self):
        class Analyzed(RouteClient):
            def generate(self, request):
                response = super().generate(request)
                if request.task.id == "solver:operations":
                    tools = PlanningTools(request.tool_context)
                    tools.invoke("planning_lookup", {"kind": "facts", "ids": ["required-fee"]})
                    data = loads(response.content)
                    data["findings"] = [
                        {
                            "statement": "按已有费用口径规划。",
                            "implication": "保留费用支出。",
                            "references": ["required-fee"],
                        }
                    ]
                    return replace(
                        response,
                        content=dumps(data),
                        accessed_reference_ids=tuple(tools.accessed_reference_ids),
                    )
                if request.schema_name == "erp_planning_route":
                    self_test.assertIn(
                        "required-fee", [f["id"] for f in loads(request.user)["context"]["facts"]]
                    )
                return response

        self_test = self
        client = Analyzed()
        plan = ParallelPlanningGenerator(client, Control()).propose(source_snapshot()).baseline
        self.assertEqual(len(plan.actions), 16)
        self.assertEqual(len(client.calls), 7)

    def test_route_repair_gets_exact_missing_records_in_existing_single_repair(self):
        class Hydrated(RouteClient):
            def generate(self, request):
                if request.schema_name == "erp_planning_route":
                    payload = loads(request.user)
                    if "diagnostics" in payload:
                        evidence = payload["reference_evidence"]
                        self_test.assertEqual(
                            evidence["context_hash"], request.tool_context["context_hash"]
                        )
                        self_test.assertEqual(
                            [f["id"] for f in evidence["facts"]], ["required-fee"]
                        )
                        self_test.assertEqual(evidence["facts"][0]["value"], "12.5")
                        self_test.assertEqual(
                            evidence["facts"][0]["evidence_ids"], ["fee-evidence"]
                        )
                        self_test.assertIn("required-fee", payload["diagnostics"])
                        self_test.assertIn("shared_actions.references", payload["diagnostics"])
                        self_test.assertLessEqual(len(dumps(evidence)), 12000)
                response = super().generate(request)
                data = loads(response.content)
                if request.schema_name == "erp_planning_route":
                    action = decision(1)
                    action.update(key="shared-fee", period=1, references=["required-fee"])
                    data["shared_actions"] = [action]
                elif loads(request.user).get("assigned_periods", [None])[0] == 1:
                    data["periods"]["1"] = []
                return replace(response, content=dumps(data))

        self_test = self
        client = Hydrated()
        result = ParallelPlanningGenerator(client, Control()).propose(source_snapshot())
        self.assertEqual(len(client.calls), 8)
        self.assertEqual(len(result.baseline.actions), 16)

    def test_success_cache_keeps_repair_evidence_access_without_new_requests(self):
        memory, client, control = Memory(), RouteClient(), Control()
        first = ParallelPlanningGenerator(client, control, memory).propose(source_snapshot())
        second = ParallelPlanningGenerator(client, control, memory).propose(source_snapshot())
        self.assertEqual(first.baseline, second.baseline)
        self.assertEqual(len(client.calls), 7)

    def test_cache_keeps_sources_loaded_by_tool_before_a_format_repair(self):
        snap = source_snapshot()
        rule = Rule("fee-rule", "费用口径", "按实际题目支付。", KnowledgeState.KNOWN, (), "")
        snap = replace(
            snap, reading=replace(snap.reading, rules=replace(snap.reading.rules, rules=(rule,)))
        )

        class ReadThenRepair(PlannerClient):
            def generate(self, request):
                response = super().generate(request)
                if request.schema_name != "erp_planning_route":
                    return response
                data = loads(response.content)
                for limit in data["windows"].values():
                    limit["references"] = ["fee-rule"]
                if "diagnostics" not in loads(request.user):
                    tools = PlanningTools(request.tool_context)
                    tools.invoke("planning_lookup", {"kind": "rules", "ids": ["fee-rule"]})
                    data["title"] = ""
                    return replace(
                        response,
                        content=dumps(data),
                        accessed_reference_ids=tuple(tools.accessed_reference_ids),
                    )
                return replace(response, content=dumps(data))

        client, memory, control = ReadThenRepair(), Memory(), Control()
        first = ParallelPlanningGenerator(client, control, memory).propose(snap)
        second = ParallelPlanningGenerator(client, control, memory).propose(snap)
        self.assertEqual(first.baseline, second.baseline)
        self.assertEqual(len(client.calls), 8)

    def test_unknown_reference_is_not_hydrated_or_treated_as_missing_fact(self):
        class Unknown(RouteClient):
            def generate(self, request):
                response = super().generate(request)
                if request.schema_name == "erp_planning_route":
                    data = loads(response.content)
                    data["windows"]["1"]["references"] = ["foreign-fee"]
                    payload = loads(request.user)
                    if "diagnostics" in payload:
                        self_test.assertNotIn(
                            "foreign-fee", [f["id"] for f in payload["reference_evidence"]["facts"]]
                        )
                    return replace(response, content=dumps(data))
                return response

        self_test = self
        client, memory = Unknown(), Memory()
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(client, Control(), memory).propose(source_snapshot())
        self.assertEqual(caught.exception.code, "planning.references")
        self.assertIn("foreign-fee", caught.exception.message)
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(all("findings" in loads(v["content"]) for v in memory.records.values()))

    def test_oversized_record_is_not_marked_read_or_silently_truncated(self):
        snap = source_snapshot()
        facts = tuple(
            replace(f, value="原文" * 15000) if f.id == "required-fee" else f
            for f in snap.reading.problem.facts
        )
        snap = replace(
            snap, reading=replace(snap.reading, problem=replace(snap.reading.problem, facts=facts))
        )
        evidence = PlanningKnowledge(snap).reference_batch(("required-fee",))
        self.assertFalse(evidence["facts"])
        self.assertEqual(evidence["remaining_count"], 1)
        with self.assertRaises(ERPError) as caught:
            ParallelPlanningGenerator(RouteClient(), Control()).propose(snap)
        self.assertEqual(caught.exception.code, "planning.context_limit")
        self.assertIn("required-fee", caught.exception.message)

    def test_reference_batch_is_deduplicated_and_bounded(self):
        knowledge = PlanningKnowledge(source_snapshot())
        packet = knowledge.reference_batch(("required-fee", "required-fee", "foreign"), max_items=1)
        self.assertEqual([f["id"] for f in packet["facts"]], ["required-fee"])
        self.assertEqual(packet["remaining_count"], 1)
        self.assertLessEqual(len(dumps(packet)), 12000)
