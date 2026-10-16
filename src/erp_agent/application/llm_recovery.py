import time
from contextvars import ContextVar
from dataclasses import replace

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse, RunControl
from erp_agent.application.task_budget import model_call, run_control, task_budget
from erp_agent.application.task_reporting import report_task
from erp_agent.domain.errors import ERPError, ModelOutputTruncated

TRANSIENT_ERRORS = {"llm_gateway_timeout", "llm_unavailable", "llm_timeout", "llm_connection"}
_tool_recovery: ContextVar[dict[str, object] | None] = ContextVar("erp_tool_recovery", default=None)


def current_tool_recovery() -> dict[str, object] | None:
    """仅在一次有限重试内保留工具对话，任务/线程/修正请求之间不共享。"""
    return _tool_recovery.get()


def _tools_have_room() -> bool:
    return all(getattr(state, "can_retry", True) for state in (_tool_recovery.get() or {}).values())


def _can_retry(error: Exception) -> bool:
    return isinstance(error, ERPError) and error.code in TRANSIENT_ERRORS and _tools_have_room()


def recovery_call(client: LLMClient, request: LLMRequest, control: RunControl) -> LLMResponse:
    token = _tool_recovery.set({})
    try:
        return _recovery_call(client, request, control)
    finally:
        _tool_recovery.reset(token)


def _recovery_call(client: LLMClient, request: LLMRequest, control: RunControl) -> LLMResponse:
    task = request.task or getattr(client, "task", None)
    configured = client
    for _ in range(5):
        if hasattr(configured, "task_max_calls"):
            break
        nested = getattr(configured, "client", None)
        if nested is None:
            break
        configured = nested
    budget = (
        task_budget(
            control,
            task,
            getattr(configured, "task_max_calls", 8),
            getattr(configured, "task_deadline_seconds", 900),
        )
        if task
        else None
    )

    def pause(seconds: float) -> None:
        if request.task:
            report_task(
                control,
                request.task,
                "waiting",
                stage="retrying_strategy",
                recovery="瞬时接口故障，退避后重试当前任务。",
            )
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if budget:
                budget.check()
            control.checkpoint("retrying_strategy")
            time.sleep(min(0.1, remaining))

    retry = Retrying(
        stop=stop_after_attempt(2),
        wait=wait_random_exponential(multiplier=1, min=1, max=3),
        retry=retry_if_exception(_can_retry),
        sleep=pause,
        reraise=True,
    )

    def generate(current: LLMRequest) -> LLMResponse:
        control.checkpoint("model_execution")

        def attempt() -> LLMResponse:
            if budget:
                with model_call(budget), run_control(control):
                    return client.generate(replace(current, retries=0))
            return client.generate(replace(current, retries=0))

        response = retry(attempt)
        if current.task:
            report_task(
                control,
                current.task,
                "running",
                output_limit=response.output_limit or current.max_output_tokens,
                output_tokens=response.output_tokens,
                reasoning_tokens=response.reasoning_tokens,
                elapsed_seconds=response.elapsed_seconds,
            )
        return response

    try:
        try:
            return generate(request)
        except ERPError as error:
            scheduled = client
            for _ in range(4):
                if hasattr(scheduled, "role_candidates"):
                    break
                wrapped = getattr(scheduled, "client", None)
                if wrapped is None:
                    break
                scheduled = wrapped
            models = getattr(scheduled, "models", ())
            if not _can_retry(error) or len(models) < 2:
                raise
            # 同任务最多切换一次，仍使用共同调用/时间边界。
            return generate(request)
    except ModelOutputTruncated as error:
        if not _tools_have_room():
            raise
        expanded = min(
            error.configured_limit,
            max(
                error.output_limit * 2,
                (error.reasoning_tokens or 0) + (request.max_output_tokens or 0) + 2048,
            ),
        )
        if expanded <= error.output_limit:
            raise
        if request.task:
            report_task(
                control,
                request.task,
                "waiting",
                stage="expanding_output_budget",
                output_limit=expanded,
                output_tokens=error.output_tokens,
                reasoning_tokens=error.reasoning_tokens,
                recovery=(
                    f"上次总预算 {error.output_limit} Token 已截断，"
                    f"扩至 {expanded} Token 重试一次。"
                ),
            )
        return generate(replace(request, max_output_tokens=expanded, reasoning_reserve_tokens=0))
