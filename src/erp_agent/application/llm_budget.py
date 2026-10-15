from erp_agent.application.ports import LLMRequest


def output_token_limit(
    request: LLMRequest,
    maximum: int,
    thinking_mode: str = "default",
    provider: str = "",
    thinking_budget: int = 8192,
) -> int:
    content_limit = min(request.max_output_tokens or maximum, maximum)
    explicit_thinking = provider == "anthropic" and thinking_mode == "enabled"
    if thinking_mode != "disabled" and request.reasoning_reserve_tokens:
        reserve = max(request.reasoning_reserve_tokens, thinking_budget if explicit_thinking else 0)
        return min(maximum, content_limit + reserve)
    if explicit_thinking:
        return min(maximum, max(content_limit, thinking_budget + 1024))
    return content_limit


def request_input_chars(request: LLMRequest) -> int:
    """只计真正发送的上下文、输出契约与工具签名，服务端查询目录不占输入。"""
    from erp_agent.application.contracts import dumps
    from erp_agent.application.planning_tools import PlanningTools

    return (
        len(request.system)
        + len(request.user)
        + len(request.schema_json)
        + (len(dumps(PlanningTools.schemas())) + 300 if request.tool_context is not None else 0)
    )
