import hashlib
import threading

from erp_agent.application.contracts import JSONObject, loads, obj
from erp_agent.application.llm_budget import output_token_limit
from erp_agent.application.model_dispatch import (
    DispatchDecision,
    ModelAssignment,
    ModelDispatcher,
    table_dispatch_key,
)
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
                    "thinking_mode": m.configuration.get("thinking_mode", "default"),
                    "thinking_budget": m.configuration.get("thinking_budget", 8192),
                    "provider": m.configuration.get("provider", ""),
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
        self._dispatch_cache: dict[str, tuple[DispatchDecision, int]] = {}
        self._dispatch_pending: dict[str, threading.Event] = {}
        self._reading_plan: dict[str, tuple[DispatchDecision, str]] = {}

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
        self,
        candidates: tuple[ModelBinding, ...],
        control: RunControl | None,
        balance: bool = False,
    ) -> ModelBinding:
        while True:
            self._check(control)
            with self.capacity.condition:
                available = [
                    m
                    for m in candidates
                    if self.capacity.used.get(m.preset_id, 0) < self.capacity.limit(m)
                ]
                if balance:
                    available.sort(
                        key=lambda m: (
                            self.capacity.used.get(m.preset_id, 0) / self.capacity.limit(m),
                            candidates.index(m),
                        )
                    )
                selected = available[0] if available else None
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

    def plan_solver(
        self, requests: tuple[LLMRequest, ...], control: RunControl, concurrency: int
    ) -> None:
        self.plan_reading(requests, control, concurrency, purpose="solver")

    def plan_reading(
        self,
        requests: tuple[LLMRequest, ...],
        control: RunControl,
        concurrency: int,
        purpose: str = "reader",
    ) -> None:
        if len(self.models) <= 1 or not requests:
            return
        if any(request.task is None for request in requests):
            raise ERPError("dispatch_contract", "统筹请求缺少任务标识。")
        primary = self.models[0]
        solver = purpose == "solver"
        task = TaskContext(
            purpose + ":dispatch",
            "主 Agent",
            "统一分配协作规划" if solver else "统一分配章节与整表",
            f"{len(requests)} 个{'规划' if solver else '读题'}任务",
            purpose,
        )
        report_task(
            control,
            task,
            "waiting",
            stage="dispatching_solver" if solver else "dispatching_reading",
        )
        acquired = self._acquire((primary,), control)
        try:
            self._check(control)
            catalog = self.capacity.catalog(self.models)
            for model in catalog:
                model["available_after_dispatch"] = int(model["available"]) + (
                    1 if model["id"] == primary.preset_id else 0
                )
            report_task(
                control,
                task,
                "running",
                model=primary.configuration["model"],
                preset_name=primary.preset_name,
                model_loads=catalog,
            )
            try:
                options = {"purpose": purpose} if solver else {}
                decisions = self.dispatcher.plan_reading(
                    requests,
                    catalog,
                    primary.client,
                    int(primary.configuration["max_input_chars"]),
                    concurrency,
                    **options,
                )
            except Cancelled:
                raise
            except ERPError as error:
                reason = (
                    "章节统筹失败，保守回退到可承载输入的最高优先级模型；" + error.message[:200]
                )
                decisions = {}
                for request in requests:
                    size = len(request.system) + len(request.user) + len(request.schema_json)
                    model = next(
                        (m for m in self.models if int(m.configuration["max_input_chars"]) >= size),
                        None,
                    )
                    if model is None:
                        raise ERPError(
                            "llm_input_limit", "完整读题任务超过所有模型输入上限。", 413
                        ) from error
                    decisions[request.task.id] = DispatchDecision(
                        "unassessed",
                        (ModelAssignment(model.preset_id, reason),),
                        reason,
                        fallback=True,
                    )
            self._check(control)
            with self._lock:
                self._reading_plan.update(
                    {key: (decision, task.id) for key, decision in decisions.items()}
                )
            failed = any(d.fallback for d in decisions.values())
            report_task(
                control,
                task,
                "failed" if failed else "succeeded",
                dispatch_fallback=failed,
                planned_count=len(decisions),
                dispatch_reason=next(iter(decisions.values())).summary
                if failed
                else "已统一安排协作规划，子任务直接执行。"
                if solver
                else "已统一安排章节与整表，子任务直接执行。",
            )
            for request in requests:
                decision = decisions[request.task.id]
                report_task(
                    control,
                    request.task,
                    "queued",
                    difficulty=decision.difficulty,
                    dispatch_reason=decision.summary,
                    coordinator=task.id,
                    dispatch_fallback=decision.fallback,
                    dispatch_planned=True,
                    candidates=[
                        next(m.preset_name for m in self.models if m.preset_id == a.preset_id)
                        for a in decision.assignments
                    ],
                    stage="planning_assigned" if solver else "reading_assigned",
                )
        finally:
            self.capacity.release(acquired)

    def generate_controlled(self, request: LLMRequest, control: RunControl | None) -> LLMResponse:
        if not self.models:
            raise ERPError("llm_not_configured", "没有可调度的完整模型预设。", 503)
        size = len(request.system) + len(request.user) + len(request.schema_json)
        eligible = tuple(m for m in self.models if int(m.configuration["max_input_chars"]) >= size)
        if not eligible:
            raise ERPError("llm_input_limit", "任务输入超过所有协作模型的上限，请缩小任务。", 413)
        child = request.task
        with self._lock:
            planned = self._reading_plan.get(child.id) if child else None
            if (
                child
                and not planned
                and (
                    child.parent_id.startswith("template-read:")
                    or child.parent_id.startswith("solver:")
                )
            ):
                planned = self._reading_plan.get(child.parent_id)
        if planned:
            try:
                data = obj(loads(request.user))
                if any(k in data for k in ("error", "errors", "previous_output")):
                    planned = None
            except ERPError:
                planned = None
        if planned:
            solver = planned[1].startswith("solver:")
            dispatch = TaskContext(
                planned[1],
                "主 Agent",
                "统一分配协作规划" if solver else "统一分配章节与整表",
                "规划统筹" if solver else "读题统筹",
                "solver" if solver else "reader",
            )
        else:
            child, dispatch = self._task(request)
        reused = False
        if planned:
            decision = planned[0]
            eligible_ids = {m.preset_id for m in eligible}
            assignments = tuple(a for a in decision.assignments if a.preset_id in eligible_ids)
            if not assignments:
                raise ERPError("llm_input_limit", "已分配模型无法承载完整任务，请拆小任务。", 413)
            decision = DispatchDecision(
                decision.difficulty, assignments, decision.summary, decision.fallback
            )
        elif len(self.models) > 1:
            key = table_dispatch_key(request)
            if key:
                key += ":" + ",".join(m.preset_id for m in eligible)
            owner = False
            if key:
                while True:
                    self._check(control)
                    with self._lock:
                        cached = self._dispatch_cache.get(key)
                        if cached and cached[1] < 8:
                            decision = cached[0]
                            self._dispatch_cache[key] = (decision, cached[1] + 1)
                            reused = True
                            break
                        pending = self._dispatch_pending.get(key)
                        if pending is None:
                            self._dispatch_pending[key] = threading.Event()
                            owner = True
                            break
                    pending.wait(0.2)
            if not reused:
                try:
                    decision = self._dispatch(request, eligible, control, child, dispatch)
                    if owner and decision.reuse_scope == "same_table" and not decision.fallback:
                        with self._lock:
                            self._dispatch_cache[key] = (decision, 1)
                finally:
                    if owner:
                        with self._lock:
                            self._dispatch_pending.pop(key).set()
            elif control:
                report_task(
                    control,
                    dispatch,
                    "cached",
                    difficulty=decision.difficulty,
                    dispatch_reason="复用主 Agent 对同表普通数据行的授权分配；额度仍实时检查。",
                )
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
                dispatch_reused=reused,
                dispatch_planned=bool(planned),
                coordinator=planned[1]
                if planned
                else dispatch.id
                if len(self.models) > 1
                else None,
                stage="model_execution",
            )
        selected = self._acquire(candidates, control, balance=bool(planned))
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
                    input_chars=len(request.system) + len(request.user) + len(request.schema_json),
                    output_limit=output_token_limit(
                        request,
                        int(selected.configuration.get("max_output_tokens") or 12000),
                        str(selected.configuration.get("thinking_mode", "default")),
                        str(selected.configuration.get("provider", "")),
                        int(selected.configuration.get("thinking_budget") or 8192),
                    ),
                    stage="model_execution",
                    model_loads=self.capacity.catalog(self.models),
                )
            response = selected.client.generate(request)
            self._check(control)
            return response
        finally:
            self.capacity.release(selected)
