"""模型交接只传实际需要的来源与计算；目录存在、来源已发送是两个边界。"""

from collections.abc import Iterable, Mapping

from erp_agent.application.contracts import JSONObject, dumps, to_json
from erp_agent.domain.calculations import CalculationRecord
from erp_agent.domain.errors import ERPError


def reference_packet(
    catalog: JSONObject, references: Iterable[str], max_chars: int = 12000, max_items: int = 24
) -> JSONObject:
    entries = {e["id"]: (k, e) for k in ("facts", "rules", "issues") for e in catalog[k]}
    selected = tuple(dict.fromkeys(references))
    result = {
        "context_hash": catalog["context_hash"],
        "facts": [],
        "rules": [],
        "issues": [],
        "remaining_count": len(selected),
    }
    included = 0
    for identifier in selected:
        if identifier not in entries or included >= max_items:
            continue
        kind, entry = entries[identifier]
        candidate = {
            **result,
            kind: result[kind] + [entry],
            "remaining_count": len(selected) - included - 1,
        }
        if len(dumps(candidate)) <= max_chars:
            result = candidate
            included += 1
    return result


def provided_references(payload: JSONObject) -> set[str]:
    """只统计消息中真实提供的规范记录，不统计建议文字里的 ID。"""
    result: set[str] = set()
    for name in ("context", "reference_evidence", "calculation_sources"):
        packet = payload.get(name, {})
        if isinstance(packet, dict):
            for kind in ("facts", "rules", "issues"):
                for entry in packet.get(kind, []):
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                        result.add(entry["id"])
    return result


def requested_references(data: object) -> tuple[str, ...]:
    """从结构化引用槽取 ID；不从自由文字猜测引用或事实。"""
    found: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            refs = value.get("references", [])
            if isinstance(refs, list):
                found.update(r for r in refs if isinstance(r, str))
            if value.get("kind") in ("fact", "rule", "gap"):
                ref = value.get("reference_id")
                if isinstance(ref, str):
                    found.add(ref)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(data)
    return tuple(sorted(found))


def requested_calculations(data: object) -> set[str]:
    result: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("amount_calculation_id", "quantity_calculation_id") and isinstance(
                value, str
            ):
                result.add(value)
            elif key == "calculation_ids" and isinstance(value, list):
                result.update(v for v in value if isinstance(v, str))
            else:
                result.update(requested_calculations(value))
    elif isinstance(data, (list, tuple)):
        for value in data:
            result.update(requested_calculations(value))
    return result


def calculation_handoff(
    records: Mapping[str, CalculationRecord], selected: Iterable[str], max_chars: int = 12000
) -> JSONObject:
    """完整传出被上游结论实际使用的结果，其它记录留在只读查询目录。"""
    result: JSONObject = {"calculation_results": [], "unloaded_calculation_ids": []}
    for identifier in sorted(set(selected)):
        if identifier not in records:
            raise ERPError("planning.calculation", f"交接引用不存在的计算：{identifier}")
        record = to_json(records[identifier])
        candidate = {**result, "calculation_results": result["calculation_results"] + [record]}
        if len(dumps(candidate)) <= max_chars - 512:
            result = candidate
        else:
            result["unloaded_calculation_ids"].append(identifier)
    if len(dumps(result)) > max_chars:
        raise ERPError("planning.context_limit", "计算交接目录超过容量，请缩小职责结论范围。")
    return result
