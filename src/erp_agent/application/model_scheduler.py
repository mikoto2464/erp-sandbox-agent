import hashlib
import threading

from erp_agent.application.contracts import JSONObject
from erp_agent.application.model_dispatch import DispatchDecision, ModelAssignment, ModelDispatcher
from erp_agent.application.ports import (
    LLMRequest,
    LLMResponse,
    ModelBinding,
    RunControl,
    TaskContext,
)
from erp_agent.application.task_reporting import report_task
from erp_agent.domain.errors import Cancelled, ERPError


class ModelCapacity:
    """同一服务内共享并发额度，包括主 Agent 的分配请求。"""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.used: dict[str, int] = {}
        self.limits: dict[str, int] = {}

    def configure(self, limits: dict[str, int]) -> None:
        with self.condition:
            self.limits = limits
            self.condition.notify_all()

    def limit(self, model: ModelBinding) -> int:
        return min(
            int(model.configuration["max_parallel"]),
            self.limits.get(model.preset_id, int(model.configuration["max_parallel"])),
        )

    def catalog(self, models: tuple[ModelBinding, ...]) -> list[JSONObject]:
        with self.condition:
            return [
                {
                    "id": m.preset_id,
                    "name": m.preset_name,
                    "model": m.configuration["model"],
                    "priority": m.configuration["priority"],
                    "max_parallel": self.limit(m),
                    "in_use": self.used.get(m.preset_id, 0),
                    "available": max(0, self.limit(m) - self.used.get(m.preset_id, 0)),
                    "capabilities": m.configuration.get("capabilities", "未填写"),
                    "max_input_chars": m.configuration["max_input_chars"],
                    "max_output_tokens": m.configuration.get("max_output_tokens"),
                }
                for m in models
            ]

    def release(self, model: ModelBinding) -> None:
        with self.condition:
            self.used[model.preset_id] -= 1
            self.condition.notify_all()


