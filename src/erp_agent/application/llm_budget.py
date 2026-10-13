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
