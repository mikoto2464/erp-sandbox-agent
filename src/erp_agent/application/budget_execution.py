import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal

from erp_agent.application.contracts import array, integer, obj, string, strings
from erp_agent.application.mapping_sources import compatible_conversion, target_kind
from erp_agent.domain.budget import BudgetGap, BudgetTarget
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetItem,
    BudgetSheet,
    BudgetTemplate,
    OutputSource,
    Plan,
    WorkbookEntry,
)
from erp_agent.domain.workbook import mapped_value, writable_target


@dataclass(frozen=True)
class CheckedInput:
    id: str
    label: str
    range: str
    unit: str
    description: str
    depends_on_sheets: tuple[str, ...] = ()


DERIVED_SHEET_MARKERS = ("利润", "损益", "资产负债", "现金流量", "权益变动")
DERIVED_LABEL_MARKERS = ("余额", "利润", "权益", "得分", "期末现金", "期初现金", "现金结余")


def derived_statement_target(sheet_name: str, label: str) -> bool:
    """派生报表（利润表/资产负债表/现金流量表）或需确定性核算的汇总格只留空。"""
    return any(marker in sheet_name for marker in DERIVED_SHEET_MARKERS) or any(
        marker in label for marker in DERIVED_LABEL_MARKERS
    )


def addresses(region: str) -> tuple[str, ...]:
    first, _, last = region.partition(":")
    lo, hi = cell_position(first), cell_position(last or first)
    if not lo or not hi or lo[0] > hi[0] or lo[1] > hi[1]:
        raise ERPError("budget.range", "输入范围无效。")
    if (hi[0] - lo[0] + 1) * (hi[1] - lo[1] + 1) > 10000:
        raise ERPError("budget.range", "输入范围超出安全容量。")
    result = []
    for row in range(lo[0], hi[0] + 1):
        for column in range(lo[1], hi[1] + 1):
            label, n = "", column
            while n:
                n, remainder = divmod(n - 1, 26)
                label = chr(65 + remainder) + label
            result.append(label + str(row))
    return tuple(result)