class ScheduledLLMClient:
    def __init__(
        self,
        models: tuple[ModelBinding, ...],
        capacity: ModelCapacity,
        dispatcher: ModelDispatcher | None = None,
    ) -> None:
        self.models = tuple(sorted(models, key=lambda m: -int(m.configuration["priority"])))
        self.capacity = capacity
        self.dispatcher = dispatcher or ModelDispatcher()
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.models)

    @property
    def max_input_chars(self) -> int:
        return max(int(m.configuration["max_input_chars"]) for m in self.models)

    @property
    def request_overhead_chars(self) -> int:
        return 0

    @staticmethod
    def _check(control: RunControl | None) -> None:
        if control:
            check = getattr(control, "check_cancelled", None)
            if check:
                check()
            else:
                control.checkpoint("waiting_model")

    def _acquire(
        self, candidates: tuple[ModelBinding, ...], control: RunControl | None
    ) -> ModelBinding:
        while True:
            self._check(control)
            with self.capacity.condition:
                selected = next(
                    (
                        m
                        for m in candidates
                        if self.capacity.used.get(m.preset_id, 0) < self.capacity.limit(m)
                    ),
                    None,
                )
                if selected:
                    self.capacity.used[selected.preset_id] = (
                        self.capacity.used.get(selected.preset_id, 0) + 1
                    )
                    return selected
                self.capacity.condition.wait(timeout=0.2)

    def _task(self, request: LLMRequest) -> tuple[TaskContext, TaskContext]:
        child = request.task or TaskContext(
            hashlib.sha256(request.user.encode()).hexdigest()[:24],
            "Agent",
            request.schema_name,
            "模型任务",
        )
        with self._lock:
            attempt = self._attempts.get(child.id, 0) + 1
            self._attempts[child.id] = attempt
        dispatch = TaskContext(
            f"dispatch:{child.id}:{attempt}",
            "主 Agent",
            "评估难度与分配模型",
            child.object,
            child.parent_id,
        )
        return child, dispatch

    def _dispatch(
        self,
        request: LLMRequest,
        eligible: tuple[ModelBinding, ...],
        control: RunControl | None,
        child: TaskContext,
        task: TaskContext,
    ) -> DispatchDecision:
        primary = self.models[0]
        if control:
            report_task(
                control,
                task,
                "waiting",
                candidates=[primary.preset_name],
                stage="dispatching_model",
            )
            report_task(control, child, "queued", stage="dispatching_model")
        acquired = self._acquire((primary,), control)
        try:
            self._check(control)
            if control:
                report_task(
                    control,
                    task,
                    "running",
                    model=primary.configuration["model"],
                    preset_name=primary.preset_name,
                    stage="dispatching_model",
                )
            catalog = self.capacity.catalog(self.models)
            eligible_ids = {model.preset_id for model in eligible}
            for model in catalog:
                model["eligible"] = model["id"] in eligible_ids
                model["available_after_dispatch"] = int(model["available"]) + (
                    1 if model["id"] == primary.preset_id else 0
                )
            try:
                decision = self.dispatcher.decide(
                    request, catalog, primary.client, int(primary.configuration["max_input_chars"])
                )
            except Cancelled:
                raise
            except ERPError as error:
                # 主模型调度失败不能让任务转给不确定能力的模型；明确标记回退。
                selected = primary if primary in eligible else eligible[0]
                reason = (
                    "主 Agent 分配失败，保守回退到可承载输入的最高优先级模型；"
                    + error.message[:200]
                )
                decision = DispatchDecision(
                    "complex", (ModelAssignment(selected.preset_id, reason),), reason, fallback=True
                )
            self._check(control)
            if control:
                report_task(
                    control,
                    task,
                    "failed" if decision.fallback else "succeeded",
                    difficulty=decision.difficulty,
                    dispatch_reason=decision.summary,
                    dispatch_fallback=decision.fallback,
                    assignments=[
                        {"preset_id": a.preset_id, "reason": a.reason} for a in decision.assignments
                    ],
                    model_loads=catalog,
                )
            return decision
        finally:
            self.capacity.release(acquired)

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self.generate_controlled(request, None)

    def generate_controlled(self, request: LLMRequest, control: RunControl | None) -> LLMResponse:
        if not self.models:
            raise ERPError("llm_not_configured", "没有可调度的完整模型预设。", 503)
        size = len(request.system) + len(request.user) + len(request.schema_json)
        eligible = tuple(m for m in self.models if int(m.configuration["max_input_chars"]) >= size)
        if not eligible:
            raise ERPError("llm_input_limit", "任务输入超过所有协作模型的上限，请缩小任务。", 413)
        child, dispatch = self._task(request)
        if len(self.models) > 1:
            decision = self._dispatch(request, eligible, control, child, dispatch)
        else:
            reason = "仅一个可用模型，直接使用当前模型，不额外请求主 Agent 分配。"
            decision = DispatchDecision(
                "unassessed", (ModelAssignment(eligible[0].preset_id, reason),), reason
            )
        allowed = {m.preset_id: m for m in eligible}
        candidates = tuple(allowed[a.preset_id] for a in decision.assignments)
        if control:
            report_task(
                control,
                child,
                "waiting",
                candidates=[m.preset_name for m in candidates],
                difficulty=decision.difficulty,
                dispatch_reason=decision.summary,
                dispatch_fallback=decision.fallback,
                coordinator=dispatch.id if len(self.models) > 1 else None,
                stage="model_execution",
            )
        selected = self._acquire(candidates, control)
        try:
            self._check(control)
            reason = next(
                a.reason for a in decision.assignments if a.preset_id == selected.preset_id
            )
            if control:
                report_task(
                    control,
                    child,
                    "running",
                    model=selected.configuration["model"],
                    preset_name=selected.preset_name,
                    preset_id=selected.preset_id,
                    preset_revision=selected.preset_revision,
                    assignment_reason=reason,
                    stage="model_execution",
                    model_loads=self.capacity.catalog(self.models),
                )
            response = selected.client.generate(request)
            self._check(control)
            return response
        finally:
            self.capacity.release(selected)
