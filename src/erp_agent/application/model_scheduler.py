import threading
from dataclasses import replace

from erp_agent.application.contracts import dumps
from erp_agent.application.ports import LLMRequest, LLMResponse, ModelBinding, RunControl
from erp_agent.application.task_reporting import report_task
from erp_agent.domain.errors import ERPError


class ModelCapacity:
    """同一服务内共享并发额度，包括已冻结配置的在途任务。"""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.used: dict[str, int] = {}
        self.limits: dict[str, int] = {}

    def configure(self, limits: dict[str, int]) -> None:
        with self.condition:
            self.limits = limits
            self.condition.notify_all()


class ScheduledLLMClient:
    def __init__(self, models: tuple[ModelBinding, ...], capacity: ModelCapacity) -> None:
        self.models, self.capacity = models, capacity

    @property
    def configured(self) -> bool:
        return bool(self.models)

    @property
    def max_input_chars(self) -> int:
        return min(int(m.configuration["max_input_chars"]) for m in self.models)

    @property
    def request_overhead_chars(self) -> int:
        return 1000

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self.generate_controlled(request, None)

    def generate_controlled(self, request: LLMRequest, control: RunControl | None) -> LLMResponse:
        if not self.models:
            raise ERPError("llm_not_configured", "没有可调度的完整模型预设。", 503)
        ordered = sorted(self.models, key=lambda m: -int(m.configuration["priority"]))
        # 复杂工作保留最高优先级模型；简单提取优先分给其他模型，满载时回用主模型。
        candidates = ordered[1:] + ordered[:1] if request.complexity == "simple" else ordered[:1]
        if control and request.task:
            report_task(
                control, request.task, "waiting", candidates=[m.preset_name for m in candidates]
            )
        while True:
            if control:
                check = getattr(control, "check_cancelled", None)
                if check:
                    check()
                else:
                    control.checkpoint("waiting_model")
            with self.capacity.condition:
                selected = next(
                    (
                        m
                        for m in candidates
                        if self.capacity.used.get(m.preset_id, 0)
                        < min(
                            int(m.configuration["max_parallel"]),
                            self.capacity.limits.get(
                                m.preset_id, int(m.configuration["max_parallel"])
                            ),
                        )
                    ),
                    None,
                )
                if selected:
                    self.capacity.used[selected.preset_id] = (
                        self.capacity.used.get(selected.preset_id, 0) + 1
                    )
                    break
                self.capacity.condition.wait(timeout=0.2)
        try:
            if control and request.task:
                report_task(
                    control,
                    request.task,
                    "running",
                    model=selected.configuration["model"],
                    preset_name=selected.preset_name,
                    preset_id=selected.preset_id,
                    preset_revision=selected.preset_revision,
                )
            available = [
                {
                    "name": m.preset_name[:40],
                    "model": str(m.configuration["model"])[:70],
                    "priority": m.configuration["priority"],
                    "max_parallel": m.configuration["max_parallel"],
                }
                for m in ordered
            ]
            context = dumps(available)
            # 全部模型目录留在任务元数据；请求只带有界的调度摘要。
            if len(context) > 850:
                context = dumps(available[:3]) + f"；共 {len(available)} 个模型"
            response = selected.client.generate(
                replace(
                    request,
                    system=request.system
                    + "\n模型协作由系统调度，当前任务只完成指定职责；"
                    + "规范数据与共享记忆对齐。可用模型："
                    + context,
                )
            )
            return response
        finally:
            with self.capacity.condition:
                self.capacity.used[selected.preset_id] -= 1
                self.capacity.condition.notify_all()
