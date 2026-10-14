import threading
import unittest
from unittest.mock import patch

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.report_composer import CollaborativeReportComposer, decode_outline
from erp_agent.domain.errors import ERPError
from tests.helpers import Control, FakeLLM
from tests.test_artifacts import report
from tests.test_workbook_mapper import Memory, TaskControl


class ReportComposerTests(unittest.TestCase):
    def test_roles_work_concurrently_with_fixed_plan_and_resume_cached_result(self):
        data = report()
        barrier = threading.Barrier(2)

        class Client(FakeLLM):
            def __init__(self):
                super().__init__("")
                self.plans = []

            def plan_output(self, requests, control, concurrency):
                self.plans.append((requests, concurrency))

            def generate(self, request):
                barrier.wait(timeout=3)
                return super().generate(request)

        client, memory, control = Client(), Memory(), TaskControl()
        composer = CollaborativeReportComposer(memory)
        result = composer.compose(data, client, control)
        self.assertTrue(result.strategy)
        self.assertTrue(result.risks)
        self.assertEqual(len(client.plans), 1)
        self.assertEqual(client.plans[0][1], 2)
        for request in client.calls:
            payload = loads(request.user)
            self.assertEqual(payload["solution_id"], data.solution.id)
            self.assertNotIn("budget_templates", payload)
            self.assertNotIn("raw_response", payload)
            self.assertTrue(request.stream)
            self.assertEqual(request.reasoning_reserve_tokens, 8192)
        self.assertEqual(composer.compose(data, client, control), result)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(any(meta["status"] == "cached" for _, meta in control.tasks))

    def test_empty_role_is_repaired_once_and_cannot_be_cached_as_success(self):
        client = FakeLLM(dumps({"strategy": [], "milestones": [], "risks": []}))
        client.generate = lambda request: (
            client.calls.append(request) or LLMResponse(client.response, "empty")
        )
        memory = Memory()
        with self.assertRaises(ERPError) as error:
            CollaborativeReportComposer(memory).compose(report(), client, Control())
        self.assertEqual(error.exception.code, "report_role_invalid")
        self.assertEqual(len(client.calls), 4)
        self.assertFalse(memory.entries)

    def test_failed_role_resumes_without_regenerating_completed_explanation(self):
        class Client(FakeLLM):
            failing = True

            def generate(self, request):
                if loads(request.user)["role"] == "execution" and self.failing:
                    raise ERPError("llm_connection", "连接失败", 502)
                return super().generate(request)

        client, memory = Client(""), Memory()
        composer = CollaborativeReportComposer(memory)
        with patch(
            "erp_agent.application.llm_recovery.wait_random_exponential",
            return_value=lambda state: 0,
        ):
            with self.assertRaises(ERPError):
                composer.compose(report(), client, Control())
        client.failing = False
        result = composer.compose(report(), client, Control())
        self.assertTrue(result.risks)
        self.assertEqual(sum(loads(r.user)["role"] == "strategy" for r in client.calls), 1)

    def test_numeric_claims_and_wrong_refs_are_rejected(self):
        data = report()
        note = {
            "title": "风险",
            "explanation": "利润提升百分之五",
            "action_indices": [0],
            "fact_ids": [],
            "rule_ids": [],
            "budget_indices": [],
            "period": None,
            "condition": "",
            "response": "",
        }
        for changes in (
            {},
            {"explanation": "追加两台设备"},
            {"explanation": "预留现金", "action_indices": [200]},
        ):
            with self.assertRaises(ERPError):
                decode_outline(
                    {"strategy": [{**note, **changes}], "milestones": [], "risks": []}, data
                )

    def test_local_report_patch_repairs_only_invalid_note_and_preserves_plan(self):
        data = report()
        original = data.solution.plan

        class Client(FakeLLM):
            def generate(self, request):
                if request.schema_name == "erp_report_patch":
                    self.calls.append(request)
                    payload = loads(request.user)
                    target = payload["allowed_targets"][0]
                    note = {**target["note"], "explanation": "保留现有行动并核对执行条件。"}
                    return LLMResponse(
                        dumps(
                            {
                                "patches": [
                                    {
                                        "section": target["section"],
                                        "index": target["index"],
                                        "note": note,
                                    }
                                ]
                            }
                        ),
                        "patch-model",
                    )
                response = super().generate(request)
                if loads(request.user)["role"] == "strategy":
                    output = loads(response.content)
                    output["strategy"][0]["explanation"] = "追加两台设备"
                    return LLMResponse(dumps(output), response.model)
                return response

        client = Client("")
        result = CollaborativeReportComposer().compose(data, client, Control())
        self.assertEqual(result.strategy[0].explanation, "保留现有行动并核对执行条件。")
        self.assertEqual(data.solution.plan, original)
        patches = [r for r in client.calls if r.schema_name == "erp_report_patch"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(len(loads(patches[0].user)["allowed_targets"]), 1)
