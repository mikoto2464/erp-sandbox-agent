"""发布生成时已冻结的预算，只检查绑定，不再生成或推理参数。"""

from erp_agent.application.planning_context import context_digest
from erp_agent.application.solution_stages import plan_hash
from erp_agent.application.solver_context import selected_snapshot
from erp_agent.domain.budget import BudgetResult
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import BudgetTemplate, ProblemSnapshot, SolutionBundle


def prepared_budget(
    solution: SolutionBundle, snapshot: ProblemSnapshot, templates: tuple[BudgetTemplate, ...]
) -> BudgetResult:
    preparation = solution.plan.preparation
    hashes = tuple((t.source.id, t.source.sha256) for t in templates)
    if preparation is None or preparation.template_hashes != hashes:
        raise ERPError("planning.binding", "预算模板与生成时的绑定不一致。", 409)
    if snapshot.project_id != solution.project_id or snapshot.version != solution.problem_version:
        raise ERPError("planning.binding", "预算题目与生成方案版本不一致。", 409)
    if preparation.snapshot_hash != context_digest(selected_snapshot(snapshot, templates)):
        raise ERPError("planning.binding", "题目内容与生成时的冻结快照不一致。", 409)
    status = "failed" if preparation.failed_sheets else "succeeded"
    return BudgetResult(
        solution.plan.budget_items,
        preparation.budget_gaps,
        status,
        solution.id,
        solution.problem_version,
        hashes,
        plan_hash(solution.plan),
        preparation.completed_sheets,
        preparation.failed_sheets,
    )
