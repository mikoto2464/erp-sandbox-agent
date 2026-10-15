from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from erp_agent.domain.models import BudgetItem


@dataclass(frozen=True)
class BudgetTarget:
    template_id: str
    sheet: str
    cell: str


@dataclass(frozen=True)
class BudgetGap:
    id: str
    code: str
    message: str
    targets: tuple[BudgetTarget, ...]
    source_ids: tuple[str, ...] = ()
    rule_id: str = ""


@dataclass(frozen=True)
class BudgetResult:
    items: tuple[BudgetItem, ...]
    gaps: tuple[BudgetGap, ...]
    status: str
    solution_id: str
    problem_version: int
    template_hashes: tuple[tuple[str, str], ...]
    plan_hash: str
    completed_sheets: tuple[tuple[str, str], ...] = ()
    failed_sheets: tuple[tuple[str, str, str], ...] = ()
