"""任务级调用与时间边界，重试、修复和拆分共享。"""

import threading
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar

from erp_agent.application.ports import RunControl, TaskContext
from erp_agent.domain.errors import ERPError


class TaskCallBudget:
    def __init__(
        self,
        max_calls: int = 8,
        deadline_seconds: float = 900,
        *,
        parent: "TaskCallBudget | None" = None,
    ) -> None:
        self.max_calls = max_calls
        self.started_at = time.monotonic()
        self.deadline = self.started_at + deadline_seconds
        self.calls = 0
        self.lock = threading.Lock()
        self.parent = parent
        if parent:
            self.deadline = min(self.deadline, parent.deadline)

    def check(self) -> None:
        if self.parent:
            self.parent.check()
        if time.monotonic() >= self.deadline:
            raise ERPError(
                "task_deadline", "任务已达截止时间，已完成结果保留；请重试未完成阶段。", 408
            )

    def reserve(self, *, charged: set["TaskCallBudget"] | None = None) -> None:
        charged = charged if charged is not None else set()
        if self in charged:
            return
        with self.lock:
            self.check()
            if self.calls >= self.max_calls:
                message = "任务已达模型调用上限，停止重试、修复和拆分。"
                if self.parent:
                    message = (
                        f"当前子任务已达预分配模型调用上限（{self.calls}/{self.max_calls}）；"
                        f"求解总额度已用 {self.parent.calls}/{self.parent.max_calls}。"
                        "其它任务的预留额度不能被本任务占用。"
                    )
                raise ERPError("task_call_limit", message, 429)
            if self.parent:
                self.parent.reserve(charged=charged)
            self.calls += 1
            charged.add(self)

    @property
    def remaining_seconds(self) -> float:
        self.check()
        return self.deadline - time.monotonic()


class TaskBudgetRegistry:
    def __init__(self, max_calls: int = 8, deadline_seconds: float = 900) -> None:
        self.max_calls, self.deadline_seconds = max_calls, deadline_seconds
        self.tasks: dict[str, TaskCallBudget] = {}
        self.lock = threading.RLock()

    def task(self, task: TaskContext) -> TaskCallBudget:
        with self.lock:
            budget = self.tasks.get(task.id)
            if budget is None:
                budget = self.tasks.get(task.parent_id) or TaskCallBudget(
                    self.max_calls, self.deadline_seconds
                )
                self.tasks[task.id] = budget
            return budget


class BudgetLLMClient:
    def __init__(
        self, client, budget: TaskCallBudget, affected: tuple[TaskCallBudget, ...] = ()
    ) -> None:
        self.client, self.budget, self.affected = client, budget, affected

    @property
    def configured(self) -> bool:
        return self.client.configured

    def generate(self, request):
        for budget in dict.fromkeys(self.affected):
            if budget is not self.budget:
                budget.reserve()
        with model_call(self.budget):
            return self.client.generate(request)


_registries: weakref.WeakKeyDictionary[object, TaskBudgetRegistry] = weakref.WeakKeyDictionary()
_lock = threading.RLock()
_chain: ContextVar[tuple[TaskCallBudget, ...]] = ContextVar("erp_budget_chain", default=())
_control: ContextVar[RunControl | None] = ContextVar("erp_run_control", default=None)


def current_run_control() -> RunControl | None:
    return _control.get()


@contextmanager
def run_control(control: RunControl):
    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


def remaining_model_calls() -> int | None:
    budgets = list(dict.fromkeys(_chain.get()))
    for budget in budgets:
        if budget.parent is not None and budget.parent not in budgets:
            budgets.append(budget.parent)
    if not budgets:
        return None
    remaining = []
    for budget in budgets:
        with budget.lock:
            budget.check()
            remaining.append(max(0, budget.max_calls - budget.calls))
    return min(remaining)


def followup_model_call() -> None:
    charged: set[TaskCallBudget] = set()
    # 先检查子额度；不因子任务已耗尽而额外扣减总额度。
    for budget in reversed(tuple(dict.fromkeys(_chain.get()))):
        budget.reserve(charged=charged)


_active: ContextVar[TaskCallBudget | None] = ContextVar("erp_task_call_budget", default=None)


def current_task_budget() -> TaskCallBudget | None:
    return _active.get()


def task_budget(
    control: RunControl,
    task: TaskContext,
    max_calls: int | None = None,
    deadline_seconds: float | None = None,
) -> TaskCallBudget:
    with _lock:
        registry = _registries.get(control)
        if registry is None:
            registry = TaskBudgetRegistry(max_calls or 8, deadline_seconds or 900)
            _registries[control] = registry
        if max_calls is not None:
            registry.max_calls = max_calls
        if deadline_seconds is not None:
            registry.deadline_seconds = deadline_seconds
        budget = registry.task(task)
        if budget.calls == 0 and budget.parent is None:
            if max_calls is not None:
                budget.max_calls = max_calls
            if deadline_seconds is not None:
                budget.deadline = budget.started_at + deadline_seconds
    return budget


def allocate_child_budgets(
    control: RunControl,
    allocations: dict[str, int],
    max_calls: int,
    deadline_seconds: float,
) -> dict[str, TaskCallBudget]:
    root = task_budget(control, TaskContext("solver", "Solver", "经营方案生成", ""))
    with _lock:
        registry = _registries[control]
        if any(registry.tasks.get(key, root).parent is root for key in allocations):
            if any(key not in registry.tasks for key in allocations):
                raise ERPError(
                    "planning.budget_scope", "规划任务范围已变化，请在新的求解任务内重新分配额度。"
                )
            return {key: registry.tasks[key] for key in allocations}
        root.max_calls = max_calls
        root.deadline = root.started_at + deadline_seconds
        result = {}
        for key, maximum in allocations.items():
            child = TaskCallBudget(maximum, deadline_seconds, parent=root)
            registry.tasks[key] = child
            result[key] = child
        return result


def exclude_human_wait(control: RunControl, seconds: float) -> None:
    """人工等待不占模型执行时限；调用次数和已消耗执行时间保持。"""
    with _lock:
        registry = _registries.get(control)
        if registry:
            for budget in set(registry.tasks.values()):
                with budget.lock:
                    budget.deadline += max(0, seconds)


@contextmanager
def model_call(budget: TaskCallBudget):
    nested = _active.get() is budget
    if not nested:
        budget.reserve(charged=set(_chain.get()))
    token = _active.set(budget)
    chain_token = _chain.set(_chain.get() + (budget,))
    try:
        yield budget
    finally:
        _chain.reset(chain_token)
        _active.reset(token)
