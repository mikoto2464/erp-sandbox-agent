from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetCell,
    BudgetSheet,
    BudgetTemplate,
    Issue,
    KnowledgeState,
    OutputSource,
    ProblemSnapshot,
    SolutionBundle,
    WorkbookEntry,
)


def output_sources(
    solution: SolutionBundle, snapshot: ProblemSnapshot | None = None
) -> tuple[OutputSource, ...]:
    result = []
    for index, action in enumerate(solution.plan.actions):
        reference = action.action_id or str(index)
        result.extend(
            (
                OutputSource(
                    f"action:{reference}:amount",
                    action.description + " · 金额",
                    None if action.amount is None else str(action.amount),
                    True,
                    action.payment_period if action.payment_period is not None else action.period,
                    action.amount_unit if action.schema_version == "2" else "题目金额单位",
                ),
                OutputSource(
                    f"action:{reference}:quantity",
                    action.description + " · 数量",
                    None if action.quantity is None else str(action.quantity),
                    True,
                    action.period,
                    action.quantity_unit if action.schema_version == "2" else "题目数量单位",
                ),
                OutputSource(
                    f"action:{reference}:description",
                    action.description + " · 操作说明",
                    action.description,
                    False,
                    action.period,
                    "",
                ),
            )
        )
    for index, item in enumerate(solution.plan.budget_items):
        result.append(
            OutputSource(
                f"budget:{index}",
                item.label,
                item.value,
                _numeric(item.value),
                item.period,
                item.unit,
            )
        )
    if snapshot:
        for fact in snapshot.reading.problem.facts:
            if fact.state == KnowledgeState.KNOWN and fact.value is not None:
                result.append(
                    OutputSource(
                        f"fact:{fact.id}",
                        f"{fact.entity_name} · {fact.key}",
                        fact.value,
                        fact.value_type in {"number", "integer"} and _numeric(fact.value),
                        None,
                        fact.unit,
                    )
                )
    return tuple(result)


def _numeric(value: str | None) -> bool:
    try:
        return value is not None and Decimal(value).is_finite()
    except InvalidOperation:
        return False


def mapped_value(entry: WorkbookEntry, sources: tuple[OutputSource, ...]) -> Decimal | str | None:
    if not entry.source_ids:
        return None
    index = {s.id: s for s in sources}
    values = [index.get(source_id) for source_id in entry.source_ids]
    if any(s is None for s in values):
        raise ERPError("mapping_source", "映射引用了不存在的方案数据。")
    if any(s.value is None for s in values if s):
        return None
    if entry.conversion == "identity":
        if len(values) != 1:
            raise ERPError("mapping_conversion", "不转换时只能选择一个来源。")
        source = values[0]
        assert source is not None and source.value is not None
        return Decimal(source.value) if source.numeric else source.value
    if any(not s.numeric for s in values if s):
        raise ERPError("mapping_conversion", "数值转换或合计必须引用数值条目。")
    numbers = [Decimal(s.value) for s in values if s and s.value is not None]
    if entry.conversion == "sum":
        if len({s.unit for s in values if s}) > 1:
            raise ERPError("mapping_unit", "不同单位的来源不能直接合计，请分别映射。")
        return sum(numbers, Decimal(0))
    if entry.conversion.startswith("sum_"):
        if len({s.unit for s in values if s}) > 1:
            raise ERPError("mapping_unit", "不同单位来源不能合计后转换。")
        conversion = entry.conversion.removeprefix("sum_")
        numbers = [sum(numbers, Decimal(0))]
    else:
        conversion = entry.conversion
    if len(numbers) != 1:
        raise ERPError("mapping_conversion", "单位转换只能选择一个来源。")
    factors = {
        "yuan_to_wan": Decimal("0.0001"),
        "wan_to_yuan": Decimal(10000),
        "ratio_to_percent": Decimal(100),
    }
    if conversion not in factors:
        raise ERPError("mapping_conversion", "不支持的转换方式。")
    return numbers[0] * factors[conversion]


def writable_target(
    sheet: BudgetSheet, address: str, cells: Mapping[str, BudgetCell] | None = None
) -> bool:
    pos = cell_position(address)
    if sheet.hidden or pos is None or pos[0] > sheet.rows or pos[1] > sheet.columns:
        return False
    cell = (
        cells.get(address)
        if cells is not None
        else next((c for c in sheet.cells if c.coordinate == address), None)
    )
    if cell and (cell.formula or not cell.writable or cell.text):
        return False
    if sheet.protected and cell is None:
        return False
    for region in sheet.merged_ranges:
        start, end = region.split(":") if ":" in region else (region, region)
        first, last = cell_position(start), cell_position(end)
        if (
            first
            and last
            and first[0] <= pos[0] <= last[0]
            and first[1] <= pos[1] <= last[1]
            and address != start
        ):
            return False
    return True


