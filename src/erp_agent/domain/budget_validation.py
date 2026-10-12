import re

from erp_agent.domain.models import Issue, Plan, SolveRequest


def cell_position(address: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", address)
    if match is None:
        return None
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - ord("A") + 1
    return int(match[2]), column


def validate_budget_items(request: SolveRequest, plan: Plan) -> tuple[Issue, ...]:
    templates = {t.source.id: t for t in request.context.templates}
    sheets = {(t.source.id, s.name): s for t in templates.values() for s in t.sheets}
    cells = {(tid, name): {c.coordinate: c for c in sheet.cells}
             for (tid, name), sheet in sheets.items()}
    issues: list[Issue] = []
    seen: set[tuple[str, str, str]] = set()
    problem = request.snapshot.reading.problem
    horizon = (problem.years or 0) * (problem.periods_per_year or 0)
    for index, item in enumerate(plan.budget_items):
        target = f"budget_items.{index}"
        sheet = sheets.get((item.template_id, item.sheet))
        key = (item.template_id, item.sheet, item.cell)
        position = cell_position(item.cell)
        if not sheet or position is None:
            issues.append(Issue("budget.target", "error", target, "预算条目引用未知模板、工作表或单元格。"))
            continue
        row, column = position
        cell = cells[(item.template_id, item.sheet)].get(item.cell)
        merged_continuation = False
        for region in sheet.merged_ranges:
            start, end = region.split(":") if ":" in region else (region, region)
            first, last = cell_position(start), cell_position(end)
            if first is None or last is None:
                continue
            if first[0] <= row <= last[0] and first[1] <= column <= last[1] and item.cell != start:
                merged_continuation = True
                break
        if row > sheet.rows or column > sheet.columns or key in seen:
            issues.append(Issue("budget.target", "error", target, "预算条目位置越界或重复。"))
        elif merged_continuation:
            issues.append(Issue("budget.merged_cell", "error", target, "合并单元格只能引用左上角锚点。"))
        elif cell and (cell.formula or not cell.writable) and item.value is not None:
            issues.append(Issue("budget.readonly", "error", target, "公式或保护单元格不能作为待填数值。"))
        elif sheet.protected and cell is None and item.value is not None:
            issues.append(Issue("budget.readonly", "error", target, "受保护区域未经确认不能填入数值。"))
        if item.period is not None and item.period > horizon:
            issues.append(Issue("budget.period", "error", target, "预算条目期间超出经营周期。"))
        if any(i >= len(plan.actions) for i in item.action_indices):
            issues.append(Issue("budget.action", "error", target, "预算条目引用未知操作。"))
        if item.value is None:
            issues.append(Issue("budget.unresolved", "warning", target,
                                f"{item.sheet} · {item.label}：{item.explanation or '尚不能确定。'}"))
        seen.add(key)
    covered = {tid for tid, _, _ in seen}
    for tid, template in templates.items():
        if tid not in covered:
            issues.append(Issue("budget.template_omitted", "error", tid,
                                f"方案未说明所选预算表 {template.source.filename} 的条目或映射缺口。"))
    return tuple(issues)
