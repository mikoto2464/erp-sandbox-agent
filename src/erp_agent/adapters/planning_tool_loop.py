"""上下文由服务端绑定；在有界查询后预留一次最终提交。"""

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from erp_agent.application.contracts import JSONObject, dumps, loads, obj
from erp_agent.application.llm_recovery import current_tool_recovery
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.planning_context import context_digest
from erp_agent.application.planning_handoff import requested_references
from erp_agent.application.planning_tools import PlanningTools, tool_error
from erp_agent.application.ports import LLMRequest
from erp_agent.application.task_budget import (
    current_run_control,
    followup_model_call,
    remaining_model_calls,
)
from erp_agent.application.task_reporting import report_task
from erp_agent.domain.errors import ERPError

MAX_MODEL_ROUNDS = 5


@dataclass
class ToolConversation:
    messages: list[BaseMessage]
    tools: PlanningTools
    memo: dict[str, JSONObject] = field(default_factory=dict)
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
    )
    finish_next: bool = False
    rounds: int = 0

    @property
    def can_retry(self) -> bool:
        return self.rounds < MAX_MODEL_ROUNDS


def _submission_schema(request: LLMRequest) -> JSONObject:
    schema = deepcopy(obj(loads(request.schema_json)))
    schema.get("properties", {}).pop("context_hash", None)
    if "context_hash" in schema.get("required", []):
        schema["required"].remove("context_hash")
    return schema


def _reference_schema(schema: JSONObject, request: LLMRequest, tools: PlanningTools) -> JSONObject:
    """工具返回新资料后刷新共享引用枚举，减少长 ID 的抄写错误，不放入全题目录。"""
    allowed = {e["id"] for kind in ("facts", "rules", "issues") for e in tools.context[kind]}
    visible = set(tools.accessed_reference_ids)
    try:
        payload = obj(loads(request.user))
    except ERPError:
        payload = {}
    visible.update(requested_references(payload))
    context = payload.get("context", {})
    if isinstance(context, dict):
        visible.update(context.get("unloaded_reference_ids", []))
    identifiers = sorted(visible & allowed)
    result = deepcopy(schema)
    used = False

    def visit(node: object) -> None:
        nonlocal used
        if isinstance(node, dict):
            properties = node.get("properties", {})
            if "references" in properties:
                properties["references"]["items"] = {"$ref": "#/$defs/planning_reference"}
                used = True
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    if used:
        result.setdefault("$defs", {})["planning_reference"] = (
            {"type": "string", "enum": identifiers}
            if identifiers
            else {"type": "string", "not": {}}
        )
    return result


def _bind_submission(arguments: JSONObject, request: LLMRequest, schema: JSONObject) -> JSONObject:
    expected = (request.tool_context or {}).get("context_hash")
    if "context_hash" in arguments and arguments["context_hash"] != expected:
        raise ERPError(
            "planning.context", "提交指定了错误题目；本工具已绑定当前题目，请省略 context_hash。"
        )
    if schema.get("additionalProperties") is False and (
        set(arguments) - set(schema.get("properties", {})) - {"context_hash"}
    ):
        raise ERPError(
            "llm_structured_response", "最终提交包含未定义字段，请只返回提交契约内字段。", 502
        )
    if not set(schema.get("required", [])) <= set(arguments):
        raise ERPError(
            "llm_structured_response", "最终提交缺少必要字段，请按提交工具契约完整返回。", 502
        )
    return (
        {**arguments, "context_hash": expected}
        if "context_hash" in obj(loads(request.schema_json)).get("properties", {})
        else {key: value for key, value in arguments.items() if key != "context_hash"}
    )


