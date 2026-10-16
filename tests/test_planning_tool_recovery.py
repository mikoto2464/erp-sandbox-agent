"""真实工具边界上的上下文绑定、循环收束及共享预算回归。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from langchain_core.messages import AIMessage
from pydantic import Field

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_tools import PlanningTools
from erp_agent.application.ports import LLMRequest
from erp_agent.application.task_budget import TaskCallBudget, model_call
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from tests.helpers import snapshot
from tests.test_planning_pipeline import ToolChatModel, tool_reply


class RecordingModel(ToolChatModel):
    bindings: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bindings.append({"names": [t["name"] for t in tools], **kwargs})
        return super().bind_tools(tools, **kwargs)


class PlanningToolRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.knowledge = PlanningKnowledge(snapshot())
        self.request = LLMRequest(
            "基于已绑定的题目按需查询后提交。",
            "职责材料",
            "result",
            dumps(
                {
                    "type": "object",
                    "properties": {
                        "context_hash": {"type": "string", "const": self.knowledge.hash},
                        "value": {"type": "string"},
                    },
                    "required": ["value", "context_hash"],
                }
            ),
            stream=True,
            tool_context=self.knowledge.data,
        )

    def invoke(self, replies, *, budget=None, request=None, settings=None):
        model = RecordingModel(replies=replies)
        client = LangChainLLMClient(
            settings or Settings(llm_model="test", llm_api_key="fake"),
            lambda _: model,
        )
        if budget:
            with model_call(budget):
                result = client.generate(request or self.request)
        else:
            result = client.generate(request or self.request)
        return result, model

    def test_context_is_bound_without_model_copying_hash_in_lookup_or_submission(self):
        for schema in PlanningTools.schemas():
            self.assertNotIn("context_hash", schema["parameters"]["properties"])
            self.assertNotIn("context_hash", schema["parameters"]["required"])
        result, model = self.invoke(
            [
                tool_reply("planning_lookup", {"kind": "facts", "ids": ["cash"]}),
                tool_reply("result", {"value": "ok"}),
            ]
        )
        self.assertEqual(
            loads(result.content), {"context_hash": self.knowledge.hash, "value": "ok"}
        )
        self.assertEqual(result.accessed_reference_ids, ("cash",))
        self.assertEqual(model.calls, 2)

    def test_explicit_wrong_hash_is_rejected_then_call_can_be_corrected(self):
        original = PlanningTools.lookup
        accessed = []

        def lookup(tools, request):
            accessed.extend(request.ids)
            return original(tools, request)

        with patch.object(PlanningTools, "lookup", lookup):
            result, model = self.invoke(
                [
                    tool_reply(
                        "planning_lookup",
                        {"context_hash": "other-project", "kind": "facts", "ids": ["foreign-fact"]},
                    ),
                    tool_reply("planning_lookup", {"kind": "facts", "ids": ["cash"]}),
                    tool_reply("result", {"value": "ok"}),
                ]
            )
        self.assertEqual(accessed, ["cash"])
        feedback = loads(model.histories[1][-1].content)
        self.assertEqual(feedback["error"], "planning.context")
        self.assertIn("删除 context_hash", feedback["message"])
        self.assertEqual(loads(result.content)["context_hash"], self.knowledge.hash)

    def test_fifth_round_is_submission_only_and_fits_original_limit(self):
        replies = [
            tool_reply("planning_lookup", {"kind": "formulas", "offset": i, "limit": 1})
            for i in range(4)
        ] + [tool_reply("result", {"value": "ok"})]
        budget = TaskCallBudget(5)
        result, model = self.invoke(replies, budget=budget)
        self.assertEqual(model.calls, 5)
        self.assertEqual(budget.calls, 5)
        self.assertEqual(model.bindings[-1], {"names": ["result"], "tool_choice": "result"})
        self.assertEqual(loads(result.content)["value"], "ok")

    def test_remaining_budget_closes_queries_early(self):
        result, model = self.invoke(
            [
                tool_reply("planning_lookup", {"kind": "facts", "ids": ["cash"]}),
                tool_reply("result", {"value": "ok"}),
            ],
            budget=TaskCallBudget(2),
        )
        self.assertEqual(model.bindings[-1]["names"], ["result"])
        self.assertEqual(model.calls, 2)
        self.assertEqual(loads(result.content)["value"], "ok")

    def test_reserved_downstream_calls_are_not_spent_on_more_queries(self):
        budget = TaskCallBudget(4)
        request = replace(
            self.request, tool_context={**self.knowledge.data, "reserved_model_calls": 2}
        )
        _, model = self.invoke(
            [
                tool_reply("planning_lookup", {"kind": "facts", "ids": ["cash"]}),
                tool_reply("result", {"value": "ok"}),
            ],
            budget=budget,
            request=request,
        )
        self.assertEqual(budget.calls, 2)
        self.assertEqual(model.bindings[-1]["names"], ["result"])

    def test_repeat_lookup_reuses_result_and_closes_loop(self):
        query = {"kind": "facts", "ids": ["cash"]}
        original = PlanningTools.lookup
        count = []

        def lookup(tools, request):
            count.append(request.ids)
            return original(tools, request)

        with patch.object(PlanningTools, "lookup", lookup):
            _, model = self.invoke(
                [
                    tool_reply("planning_lookup", query),
                    tool_reply("planning_lookup", query),
                    tool_reply("result", {"value": "ok"}),
                ]
            )
        self.assertEqual(count, [["cash"]])
        self.assertEqual(model.bindings[-1]["names"], ["result"])

    def test_calculation_without_hash_is_idempotent_within_the_loop(self):
        args = {
            "id": "cost",
            "formula": "multiply",
            "unit": "元",
            "period": 1,
            "assumption": "采购情景",
            "inputs": [
                {"name": "quantity", "value": "3", "unit": "件", "reason": "拟定采购"},
                {"name": "unit_price", "value": "2.5", "unit": "元/件", "reason": "价格情景"},
            ],
        }
        result, model = self.invoke(
            [
                tool_reply("erp_calculate", args),
                tool_reply("erp_calculate", args),
                tool_reply("result", {"value": "ok"}),
            ]
        )
        self.assertEqual(len(result.calculations), 1)
        self.assertEqual(result.calculations[0].value, "7.5")
        self.assertEqual(result.calculations[0].context_hash, self.knowledge.hash)
        self.assertEqual(model.bindings[-1]["names"], ["result"])

    def test_validation_error_returns_field_path_before_single_correction(self):
        _, model = self.invoke(
            [
                tool_reply("planning_lookup", {"kind": "facts", "limit": "wrong"}),
                tool_reply("planning_lookup", {"kind": "facts", "limit": 1}),
                tool_reply("result", {"value": "ok"}),
            ]
        )
        feedback = loads(model.histories[1][-1].content)
        self.assertEqual(feedback["error"], "tool_arguments")
        self.assertEqual(feedback["fields"][0]["path"], "limit")

    def test_complete_plain_json_is_accepted_and_bound_but_wrong_context_is_not(self):
        result, _ = self.invoke(
            [AIMessage(content='{"value":"ok"}', response_metadata={"finish_reason": "stop"})]
        )
        self.assertEqual(loads(result.content)["context_hash"], self.knowledge.hash)
        with self.assertRaises(ERPError) as caught:
            self.invoke(
                [
                    AIMessage(
                        content='{"value":"ok","context_hash":"other"}',
                        response_metadata={"finish_reason": "stop"},
                    )
                ]
            )
        self.assertEqual(caught.exception.code, "planning.context")

    def test_wrong_context_submission_gets_one_safe_finalization_without_adoption(self):
        result, model = self.invoke(
            [
                tool_reply("result", {"context_hash": "other-project", "value": "not-adopted"}),
                tool_reply("result", {"value": "correct"}),
            ]
        )
        self.assertEqual(loads(result.content)["value"], "correct")
        self.assertEqual(loads(result.content)["context_hash"], self.knowledge.hash)
        self.assertEqual(model.bindings[-1]["names"], ["result"])

    def test_final_only_choice_uses_the_real_openai_sdk_without_copying_binding(self):
        import json

        from tests.test_langchain_llm import mock_transport

        response = {
            "id": "test",
            "object": "chat.completion",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "final",
                                "type": "function",
                                "function": {"name": "result", "arguments": '{"value":"ok"}'},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        seen = []
        with mock_transport(response, seen), model_call(TaskCallBudget(1)):
            result = LangChainLLMClient(
                Settings(llm_model="test", llm_api_key="fake", llm_retries=0)
            ).generate(replace(self.request, stream=False))
        body = json.loads(seen[0].content)
        self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "result"}})
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["result"])
        self.assertNotIn("context_hash", body["tools"][0]["function"]["parameters"]["properties"])
        self.assertEqual(loads(result.content)["context_hash"], self.knowledge.hash)

    def test_explicit_thinking_keeps_final_tool_without_forcing_tool_choice(self):
        _, model = self.invoke(
            [tool_reply("result", {"value": "ok"})],
            budget=TaskCallBudget(1),
            settings=Settings(llm_model="test", llm_api_key="fake", llm_thinking_mode="enabled"),
        )
        self.assertEqual(model.bindings[-1], {"names": ["result"]})

    def test_mixed_submission_waits_for_lookup_results(self):
        mixed = AIMessage(
            content="",
            response_metadata={"finish_reason": "tool_calls"},
            tool_calls=[
                {
                    "name": "planning_lookup",
                    "args": {"kind": "facts", "ids": ["cash"]},
                    "id": "lookup",
                },
                {"name": "result", "args": {"value": "early"}, "id": "early"},
            ],
        )
        result, model = self.invoke([mixed, tool_reply("result", {"value": "complete"})])
        self.assertEqual(loads(result.content)["value"], "complete")
        self.assertEqual(result.accessed_reference_ids, ("cash",))
        self.assertEqual(model.bindings[-1]["names"], ["result"])

    def test_isolated_application_http_solve_recovers_binding_and_finishes_within_budget(self):
        from tests.test_planning_pipeline import decisions
        from tests.test_web import WebTests

        case = WebTests("test_upload_read_revise_solve_and_isolation")
        models = {}

        class AdaptiveModel(RecordingModel):
            final_data: dict = Field(default_factory=dict)
            cash_id: str = ""
            bad_binding: bool = False

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                names = [t["name"] for t in kwargs.get("tools", [])]
                final_name = names[-1]
                if len(names) == 1 or self.calls >= 4:
                    reply = tool_reply(final_name, self.final_data)
                elif self.bad_binding and self.calls == 0:
                    reply = tool_reply(
                        "planning_lookup",
                        {
                            "context_hash": "model-mistyped-context",
                            "kind": "facts",
                            "ids": ["foreign-fact"],
                        },
                    )
                else:
                    reply = tool_reply(
                        "planning_lookup",
                        {
                            "kind": "facts",
                            "ids": [self.cash_id],
                            "offset": max(0, self.calls - 1) if self.bad_binding else 0,
                        },
                    )
                self.replies.append(reply)
                return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        def native_generate(request):
            context = request.tool_context
            cash = next(f["id"] for f in context["facts"] if f["key"] == "initial_cash")
            if request.schema_name == "erp_planning_analysis":
                data = {"summary": "按当前题目的资金与资源条件规划。", "findings": []}
            else:
                data = decisions(
                    context["context_hash"], context["years"] * context["periods_per_year"]
                )
                data.pop("context_hash")
                for actions in data["periods"].values():
                    for action in actions:
                        action["references"] = [cash]
            model = AdaptiveModel(
                final_data=data, cash_id=cash, bad_binding=request.task.id == "solver:operations"
            )
            models[request.task.id] = model
            return LangChainLLMClient(
                Settings(llm_model="test", llm_api_key="fake"), lambda _: model
            ).generate(request)

        with patch("erp_agent.api.server.Handler.log_message"):
            case.setUp()
            try:
                project, reading = case.setup_problem()
                store = case.server.workflow.model_settings
                store._base = replace(store._base, solver_max_model_calls=8)
                base = f"/api/projects/{project}"
                with patch.object(case.llm, "generate", side_effect=native_generate):
                    job = case.request(base + "/solve", {"version": reading["snapshot"]["version"]})
                    result = case.wait_job(project, job["id"])
                self.assertEqual(result["status"], "succeeded", result.get("error"))
                solution = case.request(base + "/solutions")[0]
                self.assertIsNotNone(solution["plan"]["preparation"])
                self.assertLessEqual(sum(m.calls for m in models.values()), 8)
                self.assertTrue(
                    all(
                        m.bindings[-1]["names"] == [m.bindings[-1]["names"][-1]]
                        for m in models.values()
                    )
                )
            finally:
                case.tearDown()
