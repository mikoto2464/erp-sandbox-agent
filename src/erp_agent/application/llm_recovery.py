import time

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse, RunControl
from erp_agent.application.task_reporting import report_task
from erp_agent.domain.errors import ERPError

TRANSIENT_ERRORS = {"llm_gateway_timeout", "llm_unavailable", "llm_timeout", "llm_connection"}


def recovery_call(client: LLMClient, request: LLMRequest, control: RunControl) -> LLMResponse:
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
            control.checkpoint("retrying_strategy")
            time.sleep(min(0.1, remaining))

    retry = Retrying(
        stop=stop_after_attempt(2),
        wait=wait_random_exponential(multiplier=1, min=1, max=3),
        retry=retry_if_exception(lambda e: isinstance(e, ERPError) and e.code in TRANSIENT_ERRORS),
        sleep=pause,
        reraise=True,
    )
    return retry(client.generate, request)
