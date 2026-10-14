import re
from dataclasses import replace

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
    cells = {
        (tid, name): {c.coordinate: c for c in sheet.cells} for (tid, name), sheet in sheets.items()
    }
    issues: list[Issue] = []
    seen: set[tuple[str, str, str]] = set()
    problem = request.snapshot.reading.problem
    horizon = (problem.years or 0) * (problem.periods_per_year or 0)
    unresolved_seen = set()
    for index, item in enumerate(plan.budget_items):
        target = f"budget_items.{index}"
        sheet = sheets.get((item.template_id, item.sheet))
        key = (item.template_id, item.sheet, item.cell)
        position = cell_position(item.cell)
        if not sheet or position is None:
            issues.append(
                Issue("budget.target", "error", target, "预算条目引用未知模板、工作表或单元格。")
            )
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
            issues.append(
                Issue("budget.merged_cell", "error", target, "合并单元格只能引用左上角锚点。")
            )
        elif sheet.hidden or (cell and (cell.formula or not cell.writable or cell.text)):
            issues.append(
                Issue(
                    "budget.readonly",
                    "error",
                    target,
                    "公式、已有内容或隐藏/保护格不能列为预算输入。",
                )
            )
        elif sheet.protected and cell is None:
            issues.append(
                Issue("budget.readonly", "error", target, "受保护区域未经确认不能填入数值。")
            )
        if item.period is not None and item.period > horizon:
            issues.append(Issue("budget.period", "error", target, "预算条目期间超出经营周期。"))
        if any(i < 0 or i >= len(plan.actions) for i in item.action_indices) or not set(
            item.action_ids
        ) <= {a.action_id for a in plan.actions}:
            issues.append(Issue("budget.action", "error", target, "预算条目引用未知操作。"))
        if item.value is not None:
            from decimal import Decimal, InvalidOperation

            from erp_agent.domain.errors import ERPError
            from erp_agent.domain.models import Evaluation, SolutionBundle, WorkbookEntry
            from erp_agent.domain.workbook import mapped_value, output_sources

            if not item.source_ids and any(a.schema_version == "2" for a in plan.actions):
                issues.append(
                    Issue("budget.source_missing", "error", target, "确定预算值必须绑定业务来源。")
                )
            elif item.source_ids:
                solution = SolutionBundle(
                    "validation",
                    request.snapshot.project_id,
                    request.snapshot.version,
                    request.mode,
                    plan,
                    Evaluation(False, False, None, (), "draft"),
                    None,
                    0,
                    0,
                    None,
                    "",
                    "",
                )
                try:
                    resolved = mapped_value(
                        WorkbookEntry(
                            item.sheet, item.cell, item.label, item.source_ids, item.conversion
                        ),
                        output_sources(solution, request.snapshot),
                    )
                    try:
                        expected = Decimal(item.value)
                    except InvalidOperation:
                        expected = item.value
                    if resolved != expected:
                        issues.append(
                            Issue(
                                "budget.source_mismatch",
                                "error",
                                target,
                                "预算值与绑定业务来源不一致。",
                            )
                        )
                except ERPError as error:
                    issues.append(Issue(error.code, "error", target, error.message))
        if item.value is None and item.gap_id and item.gap_id not in unresolved_seen:
            unresolved_seen.add(item.gap_id)
            affected = tuple(
                f"{other.template_id}:{other.sheet}!{other.cell}"
                for other in plan.budget_items
                if other.gap_id == item.gap_id
            )
            issues.append(
                Issue(
                    "budget.unresolved",
                    "warning",
                    target,
                    item.explanation,
                    budget_id=item.gap_id,
                    source_ids=item.source_ids,
                    impact=affected,
                )
            )
        if item.value is None and not item.gap_id:
            issues.append(
                Issue(
                    "budget.unresolved",
                    "warning",
                    target,
                    f"{item.sheet} · {item.label}：{item.explanation or '尚不能确定。'}",
                )
            )
        seen.add(key)
    covered = {tid for tid, _, _ in seen}
    for tid, template in templates.items():
        if tid not in covered:
            issues.append(
                Issue(
                    "budget.template_omitted",
                    "error",
                    tid,
                    f"方案未说明所选预算表 {template.source.filename} 的条目或映射缺口。",
                )
            )
        for sheet in template.sheets:
            if (
                not sheet.hidden
                and any(not c.text and c.writable for c in sheet.cells)
                and not any(t == tid and name == sheet.name for t, name, _ in seen)
            ):
                issues.append(
                    Issue(
                        "budget.sheet_omitted",
                        "error",
                        tid,
                        f"{template.source.filename} · {sheet.name} 未安排预算输入。",
                    )
                )
    enriched = []
    for issue in issues:
        index = None
        if issue.target.startswith("budget_items."):
            try:
                index = int(issue.target.split(".")[1])
            except (ValueError, IndexError):
                pass
        item = (
            plan.budget_items[index]
            if index is not None and index < len(plan.budget_items)
            else None
        )
        enriched.append(
            replace(
                issue,
                field_path=issue.target,
                budget_id=item.gap_id or f"{item.template_id}:{item.sheet}:{item.cell}"
                if item
                else "",
                action_id=item.action_ids[0] if item and item.action_ids else "",
                source_ids=item.source_ids if item else (),
                impact=issue.impact
                or ((f"{item.template_id}:{item.sheet}!{item.cell}",) if item else (issue.target,)),
            )
        )
    return tuple(enriched)
