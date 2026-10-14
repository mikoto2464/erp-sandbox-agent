"""任务级调用与时间边界，重试、修复和拆分共享。"""

import threading
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar

from erp_agent.application.ports import RunControl, TaskContext
from erp_agent.domain.errors import ERPError


class TaskCallBudget:
    def __init__(self, max_calls: int = 8, deadline_seconds: float = 900) -> None:
        self.max_calls = max_calls
        self.started_at = time.monotonic()
        self.deadline = self.started_at + deadline_seconds
        self.calls = 0
        self.lock = threading.Lock()

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ERPError(
                "task_deadline", "任务已达截止时间，已完成结果保留；请重试未完成阶段。", 408
            )

    def reserve(self) -> None:
        with self.lock:
            self.check()
            if self.calls >= self.max_calls:
                raise ERPError(
                    "task_call_limit", "任务已达模型调用上限，停止重试、修复和拆分。", 429
                )
            self.calls += 1

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
        if budget.calls == 0:
            if max_calls is not None:
                budget.max_calls = max_calls
            if deadline_seconds is not None:
                budget.deadline = budget.started_at + deadline_seconds
    return budget


@contextmanager
def model_call(budget: TaskCallBudget):
    nested = _active.get() is budget
    if not nested:
        budget.reserve()
    token = _active.set(budget)
    try:
        yield budget
    finally:
        _active.reset(token)
