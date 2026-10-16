"""规划调用额度提前分配，父额度计总账，各分段无法挤占彼此额度。"""

from dataclasses import dataclass

from erp_agent.application.ports import LLMClient, RunControl
from erp_agent.application.task_budget import TaskCallBudget, allocate_child_budgets
from erp_agent.domain.errors import ERPError


@dataclass(frozen=True)
class PlanningCallLimits:
    task_calls: int = 8
    workflow_calls: int = 32
    deadline_seconds: float = 900


def planning_call_limits(client: LLMClient) -> PlanningCallLimits:
    for _ in range(8):
        if hasattr(client, "task_max_calls"):
            return PlanningCallLimits(
                client.task_max_calls,
                getattr(client, "solver_max_calls", 32),
                client.task_deadline_seconds,
            )
        settings = getattr(client, "settings", None)
        if settings is not None:
            return PlanningCallLimits(
                settings.task_max_model_calls,
                settings.solver_max_model_calls,
                settings.task_deadline_seconds,
            )
        nested = getattr(client, "client", None)
        if nested is None:
            break
        client = nested
    return PlanningCallLimits()


def allocate_planning_budgets(
    control: RunControl,
    limits: PlanningCallLimits,
    window_count: int,
    budget_calls: int,
) -> dict[str, TaskCallBudget]:
    ids = ["solver:operations", "solver:finance"]
    if window_count > 1:
        ids += ["solver:synthesis:route"] + [
            f"solver:synthesis:window:{i + 1}" for i in range(window_count)
        ]
    else:
        ids += ["solver:synthesis"]
    if budget_calls:
        ids += ["solver-budget"]
    # 每个规划任务至少留最终提交；预算阶段按工作表数量预留，受单任务上限约束。
    allocations = {key: 1 for key in ids}
    if budget_calls:
        allocations["solver-budget"] = min(budget_calls, limits.task_calls)
    minimum = sum(allocations.values())
    if minimum > limits.workflow_calls:
        raise ERPError(
            "planning.call_configuration",
            f"当前分段与预算至少需要 {minimum} 次模型调用，求解总上限为 "
            f"{limits.workflow_calls}；请调整 ERP_SOLVER_MAX_MODEL_CALLS。",
            429,
        )
    remaining = limits.workflow_calls - minimum
    while remaining:
        eligible = [key for key in ids if allocations[key] < limits.task_calls]
        if not eligible:
            break
        key = min(eligible, key=lambda key: allocations[key])
        allocations[key] += 1
        remaining -= 1
    return allocate_child_budgets(
        control, allocations, limits.workflow_calls, limits.deadline_seconds
    )
