from dataclasses import replace

from erp_agent.application.contracts import JSONObject, array, obj, to_json
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetItem,
    BudgetTemplate,
    KnowledgeState,
    OutputSource,
    ProblemSnapshot,
    SolutionBundle,
    WorkbookEntry,
)

DERIVED_ITEMS = ("期末", "期初", "余额", "利润", "权益", "得分", "存货", "资产", "负债", "现金结余")
MONEY_UNITS = {"元", "万元", "千元", "百万元"}
MONEY_FACT_KEYS = {"initial_cash", "initial_capital", "cash", "capital", "initial_equity"}


def unit_name(unit: str) -> str:
    return {
        "人民币元": "元",
        "人民币万元": "万元",
        "无单位": "",
        "无": "",
        "百分比": "%",
        "百分数": "%",
        "ratio": "比例",
    }.get(unit.strip(), unit.strip())


def target_kind(item: BudgetItem, number_format: str) -> str:
    if number_format == "@" or any(word in item.label for word in ("说明", "操作", "名称", "方式")):
        return "text"
    if any(word in item.label for word in ("数量", "人数", "产量", "采购量", "台数", "条数")):
        return "quantity"
    return "amount"


def compatible_conversion(source_unit: str, target_unit: str, conversion: str) -> bool:
    source, target = unit_name(source_unit), unit_name(target_unit)
    if conversion.startswith("sum_"):
        conversion = conversion.removeprefix("sum_")
    if conversion in {"identity", "sum"}:
        return source == target and not source.startswith("题目")
    return (source, target, conversion) in {
        ("元", "万元", "yuan_to_wan"),
        ("万元", "元", "wan_to_yuan"),
        ("比例", "%", "ratio_to_percent"),
    }


def mapping_material(
    solution: SolutionBundle,
    template: BudgetTemplate,
    snapshot: ProblemSnapshot | None,
    pending: tuple[tuple[int, BudgetItem], ...],
    sources: tuple[OutputSource, ...],
) -> JSONObject:
    currencies = (
        {
            unit_name(fact.unit)
            for fact in snapshot.reading.problem.facts
            if fact.state == KnowledgeState.KNOWN
            and fact.key in MONEY_FACT_KEYS
            and unit_name(fact.unit) in MONEY_UNITS
        }
        if snapshot
        else set()
    )
    currency = next(iter(currencies)) if len(currencies) == 1 else ""
    cells = {
        (sheet.name, cell.coordinate): cell for sheet in template.sheets for cell in sheet.cells
    }
    items, allowed = [], set()
    resolved = {
        source.id: replace(source, unit=currency)
        if source.id.endswith(":amount") and currency and source.unit == "题目金额单位"
        else source
        for source in sources
    }
    for index, item in pending:
        cell = cells.get((item.sheet, item.cell))
        kind = target_kind(item, cell.number_format if cell else "")
        candidates = []
        for source in resolved.values():
            if source.value is None or (source.numeric != (kind != "text")):
                continue
            if any(marker in item.label for marker in DERIVED_ITEMS):
                continue
            if source.id.startswith("action:"):
                action_id, source_kind = source.id.removeprefix("action:").rsplit(":", 1)
                allowed_action_ids = set(item.action_ids) or {
                    solution.plan.actions[i].action_id
                    for i in item.action_indices
                    if 0 <= i < len(solution.plan.actions)
                }
                if action_id not in allowed_action_ids or source.period != item.period:
                    continue
                if (
                    source_kind
                    != {"amount": "amount", "quantity": "quantity", "text": "description"}[kind]
                ):
                    continue
            elif not (
                source.id.startswith("fact:") and item.period is None and source.label == item.label
            ):
                continue
            if kind == "text" or any(
                compatible_conversion(source.unit, item.unit, conversion)
                for conversion in ("identity", "yuan_to_wan", "wan_to_yuan", "ratio_to_percent")
            ):
                candidates.append(source.id)
        allowed.update(candidates)
        items.append(
            {
                "item": to_json(item),
                "source_id": f"budget:{index}",
                "target_kind": kind,
                "candidate_source_ids": candidates,
            }
        )
    sheet_names = {item.sheet for _, item in pending}
    readings = (
        tuple(
            replace(reading, sheets=tuple(s for s in reading.sheets if s.name in sheet_names))
            for reading in snapshot.template_readings
            if reading.template_id == template.source.id
        )
        if snapshot
        else ()
    )
    return {
        "solution_id": solution.id,
        "problem_version": solution.problem_version,
        "template_id": template.source.id,
        "template_sha256": template.source.sha256,
        "periods": {
            "years": snapshot.reading.problem.years if snapshot else None,
            "periods_per_year": snapshot.reading.problem.periods_per_year if snapshot else None,
        },
        "template_readings": to_json(readings),
        "planned_inputs": items,
        "sources": to_json(
            tuple(resolved[source.id] for source in sources if source.id in allowed)
        ),
    }


def validate_entry_sources(entry: WorkbookEntry, pending: JSONObject, material: JSONObject) -> None:
    if not set(entry.source_ids) <= set(array(pending["candidate_source_ids"])):
        raise ERPError("mapping_source", "填写引用只能来自当前预算项的已有候选来源。")
    if not entry.source_ids:
        return
    item = obj(pending["item"])
    sources = {str(obj(source)["id"]): obj(source) for source in array(material["sources"])}
    for source_id in entry.source_ids:
        source = sources[source_id]
        if pending["target_kind"] == "text":
            if source["numeric"] or entry.conversion != "identity":
                raise ERPError("mapping_type", "文字输入只能引用对应文字，不能使用数值或单位转换。")
        elif not source["numeric"] or not compatible_conversion(
            str(source["unit"]), str(item["unit"]), entry.conversion
        ):
            raise ERPError("mapping_unit", "填写来源的类型或单位与预算项不匹配，不能猜测转换口径。")
