import hashlib
import threading
from dataclasses import replace
from urllib.parse import urlsplit

from erp_agent.application.contracts import JSONObject, loads, obj
from erp_agent.application.llm_budget import output_token_limit
from erp_agent.application.model_dispatch import (
    DispatchDecision,
    ModelAssignment,
    ModelDispatcher,
    table_dispatch_key,
    task_responsibility,
)
from erp_agent.application.ports import (
    LLMRequest,
    LLMResponse,
    ModelBinding,
    RunControl,
    TaskContext,
)
from erp_agent.application.task_budget import (
    BudgetLLMClient,
    TaskBudgetRegistry,
    TaskCallBudget,
    model_call,
    task_budget,
)
from erp_agent.application.task_reporting import report_task
from erp_agent.application.task_suitability import classify_task
from erp_agent.domain.errors import Cancelled, ERPError


class ModelCapacity:
    """同一服务内共享并发额度，包括主 Agent 的分配请求。"""

    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.used: dict[str, int] = {}
        self.limits: dict[str, int] = {}
        self.endpoints: dict[str, str] = {}
        self.endpoint_limits: dict[str, int] = {}
        self.endpoint_used: dict[str, int] = {}
        self.bindings: dict[str, ModelBinding] = {}

    @staticmethod
    def endpoint(model: ModelBinding) -> str:
        provider = str(model.configuration.get("provider", ""))
        url = str(model.configuration.get("base_url", ""))
        if not provider or not url:
            return "preset:" + model.preset_id
        family = "openai" if provider in {"openai_chat", "openai_responses"} else provider
        parsed = urlsplit(url.rstrip("/"))
        path = parsed.path
        for suffix in ("/chat/completions", "/responses", "/messages", "/api/chat"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        if path.endswith("/v1"):
            path = path[:-3]
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{family}:{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}:{port}{path}"

    def register(self, models: tuple[ModelBinding, ...]) -> None:
        with self.condition:
            for model in models:
                key = self.endpoint(model)
                self.endpoints[model.preset_id] = key
                self.bindings[model.preset_id] = model
            self._endpoint_limits()

    def _endpoint_limits(self) -> None:
        self.endpoint_limits = {}
        for model in self.bindings.values():
            key = self.endpoint(model)
            limit = self.limit(model)
            self.endpoint_limits[key] = min(self.endpoint_limits.get(key, limit), limit)

    def available(self, model: ModelBinding) -> int:
        key = self.endpoint(model)
        return max(
            0,
            min(
                self.limit(model) - self.used.get(model.preset_id, 0),
                self.endpoint_limits.get(key, self.limit(model)) - self.endpoint_used.get(key, 0),
            ),
        )

    def configure(self, limits: dict[str, int]) -> None:
        with self.condition:
            self.limits = limits
            self._endpoint_limits()
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
                    "available": self.available(m),
                    "endpoint_in_use": self.endpoint_used.get(self.endpoint(m), 0),
                    "endpoint_max_parallel": self.endpoint_limits.get(
                        self.endpoint(m), self.limit(m)
                    ),
                    "capabilities": m.configuration.get("capabilities", "未填写"),
                    "task_tier": m.configuration.get("task_tier", "general"),
                    "task_roles": m.configuration.get("task_roles", []),
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
            key = self.endpoint(model)
            self.endpoint_used[key] = self.endpoint_used.get(key, 0) - 1
            self.condition.notify_all()


class ScheduledLLMClient:
    def __init__(
        self,
        models: tuple[ModelBinding, ...],
        capacity: ModelCapacity,
        dispatcher: ModelDispatcher | None = None,
        *,
        strategy: str = "direct",
        role_candidates: dict[str, tuple[str, ...]] | None = None,
        simple_max_chars: int = 32000,
        moderate_max_chars: int = 64000,
        task_max_calls: int = 8,
        task_deadline_seconds: float = 900,
    ) -> None:
        self.models = tuple(sorted(models, key=lambda m: -int(m.configuration["priority"])))
        self.capacity = capacity
        self.capacity.register(self.models)
        self.dispatcher = dispatcher or ModelDispatcher()
        if strategy not in {"direct", "agent"}:
            raise ValueError("模型调度策略必须为 direct 或 agent。")
        self.strategy, self.role_candidates = strategy, role_candidates or {}
        if not 1 <= simple_max_chars < moderate_max_chars:
            raise ValueError("轻量输入阈值必须为正且小于复杂输入阈值。")
        self.simple_max_chars, self.moderate_max_chars = simple_max_chars, moderate_max_chars
        self.task_max_calls, self.task_deadline_seconds = task_max_calls, task_deadline_seconds
        self._budgets = TaskBudgetRegistry(task_max_calls, task_deadline_seconds)
        self._selected: dict[tuple[object, str], str] = {}
        self._failures: dict[tuple[object, str, str], int] = {}
        self._switch_targets: dict[tuple[object, str], str] = {}
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
        budget: TaskCallBudget | None = None,
        load_snapshot: list[JSONObject] | None = None,
    ) -> ModelBinding:
        while True:
            self._check(control)
            if budget:
                budget.check()
            with self.capacity.condition:
                available = [m for m in candidates if self.capacity.available(m) > 0]
                if balance:
                    available.sort(
                        key=lambda m: (
                            self.capacity.used.get(m.preset_id, 0) / self.capacity.limit(m),
                            candidates.index(m),
                        )
                    )
                selected = available[0] if available else None
                if selected:
                    if load_snapshot is not None:
                        load_snapshot.extend(self.capacity.catalog(self.models))
                    self.capacity.used[selected.preset_id] = (
                        self.capacity.used.get(selected.preset_id, 0) + 1
                    )
                    key = self.capacity.endpoint(selected)
                    self.capacity.endpoint_used[key] = self.capacity.endpoint_used.get(key, 0) + 1
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
        budget = (
            task_budget(control, child, self.task_max_calls, self.task_deadline_seconds)
            if control
            else self._budgets.task(child)
        )
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
        acquired = self._acquire((primary,), control, budget=budget)
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
                    request,
                    catalog,
                    BudgetLLMClient(
                        primary.client,
                        task_budget(control, child, self.task_max_calls, self.task_deadline_seconds)
                        if control
                        else self._budgets.task(child),
                    ),
                    int(primary.configuration["max_input_chars"]),
                )
            except Cancelled:
                raise
            except ERPError as error:
                if error.code in {"task_deadline", "task_call_limit"}:
                    raise
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

    def _candidate_review(
        self, request: LLMRequest, sticky_preset: str = ""
    ) -> tuple[tuple[ModelBinding, ...], list[JSONObject]]:
        size = len(request.system) + len(request.user) + len(request.schema_json)
        role = task_responsibility(request)
        role_ids = self.role_candidates.get(role, ())
        suitability = classify_task(request, self.simple_max_chars, self.moderate_max_chars)
        eligible, checks = [], []
        for model in self.models:
            roles = model.configuration.get("task_roles", [])
            reason = ""
            if role_ids and model.preset_id not in role_ids:
                reason = "不在环境配置的职责候选内"
            elif roles and role not in roles:
                reason = "预设未开放当前职责"
            elif suitability.rank(model) is None and model.preset_id != sticky_preset:
                reason = "轻量预设不承接中等或复杂任务"
            elif int(model.configuration["max_input_chars"]) < size:
                reason = "完整输入超过预设字符容量"
            else:
                capacity_check = getattr(model.client, "check_request_capacity", None)
                if capacity_check:
                    try:
                        capacity_check(request)
                    except ERPError as error:
                        if error.code == "llm_input_limit":
                            reason = "完整请求超过模型 Token/输出预留容量"
                        else:
                            raise
            checks.append(
                {
                    "id": model.preset_id,
                    "name": model.preset_name,
                    "task_tier": model.configuration.get("task_tier", "general"),
                    "eligible": not reason,
                    "reason": reason or "任务档位、职责及容量适用",
                }
            )
            if not reason:
                eligible.append(model)
        if self.strategy == "direct":
            # 原列表已按用户优先级稳定排序，适用档位优先于优先级。
            eligible.sort(key=lambda m: 0 if m.preset_id == sticky_preset else suitability.rank(m))
        return tuple(eligible), checks

    @staticmethod
    def _load_checks(checks: list[JSONObject], snapshot: list[JSONObject]) -> list[JSONObject]:
        unavailable = {str(m["id"]) for m in snapshot if not m["available"]}
        return [
            {**check, "reason": "适用，但预设或共享端点额度已占满"}
            if check["eligible"] and check["id"] in unavailable
            else check
            for check in checks
        ]

    @staticmethod
    def _no_candidates(checks: list[JSONObject]) -> ERPError:
        capacity_failure = any("容量" in str(check["reason"]) for check in checks)
        detail = "；".join(f"{c['name']}：{c['reason']}" for c in checks[:6])
        return ERPError(
            "llm_input_limit" if capacity_failure else "llm_task_suitability",
            "没有同时满足任务档位、职责及容量的模型，请检查模型预设。" + detail,
            413 if capacity_failure else 400,
        )

    def _eligible(self, request: LLMRequest) -> tuple[ModelBinding, ...]:
        eligible, checks = self._candidate_review(request)
        if not eligible:
            raise self._no_candidates(checks)
        return eligible

    def _direct_generate(self, request: LLMRequest, control: RunControl | None) -> LLMResponse:
        child, _ = self._task(request)
        budget = (
            task_budget(control, child, self.task_max_calls, self.task_deadline_seconds)
            if control
            else self._budgets.task(child)
        )
        budget.check()
        suitability = classify_task(request, self.simple_max_chars, self.moderate_max_chars)
        owner: object = control if control is not None else self
        key = (owner, child.id)
        try:
            data = obj(loads(request.user))
            repair = any(k in data for k in ("error", "errors", "previous_output", "patch_errors"))
        except ERPError:
            repair = False
        with self._lock:
            previous = self._selected.get(key) or self._selected.get((owner, child.parent_id))
            failures = self._failures.get((owner, child.id, previous or ""), 0)
            switch_target = self._switch_targets.get(key)
        candidates, checks = self._candidate_review(request, previous if repair else "")
        if not candidates:
            if control:
                report_task(
                    control,
                    child,
                    "waiting",
                    difficulty=suitability.difficulty,
                    dispatch_reason=suitability.reason,
                    candidate_checks=checks,
                )
            raise self._no_candidates(checks)
        if previous:
            same = tuple(m for m in candidates if m.preset_id == previous)
            if repair:
                if not same:
                    raise ERPError(
                        "llm_input_limit", "格式修复须沿用原模型，当前修复输入超过该模型容量。", 413
                    )
                candidates = same
            elif failures < 2 or switch_target:
                candidates = same or candidates
            else:
                remaining = tuple(m for m in candidates if m.preset_id != previous)
                candidates = remaining[:1] or same
                if remaining:
                    with self._lock:
                        self._switch_targets[key] = remaining[0].preset_id
        role = task_responsibility(request)
        reason = (
            f"本地规则判定：{suitability.reason}；按 {role} 职责及任务档位优先，"
            "同档位按用户优先级，再检查实际容量和共享额度。"
        )
        if previous and repair:
            reason += " 格式修复沿用原执行模型。"
        elif previous and failures >= 2 and not switch_target:
            reason += " 临时故障已重试，有限切换一次适用候选。"
        elif previous:
            reason += " 当前任务继续沿用已选模型。"
        if control:
            waiting_loads = self.capacity.catalog(self.models)
            report_task(
                control,
                child,
                "waiting",
                candidates=[m.preset_name for m in candidates],
                dispatch_reason=reason,
                difficulty=suitability.difficulty,
                difficulty_source="local",
                model_loads=waiting_loads,
                candidate_checks=self._load_checks(checks, waiting_loads),
                stage="model_execution",
            )
        loads_snapshot: list[JSONObject] = []
        selected = self._acquire(candidates, control, budget=budget, load_snapshot=loads_snapshot)
        checks = self._load_checks(checks, loads_snapshot)
        if selected != candidates[0]:
            reason += " 更优先的适用候选额度占满，使用下一可用候选。"
        tier = selected.configuration.get("task_tier", "general")
        reason += f" 执行预设：{selected.preset_name}（{tier}）。"
        with self._lock:
            self._selected[key] = selected.preset_id
        try:
            self._check(control)
            budget.check()
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
                    model_loads=loads_snapshot,
                    candidate_checks=checks,
                    task_call_limit=budget.max_calls,
                    task_calls=budget.calls + 1,
                )
            with model_call(budget):
                response = selected.client.generate(replace(request, retries=0))
            with self._lock:
                self._failures[(owner, child.id, selected.preset_id)] = 0
            self._check(control)
            budget.check()
            return response
        except ERPError as error:
            if error.code in {
                "llm_gateway_timeout",
                "llm_unavailable",
                "llm_timeout",
                "llm_connection",
            }:
                with self._lock:
                    failure_key = (owner, child.id, selected.preset_id)
                    self._failures[failure_key] = self._failures.get(failure_key, 0) + 1
            raise
        finally:
            # SDK 调用返回或抛错之后才释放；取消标记不能提前归还在途额度。
            self.capacity.release(selected)

    def plan_solver(
        self, requests: tuple[LLMRequest, ...], control: RunControl, concurrency: int
    ) -> None:
        self.plan_reading(requests, control, concurrency, purpose="solver")

    def plan_output(
        self, requests: tuple[LLMRequest, ...], control: RunControl, concurrency: int
    ) -> None:
        self.plan_reading(requests, control, concurrency, purpose="answerer")

    def plan_reading(
        self,
        requests: tuple[LLMRequest, ...],
        control: RunControl,
        concurrency: int,
        purpose: str = "reader",
    ) -> None:
        if self.strategy == "direct":
            for request in requests:
                if request.task:
                    self._budgets.task(request.task)
                    task_budget(
                        control, request.task, self.task_max_calls, self.task_deadline_seconds
                    )
            return
        if len(self.models) <= 1 or not requests:
            return
        if any(request.task is None for request in requests):
            raise ERPError("dispatch_contract", "统筹请求缺少任务标识。")
        eligible_ids = {
            request.task.id: tuple(m.preset_id for m in self._eligible(request))
            for request in requests
        }
        primary = self.models[0]
        solver = purpose == "solver"
        output = purpose == "answerer"
        task = TaskContext(
            purpose + ":dispatch",
            "主 Agent",
            "统一分配输出协作"
            if output
            else "统一分配协作规划"
            if solver
            else "统一分配章节与整表",
            f"{len(requests)} 个{'输出' if output else '规划' if solver else '读题'}任务",
            purpose,
        )
        budgets = tuple(
            task_budget(control, request.task, self.task_max_calls, self.task_deadline_seconds)
            for request in requests
            if request.task
        )
        report_task(
            control,
            task,
            "waiting",
            stage="dispatching_output"
            if output
            else "dispatching_solver"
            if solver
            else "dispatching_reading",
        )
        acquired = self._acquire((primary,), control, budget=budgets[0])
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
                options = {"purpose": purpose} if solver or output else {}
                decisions = self.dispatcher.plan_reading(
                    requests,
                    catalog,
                    BudgetLLMClient(primary.client, budgets[0], budgets),
                    int(primary.configuration["max_input_chars"]),
                    concurrency,
                    eligible_ids=eligible_ids,
                    **options,
                )
            except Cancelled:
                raise
            except ERPError as error:
                if error.code in {"task_deadline", "task_call_limit"}:
                    raise
                reason = (
                    "任务统筹失败，保守回退到可承载输入的最高优先级模型；" + error.message[:200]
                )
                decisions = {}
                for request in requests:
                    model = self._eligible(request)[0]
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
                else "已统一安排文档与填写任务，子角色直接执行。"
                if output
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
                    stage="output_assigned"
                    if output
                    else "planning_assigned"
                    if solver
                    else "reading_assigned",
                )
        finally:
            self.capacity.release(acquired)

    def generate_controlled(self, request: LLMRequest, control: RunControl | None) -> LLMResponse:
        if not self.models:
            raise ERPError("llm_not_configured", "没有可调度的完整模型预设。", 503)
        if self.strategy == "direct":
            return self._direct_generate(request, control)
        child = request.task
        child, default_dispatch = self._task(request)
        owner = control if control is not None else self
        budget = (
            task_budget(control, child, self.task_max_calls, self.task_deadline_seconds)
            if control
            else self._budgets.task(child)
        )
        budget.check()
        with self._lock:
            original = self._selected.get((owner, child.id))
        try:
            repair_data = obj(loads(request.user))
            repair = any(k in repair_data for k in ("error", "errors", "previous_output"))
        except ERPError:
            repair = False
        eligible, checks = self._candidate_review(request, original if repair else "")
        if not eligible:
            raise self._no_candidates(checks)
        with self._lock:
            planned = self._reading_plan.get(child.id) if child else None
            if (
                child
                and not planned
                and (
                    child.parent_id.startswith("template-read:")
                    or child.parent_id.startswith("solver:")
                    or child.parent_id.startswith("mapper:")
                )
            ):
                planned = self._reading_plan.get(child.parent_id)
        if planned:
            solver = planned[1].startswith("solver:")
            output = planned[1].startswith("answerer:")
            dispatch = TaskContext(
                planned[1],
                "主 Agent",
                "统一分配输出协作"
                if output
                else "统一分配协作规划"
                if solver
                else "统一分配章节与整表",
                "输出统筹" if output else "规划统筹" if solver else "读题统筹",
                "answerer" if output else "solver" if solver else "reader",
            )
        else:
            dispatch = default_dispatch
        reused = False
        if repair and original:
            same = next((m for m in eligible if m.preset_id == original), None)
            if same is None:
                raise ERPError("llm_input_limit", "格式修复输入超过原模型容量。", 413)
            reason = "格式修复沿用原任务模型，无额外分配请求。"
            decision = DispatchDecision("unassessed", (ModelAssignment(original, reason),), reason)
        elif planned:
            decision = planned[0]
            eligible_ids = {m.preset_id for m in eligible}
            assignments = tuple(a for a in decision.assignments if a.preset_id in eligible_ids)
            if not assignments:
                raise ERPError("llm_input_limit", "已分配模型无法承载完整任务，请拆小任务。", 413)
            decision = DispatchDecision(
                decision.difficulty, assignments, decision.summary, decision.fallback
            )
        elif len(self.models) > 1 and self.strategy == "agent":
            key = table_dispatch_key(request)
            if key:
                key += ":" + ",".join(m.preset_id for m in eligible)
            cache_owner = False
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
                            cache_owner = True
                            break
                    pending.wait(0.2)
            if not reused:
                try:
                    decision = self._dispatch(request, eligible, control, child, dispatch)
                    if (
                        cache_owner
                        and decision.reuse_scope == "same_table"
                        and not decision.fallback
                    ):
                        with self._lock:
                            self._dispatch_cache[key] = (decision, 1)
                finally:
                    if cache_owner:
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
        selected = self._acquire(candidates, control, balance=bool(planned), budget=budget)
        with self._lock:
            self._selected[(owner, child.id)] = selected.preset_id
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
            with model_call(budget):
                response = selected.client.generate(replace(request, retries=0))
            self._check(control)
            budget.check()
            return response
        finally:
            self.capacity.release(selected)
