from dataclasses import replace

from erp_agent.application.contracts import (
    ACTION_SCHEMA,
    PATCH_SCHEMA,
    JSONObject,
    JSONValue,
    apply_action_patch,
    array,
    decode_action,
    obj,
    object_schema,
)
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Plan

MAX_REPAIR_OBJECTS = 24


def plan_repair_schema(allow_additions: bool) -> JSONObject:
    if not allow_additions:
        return PATCH_SCHEMA
    return object_schema(
        {
            "patches": PATCH_SCHEMA["properties"]["patches"],
            "additions": {"type": "array", "maxItems": MAX_REPAIR_OBJECTS, "items": ACTION_SCHEMA},
        }
    )


def apply_plan_repair(
    plan: Plan,
    value: JSONValue,
    allowed_ids: set[str],
    allowed_fields: dict[str, set[str]],
    *,
    horizon: int,
    allow_additions: bool,
) -> Plan:
    data = obj(value)
    required = {"patches"}
    allowed = {"patches", "additions"} if allow_additions else required
    if not required <= set(data) or set(data) - allowed:
        raise ERPError("patch_scope", "方案补丁只能包含本次授权的替换或补充字段。")
    patches = array(data["patches"])
    raw_additions = array(data.get("additions", [])) if allow_additions else []
    if len(patches) + len(raw_additions) > MAX_REPAIR_OBJECTS:
        raise ERPError("solver_repair_limit", "本轮替换和补充合计不能超过 24 个行动。")
    if not patches and not raw_additions:
        raise ERPError("patch_empty", "未提供有效的替换或缺期补充。")
    patched = (
        apply_action_patch(plan, {"patches": patches}, allowed_ids, allowed_fields)
        if patches
        else plan
    )
    missing = set(range(1, horizon + 1)) - {a.period for a in patched.actions}
    ids = {a.action_id for a in patched.actions}
    additions = []
    for value in raw_additions:
        action = decode_action(value)
        if not action.action_id.strip() or action.action_id in ids:
            raise ERPError("patch_scope", "补充行动必须使用新的非空 ID，不能覆盖已有行动。")
        if action.period not in missing:
            raise ERPError("patch_scope", "补充行动只能属于替换后仍缺失的经营期。")
        ids.add(action.action_id)
        additions.append(action)
    # 只按期间插入新行动；已有行动的相对顺序、稳定 ID 和未开放字段保持原样。
    additions.sort(key=lambda action: action.period)
    ordered = []
    cursor = 0
    for action in patched.actions:
        while cursor < len(additions) and additions[cursor].period < action.period:
            ordered.append(additions[cursor])
            cursor += 1
        ordered.append(action)
    ordered.extend(additions[cursor:])
    return replace(patched, actions=tuple(ordered))
