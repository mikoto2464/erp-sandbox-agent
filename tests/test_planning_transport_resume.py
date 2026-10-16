"""临时网络失败重试保留原生工具对话，仍受同一调用/轮数上限。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from tenacity import wait_none

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.llm_recovery import current_tool_recovery, recovery_call
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.ports import LLMRequest, TaskContext
from erp_agent.application.task_budget import task_budget
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from tests.helpers import Control, snapshot
from tests.test_planning_batches import cost
from tests.test_planning_pipeline import ToolChatModel, tool_reply


class FailingModel(ToolChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(self.replies[self.calls], Exception):
            error = self.replies[self.calls]
            self.calls += 1
            self.histories.append(list(messages))
            raise error
        return super()._generate(list(messages), stop, run_manager, **kwargs)


class PlanningTransportResumeTests(unittest.TestCase):
    def request(self):
        knowledge = PlanningKnowledge(snapshot())
        return LLMRequest(
            "按当前题目提交",
            "职责材料",
            "result",
            dumps(
                {
                    "type": "object",
                    "properties": {"context_hash": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["context_hash", "value"],
                }
            ),
            task=TaskContext("solver:operations", "Solver", "产销分析", knowledge.hash),
            tool_context=knowledge.data,
        )

    def test_retry_after_tools_preserves_messages_values_and_usage(self):
        first = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "lookup",
                    "name": "planning_lookup",
                    "args": {"kind": "facts", "ids": ["cash"]},
                },
                {"id": "batch", "name": "erp_calculate_batch", "args": {"calculations": [cost()]}},
            ],
        )
        model = FailingModel(
            replies=[
                first,
                ERPError("llm_gateway_timeout", "controlled 504"),
                tool_reply("result", {"value": "7.5"}),
            ]
        )
        client = LangChainLLMClient(Settings(llm_model="test", llm_api_key="fake"), lambda _: model)
        request, control = self.request(), Control()
        with patch(
            "erp_agent.application.llm_recovery.wait_random_exponential", return_value=wait_none()
        ):
            result = recovery_call(client, request, control)
        self.assertEqual(model.calls, 3)
        self.assertEqual(task_budget(control, request.task).calls, 3)
        history = model.histories[-1]
        outputs = [loads(m.content) for m in history if isinstance(m, ToolMessage)]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0]["items"][0]["id"], "cash")
        self.assertEqual(outputs[1]["results"][0]["value"], "7.5")
        self.assertEqual(result.calculations[0].value, "7.5")
        self.assertIn("cash", result.accessed_reference_ids)
        self.assertIsNone(current_tool_recovery())

    def test_network_error_on_fifth_round_does_not_reset_round_counter(self):
        replies = [
            tool_reply("planning_lookup", {"kind": "formulas", "offset": i, "limit": 1})
            for i in range(4)
        ]
        replies.append(ERPError("llm_gateway_timeout", "controlled final 504"))
        model = FailingModel(replies=replies)
        client = LangChainLLMClient(Settings(llm_model="test", llm_api_key="fake"), lambda _: model)
        request, control = self.request(), Control()
        with patch(
            "erp_agent.application.llm_recovery.wait_random_exponential", return_value=wait_none()
        ):
            with self.assertRaises(ERPError) as caught:
                recovery_call(client, request, control)
        self.assertEqual(caught.exception.code, "llm_gateway_timeout")
        self.assertEqual(model.calls, 5)
        self.assertEqual(task_budget(control, request.task).calls, 5)
        self.assertIsNone(current_tool_recovery())

    def test_next_recovery_call_cannot_inherit_previous_task_tools(self):
        model = FailingModel(
            replies=[
                tool_reply("planning_lookup", {"kind": "facts", "ids": ["cash"]}),
                tool_reply("result", {"value": "ok"}),
                tool_reply("result", {"value": "other"}),
            ]
        )
        client = LangChainLLMClient(Settings(llm_model="test", llm_api_key="fake"), lambda _: model)
        request, control = self.request(), Control()
        recovery_call(client, request, control)
        other = replace(request, task=TaskContext("solver:finance", "Solver", "资金分析", ""))
        response = recovery_call(client, other, control)
        self.assertFalse(any(isinstance(m, ToolMessage) for m in model.histories[-1]))
        self.assertEqual(response.accessed_reference_ids, ())
        self.assertIsNone(current_tool_recovery())
