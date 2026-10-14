import re
from dataclasses import dataclass

from erp_agent.application.contracts import (
    SPECIALIST_SCHEMA,
    JSONObject,
    array,
    integer,
    obj,
    object_schema,
    string,
)
from erp_agent.application.solver_specialist import SpecialistValidationError
from erp_agent.domain.errors import ERPError


@dataclass(frozen=True)
class SpecialistRepair:
    objects: tuple[JSONObject, ...]
    schema: JSONObject
    allowed_fields: dict[tuple[str, int], set[str] | None]


def specialist_patch(raw: JSONObject, error: ERPError) -> SpecialistRepair | None:
    diagnostics = error.diagnostics if isinstance(error, SpecialistValidationError) else ()
    if not diagnostics:
        match = re.search(
            r"(recommendations|dependencies|risks|candidate_actions)\.(\d+)(?:\.(\w+))?",
            error.message,
        )
        if not match:
            return None
        diagnostics = ({"target": match.group(), "message": error.message},)
    fields: dict[tuple[str, int], set[str] | None] = {}
    temporal_ids: set[str] = set()
    for diagnostic in diagnostics:
        match = re.fullmatch(
            r"(recommendations|dependencies|risks|candidate_actions)\.(\d+)(?:\.(.+))?",
            str(diagnostic["target"]),
        )
        if not match:
            return None
        collection, index = match.group(1), int(match.group(2))
        entries = array(raw.get(collection, []))
        if index >= len(entries):
            return None
        key = collection, index
        field = match.group(3)
        if diagnostic.get("code") == "plan.dependency_time":
            fields.setdefault(key, set()).update({"period", "completion_period", "payment_period"})
            temporal_ids.add(str(obj(entries[index])["action_id"]))
        elif key not in fields or fields[key] is not None:
            if field:
                fields.setdefault(key, set()).add(field.split(".")[0])
                if diagnostic.get("code") == "plan.amount_unit":
                    fields[key].update({"amount_unit", "amount_meaning"})
            else:
                fields[key] = None
    # 下游时期可能随已定位动作变化；只开放时间字段，保留来源、金额、实体和依赖边。
    changed = True
    while changed:
        changed = False
        for index, value in enumerate(array(raw["candidate_actions"])):
            action = obj(value)
            if (
                temporal_ids.intersection(action.get("depends_on", []))
                and action.get("action_id") not in temporal_ids
            ):
                temporal_ids.add(str(action["action_id"]))
                allowed = fields.setdefault(("candidate_actions", index), set())
                if allowed is not None:
                    allowed.update({"period", "completion_period", "payment_period"})
                changed = True
    if len(fields) > 24:
        raise ERPError("solver_repair_limit", "关联补丁超过 24 个对象，本次修复停止。")
    objects = tuple(
        {
            "collection": collection,
            "index": index,
            "object": obj(array(raw[collection])[index]),
            "allowed_fields": sorted(allowed) if allowed is not None else None,
        }
        for (collection, index), allowed in sorted(fields.items())
    )
    variants = [
        object_schema(
            {
                "collection": {"type": "string", "const": item["collection"]},
                "index": {"type": "integer", "const": item["index"]},
                "replacement": SPECIALIST_SCHEMA["properties"][item["collection"]]["items"],
            }
        )
        for item in objects
    ]
    schema = (
        variants[0]
        if len(objects) == 1
        else object_schema(
            {
                "patches": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": len(objects),
                    "items": {"anyOf": variants},
                }
            }
        )
    )
    return SpecialistRepair(objects, schema, fields)


def apply_specialist_patch(
    raw: JSONObject, value: JSONObject, repair: SpecialistRepair
) -> JSONObject:
    entries = [value] if len(repair.objects) == 1 else array(obj(value, {"patches"})["patches"])
    updated = dict(raw)
    seen = set()
    for value in entries:
        entry = obj(value, {"collection", "index", "replacement"})
        collection = string(entry["collection"])
        index = integer(entry["index"], 0)
        key = collection, index
        if key not in repair.allowed_fields or key in seen:
            raise ERPError("patch_scope", "协作补丁不能修改目录外对象或重复替换。")
        seen.add(key)
        original = obj(array(raw[collection])[index])
        replacement = obj(entry["replacement"])
        identity = "action_id" if collection in {"candidate_actions", "dependencies"} else "id"
        if identity not in original or replacement.get(identity) != original[identity]:
            raise ERPError("patch_scope", "协作补丁不能改写稳定对象 ID。")
        allowed = repair.allowed_fields[key]
        if allowed is not None:
            for name in set(original) | set(replacement):
                if name not in allowed and original.get(name) != replacement.get(name):
                    raise ERPError("patch_scope", f"协作补丁不能修改未定位字段 {name}。")
        if collection == "candidate_actions" and allowed and "period" in allowed:
            if any(
                original.get(name) is not None and replacement.get(name) is None
                for name in ("completion_period", "payment_period")
            ):
                raise ERPError("patch_scope", "时序补丁不能清空已知完成期或支付期。")
        items = list(array(updated[collection]))
        items[index] = replacement
        updated[collection] = items
    if not seen:
        raise ERPError("patch_empty", "未提供有效协作补丁。")
    return updated