def validate_mapping(
    template: BudgetTemplate,
    entries: tuple[WorkbookEntry, ...],
    solution: SolutionBundle,
    snapshot: ProblemSnapshot | None = None,
) -> tuple[Issue, ...]:
    issues = []
    sheets = {s.name: s for s in template.sheets}
    cells = {s.name: {c.coordinate: c for c in s.cells} for s in template.sheets}
    sources = output_sources(solution, snapshot)
    source_index = {s.id: s for s in sources}
    planned = {
        (item.sheet, item.cell): item
        for item in solution.plan.budget_items
        if item.template_id == template.source.id
        and item.sheet in sheets
        and writable_target(sheets[item.sheet], item.cell, cells[item.sheet])
    }
    seen = set()
    unresolved_seen = set()
    if not entries:
        issues.append(Issue("mapping.empty", "error", "entries", "尚无预算表映射条目。"))
    for index, entry in enumerate(entries):
        target = f"entries.{index}"
        sheet = sheets.get(entry.sheet)
        pos = cell_position(entry.cell)
        if not sheet or pos is None or pos[0] > sheet.rows or pos[1] > sheet.columns:
            issues.append(Issue("mapping.target", "error", target, "工作表或单元格不存在或越界。"))
            continue
        if (entry.sheet, entry.cell) in seen:
            issues.append(
                Issue("mapping.duplicate", "error", target, "一个单元格只能对应一个映射条目。")
            )
        seen.add((entry.sheet, entry.cell))
        if planned and (entry.sheet, entry.cell) not in planned:
            issues.append(
                Issue(
                    "mapping.unplanned",
                    "error",
                    target,
                    "此输入项未纳入经营方案，请返回经营方案补充规划。",
                )
            )
        if len(set(entry.source_ids)) != len(entry.source_ids):
            issues.append(
                Issue("mapping.duplicate_source", "error", target, "同一来源不能重复计入合计。")
            )
        if not writable_target(sheet, entry.cell, cells[entry.sheet]) and entry.source_ids:
            issues.append(
                Issue(
                    "mapping.readonly",
                    "error",
                    target,
                    "已有内容、公式、隐藏/保护区域或合并续格不能直接填入。",
                )
            )
        try:
            value = mapped_value(entry, sources)
            item = planned.get((entry.sheet, entry.cell))
            if item and item.value is not None:
                expected = Decimal(item.value) if _numeric(item.value) else item.value
                if item.source_ids:
                    original = WorkbookEntry(
                        item.sheet, item.cell, item.label, item.source_ids, item.conversion
                    )
                    if mapped_value(original, sources) != expected:
                        issues.append(
                            Issue(
                                "mapping.source_mismatch",
                                "error",
                                target,
                                "预算值与绑定业务来源不一致。",
                            )
                        )
                elif any(action.schema_version == "2" for action in solution.plan.actions):
                    issues.append(
                        Issue(
                            "mapping.source_missing",
                            "error",
                            target,
                            "预算值没有绑定业务来源，不能直接填写。",
                        )
                    )
                if value != expected:
                    issues.append(
                        Issue(
                            "mapping.plan_mismatch",
                            "error",
                            target,
                            "填写值与经营方案预算不一致，请引用该方案预算值或先调整方案。",
                        )
                    )
            if value is None and (
                not item or not item.gap_id or item.gap_id not in unresolved_seen
            ):
                if item and item.gap_id:
                    unresolved_seen.add(item.gap_id)
                affected = tuple(
                    f"{template.source.id}:{other.sheet}!{other.cell}"
                    for other in planned.values()
                    if item and item.gap_id and other.gap_id == item.gap_id
                )
                issues.append(
                    Issue(
                        "mapping.unresolved",
                        "warning",
                        target,
                        entry.explanation or "暂无可用方案数据，保留原格。",
                        budget_id=item.gap_id if item else "",
                        impact=affected,
                    )
                )
            periods = {source_index[s].period for s in entry.source_ids if s in source_index}
            if (
                len(periods) > 1
                and entry.conversion != "sum"
                and not entry.conversion.startswith("sum_")
            ):
                issues.append(
                    Issue("mapping.period", "error", target, "跨期间来源须明确采用汇总。")
                )
        except ERPError as exc:
            issues.append(Issue(exc.code, "error", target, exc.message))
    for key, item in planned.items():
        if key not in seen:
            issues.append(
                Issue(
                    "mapping.plan_omitted",
                    "error",
                    "entries",
                    f"遗漏方案预算：{item.sheet} · {item.label}（{item.cell}）。",
                )
            )
    enriched = []
    for issue in issues:
        index = None
        if issue.target.startswith("entries."):
            try:
                index = int(issue.target.split(".")[1])
            except (ValueError, IndexError):
                pass
        entry = entries[index] if index is not None and index < len(entries) else None
        item = planned.get((entry.sheet, entry.cell)) if entry else None
        enriched.append(
            replace(
                issue,
                field_path=issue.target,
                budget_id=(item.gap_id if item else None)
                or f"{template.source.id}:{entry.sheet}:{entry.cell}"
                if entry
                else "",
                action_id=item.action_ids[0] if item and item.action_ids else "",
                source_ids=entry.source_ids if entry else (),
                impact=issue.impact
                or (
                    (f"{template.source.id}:{entry.sheet}!{entry.cell}",)
                    if entry
                    else (issue.target,)
                ),
            )
        )
    return tuple(enriched)