def expand_mappings(
    data,
    template: BudgetTemplate,
    sheet: BudgetSheet,
    groups: tuple[CheckedInput, ...],
    sources: tuple[OutputSource, ...],
    plan: Plan,
    horizon: int,
):
    obj(data, {"status", "mappings", "gaps"})
    status = string(data["status"])
    if status not in {"applicable", "not_applicable", "missing_input"}:
        raise ERPError("budget.status", "预算任务状态无效。")
    declared = {}
    for raw in array(data["gaps"]):
        gap = obj(raw, {"id", "code", "message", "source_ids", "rule_id"})
        identifier = string(gap["id"])
        if identifier in declared or not string(gap["message"]).strip():
            raise ERPError("budget.gap", "预算缺口编号重复或说明为空。")
        declared[identifier] = gap
    index = {s.id: s for s in sources}
    group_index = {g.id: g for g in groups}
    cell_index = {c.coordinate: c for c in sheet.cells}
    items, seen, targets = [], set(), {}
    for mapping_index, raw in enumerate(array(data["mappings"])):
        mapping = obj(
            raw,
            {
                "group_id",
                "target_range",
                "source_ids",
                "period_axis",
                "periods",
                "conversion",
                "calculation",
                "gap_id",
            },
        )
        group = group_index.get(string(mapping["group_id"]))
        if group is None:
            raise ERPError("budget.group", f"mappings.{mapping_index}.group_id 引用未知输入组。")
        region = string(mapping["target_range"])
        cells = addresses(region)
        allowed = set(addresses(group.range))
        if not set(cells) <= allowed:
            raise ERPError(
                "budget.scope", f"mappings.{mapping_index}.target_range 超出已核对输入组。"
            )
        references = strings(mapping["source_ids"])
        if len(set(references)) != len(references) or not set(references) <= set(index):
            raise ERPError(
                "budget.source", f"mappings.{mapping_index}.source_ids 引用未知或重复来源。"
            )
        axis = string(mapping["period_axis"])
        periods = tuple(
            None if p is None else integer(p, 1, horizon) for p in array(mapping["periods"])
        )
        row_count = len({cell_position(c)[0] for c in cells})
        column_count = len({cell_position(c)[1] for c in cells})
        if axis not in {"single", "rows", "columns"} or len(periods) != {
            "single": 1,
            "rows": row_count,
            "columns": column_count,
        }.get(axis):
            raise ERPError("budget.period", f"mappings.{mapping_index}.periods 与范围维度不符。")
        conversion, calculation = string(mapping["conversion"]), string(mapping["calculation"])
        gap_id = None if mapping["gap_id"] is None else string(mapping["gap_id"])
        if gap_id and gap_id not in declared:
            raise ERPError("budget.gap", "映射引用了未声明缺口。")
        if calculation not in {"direct", "sum"}:
            gap_id = gap_id or "unimplemented:" + calculation
            declared.setdefault(
                gap_id,
                {
                    "id": gap_id,
                    "code": "budget.rule_unimplemented",
                    "message": f"计算规则 {calculation} 尚未实现，保留空白。",
                    "source_ids": list(references),
                    "rule_id": calculation,
                },
            )
        if not references and not gap_id:
            raise ERPError(
                "budget.source", "无业务来源的输入必须关联具体缺口，不能填零或模型猜值。"
            )
        if gap_id and not set(strings(declared[gap_id]["source_ids"])) <= set(index):
            raise ERPError("budget.gap_source", "缺口引用未知来源。")
        if gap_id:
            explanation = string(declared[gap_id]["message"])
        else:
            explanation = "程序从已绑定业务来源展开；经营数值仍未经过完整仿真。"
        first_position = cell_position(cells[0])
        mapping_gap = gap_id
        for cell in cells:
            gap_id = mapping_gap
            if not writable_target(sheet, cell, cell_index):
                continue
            if cell in seen:
                raise ERPError("budget.duplicate", f"{sheet.name}!{cell} 重复映射。")
            seen.add(cell)
            position = cell_position(cell)
            period = periods[
                0
                if axis == "single"
                else position[0] - first_position[0]
                if axis == "rows"
                else position[1] - first_position[1]
            ]
            refs = tuple(
                r
                for r in references
                if (period is None and calculation == "sum")
                or index[r].period is None
                or index[r].period == period
            )
            value = None
            if (
                not gap_id
                and period is None
                and calculation == "direct"
                and any(index[r].period is not None for r in references)
            ):
                gap_id = "period-unknown:" + group.id
                declared.setdefault(
                    gap_id,
                    {
                        "id": gap_id,
                        "code": "budget.period_unknown",
                        "message": "输入期间尚未确定，不能按位置猜测，保留空白。",
                        "source_ids": list(references),
                        "rule_id": "",
                    },
                )
                explanation = declared[gap_id]["message"]
            if not gap_id and derived_statement_target(sheet.name, group.label):
                gap_id = "derived-unimplemented:" + group.label
                declared.setdefault(
                    gap_id,
                    {
                        "id": gap_id,
                        "code": "budget.derived_statement",
                        "message": "该格属于派生报表或需确定性核算规则"
                        "（利润/资产负债/现金流量/期末余额等），预算阶段只填写输入参数，保留空白。",
                        "source_ids": list(refs),
                        "rule_id": group.label,
                    },
                )
                explanation = declared[gap_id]["message"]
            if not gap_id and any(
                index[r].numeric and not index[r].unit and group.unit for r in refs
            ):
                gap_id = "unit-unknown:" + "|".join(refs)
                declared.setdefault(
                    gap_id,
                    {
                        "id": gap_id,
                        "code": "budget.unit_unknown",
                        "message": "来源单位尚未明确，不能猜测换算倍率，保留空白。",
                        "source_ids": list(refs),
                        "rule_id": "",
                    },
                )
                explanation = declared[gap_id]["message"]
            if not gap_id:
                selected_actions = {a.action_id: a for a in plan.actions}
                for reference in refs:
                    if not reference.startswith("action:") or not reference.endswith(":amount"):
                        continue
                    action_id = reference.rsplit(":", 1)[0].removeprefix("action:")
                    action = selected_actions[action_id]
                    if action.schema_version == "2" and (
                        action.amount_meaning in {"unknown", "none", "reference"}
                        or action.amount_meaning in {"payment", "receipt"}
                        and action.payment_period is None
                    ):
                        gap_id = "action-unresolved:" + action_id
                        declared.setdefault(
                            gap_id,
                            {
                                "id": gap_id,
                                "code": "budget.action_semantics",
                                "message": "行动金额含义或支付/收款期间尚未确定，保留空白。",
                                "source_ids": [reference],
                                "rule_id": "",
                            },
                        )
                        explanation = declared[gap_id]["message"]
                        break
            if not gap_id:
                if not refs:
                    raise ERPError(
                        "budget.period_source", f"{sheet.name}!{cell} 没有对应期间的来源。"
                    )
                kind = target_kind(
                    BudgetItem(
                        template.source.id,
                        sheet.name,
                        cell,
                        group.label,
                        period,
                        group.unit,
                        None,
                        (),
                        "",
                    ),
                    cell_index[cell].number_format if cell in cell_index else "",
                )
                if any(index[r].numeric != (kind != "text") for r in refs):
                    raise ERPError("budget.type", f"{sheet.name}!{cell} 来源类型与输入类型不符。")
                if calculation == "sum":
                    conversion = "sum" if conversion in {"identity", "sum"} else "sum_" + conversion
                if any(
                    s.numeric and not compatible_conversion(s.unit, group.unit, conversion)
                    for s in (index[r] for r in refs)
                ):
                    raise ERPError(
                        "budget.unit", f"{sheet.name}!{cell} 来源单位与已核对输入单位不匹配。"
                    )
                resolved = mapped_value(
                    WorkbookEntry(sheet.name, cell, group.label, refs, conversion), sources
                )
                if resolved is None:
                    gap_id = "missing:" + hashlib.sha256("|".join(refs).encode()).hexdigest()[:16]
                    declared.setdefault(
                        gap_id,
                        {
                            "id": gap_id,
                            "code": "budget.source_missing",
                            "message": "已绑定来源值未知，保留空白。",
                            "source_ids": list(refs),
                            "rule_id": "",
                        },
                    )
                    explanation = declared[gap_id]["message"]
                else:
                    value = str(resolved) if isinstance(resolved, Decimal) else resolved
            action_ids = tuple(
                a.action_id
                for a in plan.actions
                if any(r.startswith(f"action:{a.action_id}:") for r in refs)
            )
            items.append(
                BudgetItem(
                    template.source.id,
                    sheet.name,
                    cell,
                    group.label,
                    period,
                    group.unit,
                    value,
                    (),
                    explanation,
                    action_ids=action_ids,
                    source_ids=refs,
                    conversion=conversion,
                    gap_id=gap_id,
                )
            )
            if gap_id:
                targets.setdefault(gap_id, []).append(
                    BudgetTarget(template.source.id, sheet.name, cell)
                )
    if not items and status == "applicable":
        raise ERPError("budget.empty", "适用预算任务不能返回空结果。")
    if status in {"missing_input", "not_applicable"} and not items and groups:
        if not declared:
            raise ERPError("budget.empty", "不适用或缺少输入必须说明具体原因与影响范围。")
        gap_id = next(iter(declared))
        gap = declared[gap_id]
        for group in groups:
            for cell in addresses(group.range):
                if cell not in seen and writable_target(sheet, cell, cell_index):
                    seen.add(cell)
                    items.append(
                        BudgetItem(
                            template.source.id,
                            sheet.name,
                            cell,
                            group.label,
                            None,
                            group.unit,
                            None,
                            (),
                            string(gap["message"]),
                            gap_id=gap_id,
                        )
                    )
                    targets.setdefault(gap_id, []).append(
                        BudgetTarget(template.source.id, sheet.name, cell)
                    )
    for group in groups:
        uncovered_cells = tuple(
            cell
            for cell in addresses(group.range)
            if cell not in seen and writable_target(sheet, cell, cell_index)
        )
        if not uncovered_cells:
            continue
        gap_id = "group-unmapped:" + template.source.id + ":" + sheet.name + ":" + group.id
        message = "已核对输入组的部分位置尚无对应业务来源，保留空白。"
        declared[gap_id] = {
            "id": gap_id,
            "code": "budget.group_unmapped",
            "message": message,
            "source_ids": [],
            "rule_id": "",
        }
        for cell in uncovered_cells:
            seen.add(cell)
            items.append(
                BudgetItem(
                    template.source.id,
                    sheet.name,
                    cell,
                    group.label,
                    None,
                    group.unit,
                    None,
                    (),
                    message,
                    gap_id=gap_id,
                )
            )
            targets.setdefault(gap_id, []).append(
                BudgetTarget(template.source.id, sheet.name, cell)
            )
    gaps = tuple(
        BudgetGap(
            gap_id,
            string(declared[gap_id]["code"]),
            string(declared[gap_id]["message"]),
            tuple(affected),
            strings(declared[gap_id]["source_ids"]),
            string(declared[gap_id]["rule_id"]),
        )
        for gap_id, affected in targets.items()
    )
    return tuple(items), gaps


def merge_gaps(gaps):
    merged = {}
    for gap in gaps:
        key = (gap.code, gap.message, gap.source_ids, gap.rule_id)
        if key in merged:
            merged[key] = replace(
                merged[key], targets=tuple(dict.fromkeys(merged[key].targets + gap.targets))
            )
        else:
            merged[key] = gap
    return tuple(merged.values())
