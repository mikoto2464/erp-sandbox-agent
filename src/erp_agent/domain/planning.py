"""生成阶段冻结的数据；导出只读取这些结果。"""

from dataclasses import dataclass

from erp_agent.domain.budget import BudgetGap
from erp_agent.domain.calculations import CalculationRecord
from erp_agent.domain.reporting import ReportOutline


@dataclass(frozen=True)
class PlanPreparation:
    context_hash: str
    template_hashes: tuple[tuple[str, str], ...]
    calculations: tuple[CalculationRecord, ...]
    outline: ReportOutline
    budget_gaps: tuple[BudgetGap, ...] = ()
    completed_sheets: tuple[tuple[str, str], ...] = ()
    failed_sheets: tuple[tuple[str, str, str], ...] = ()
    snapshot_hash: str = ""
    version: str = "solver-18"