def invoke_planning_tools(
    model: BaseChatModel,
    request: LLMRequest,
    messages: list[BaseMessage],
    max_chars: int,
    validate: Callable[[object], None],
    check_capacity: Callable[[list[BaseMessage], list[dict]], None],
    model_for_turn: Callable[[], BaseChatModel] | None = None,
    force_tool_choice: bool = True,
) -> tuple[dict[str, Any], PlanningTools]:
    recovery = current_tool_recovery()
    key = context_digest(
        {
            "task": request.task,
            "context_hash": (request.tool_context or {}).get("context_hash"),
            "system": request.system,
            "user": request.user,
            "schema": request.schema_json,
        }
    )
    session = recovery.get(key) if recovery is not None else None
    if not isinstance(session, ToolConversation):
        session = ToolConversation(messages, PlanningTools(request.tool_context or {}))
        if recovery is not None:
            recovery[key] = session
    messages, tools = session.messages, session.tools
    schema = _submission_schema(request)
    submission = {
        "name": request.schema_name,
        "description": "提交本职责的完整结构化结果。题目绑定由服务端附加，无需填写 context_hash。",
        "parameters": schema,
    }
    usage_total, memo = session.usage, session.memo
    reserve = max(0, int((request.tool_context or {}).get("reserved_model_calls", 0)))
    start = session.rounds
    for turn in range(start, MAX_MODEL_ROUNDS):
        control = current_run_control()
        if control:
            control.checkpoint("planning_tools")
        if turn > start:
            followup_model_call()
        remaining = remaining_model_calls()
        finish_only = (
            session.finish_next
            or turn == MAX_MODEL_ROUNDS - 1
            or (remaining is not None and remaining <= reserve)
        )
        submission = {**submission, "parameters": _reference_schema(schema, request, tools)}
        definitions = PlanningTools.schemas() + [submission]
        active_definitions = [submission] if finish_only else definitions
        if finish_only:
            messages.append(
                HumanMessage(
                    content=(
                        "必要查询阶段结束，现在只调用 " + request.schema_name + " 完整提交。"
                        "复用已返回的证据和计算；未知保留未知与限制，不编造数值，不再查询。"
                        "题目绑定由服务端附加，不要填写 context_hash。"
                    )
                )
            )
        if control and request.task:
            report_task(
                control,
                request.task,
                "running",
                tool_round=turn + 1,
                tool_phase="finalizing" if finish_only else "retrieving",
                remaining_model_calls=remaining,
            )
        if len(dumps(active_definitions)) + sum(len(str(m.content)) for m in messages) > max_chars:
            raise ERPError("llm_input_limit", "按需查询后的上下文超过当前模型输入容量。", 413)
        check_capacity(messages, active_definitions)
        try:
            current = model_for_turn() if model_for_turn else model
            runnable = current.bind_tools(
                active_definitions,
                **(
                    {"tool_choice": request.schema_name}
                    if finish_only and force_tool_choice
                    else {}
                ),
            )
        except NotImplementedError as error:
            raise ERPError(
                "llm_tools_unsupported", "此模型不支持原生工具调用，请使用支持工具的预设。"
            ) from error
        # 重试同一失败请求时保留前面实际查询/计算的消息，不回到只有原始投影的状态。
        session.rounds += 1
        output = runnable.invoke(messages, **({"stream": True} if request.stream else {}))
        validate(output)
        if control:
            control.checkpoint("planning_tools")
        usage = getattr(output, "usage_metadata", None) or {}
        for key in usage_total:
            usage_total[key] += usage.get(key, 0)
        calls = getattr(output, "tool_calls", [])
        if getattr(output, "invalid_tool_calls", []):
            raise ERPError("llm_structured_response", "模型工具参数不是完整 JSON。", 502)
        if len(calls) == 1 and calls[0]["name"] == request.schema_name:
            try:
                parsed = _bind_submission(obj(calls[0]["args"]), request, schema)
            except ERPError as error:
                if finish_only:
                    raise
                messages.extend(
                    [
                        output,
                        ToolMessage(content=dumps(tool_error(error)), tool_call_id=calls[0]["id"]),
                    ]
                )
                session.finish_next = True
                continue
            output = output.model_copy(update={"usage_metadata": usage_total})
            return {"raw": output, "parsed": parsed, "parsing_error": None}, tools
        if not calls:
            # 某些兼容模型直接返回完整 JSON；仍执行绑定与应用层结构/业务校验。
            content = getattr(output, "content", "")
            if isinstance(content, str) and content.strip():
                parsed = _bind_submission(
                    obj(loads(normalize_model_json(content))), request, schema
                )
                output = output.model_copy(update={"usage_metadata": usage_total})
                return {"raw": output, "parsed": parsed, "parsing_error": None}, tools
        if finish_only:
            raise ERPError(
                "task_call_limit" if remaining == 0 else "llm_structured_response",
                "模型在最终提交阶段仍未返回完整结果；当前查询未被当作成功方案。",
                429 if remaining == 0 else 502,
            )
        if not calls or len(calls) > 16:
            raise ERPError("llm_structured_response", "模型未提交结果或单轮工具数量超限。", 502)
        messages.append(output)
        new_calls = 0
        mixed_submission = False
        for call in calls:
            if control:
                control.checkpoint("planning_tools")
            if call["name"] == request.schema_name:
                mixed_submission = True
                result = {
                    "error": "submission_order",
                    "message": "请先读取本轮工具结果，再单独完整提交。",
                }
            else:
                arguments = obj(call["args"])
                key = json.dumps([call["name"], arguments], ensure_ascii=False, sort_keys=True)
                if key in memo:
                    result = memo[key]
                else:
                    new_calls += 1
                    try:
                        result = tools.invoke(call["name"], arguments)
                    except (ERPError, ValidationError) as error:
                        if isinstance(error, ERPError) and error.code in {
                            "cancelled",
                            "task_deadline",
                        }:
                            raise
                        result = tool_error(error)
                    # 批次部分失败时允许再试：前置计算可能已在后续批次中完成。
                    # 成功条目仍通过稳定 ID 复用，不能缓存整批的暂时错误。
                    if call["name"] != "erp_calculate_batch" or (
                        "results" in result and all("value" in r for r in result["results"])
                    ):
                        memo[key] = result
            messages.append(ToolMessage(content=dumps(result), tool_call_id=call["id"]))
        session.finish_next = mixed_submission or new_calls == 0
    raise ERPError("llm_structured_response", "模型没有完成最终提交。", 502)
