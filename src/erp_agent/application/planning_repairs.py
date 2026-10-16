"""新决策契约的一次受限修正；有效经营期与元数据不被连带改写。"""

import re
from copy import deepcopy

from erp_agent.application.contracts import JSONObject, obj
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Issue, Plan


def repair_periods(
    data: JSONObject | None, message: str, plan: Plan | None, issues: tuple[Issue, ...] = ()
) -> set[str]:
    if not data or not isinstance(data.get("periods"), dict):
        return set()
    affected = set(re.findall(r"periods\.(\d+)", message))
    if plan:
        for issue in issues:
            for action in plan.actions:
                if issue.action_id == action.action_id or action.action_id in issue.impact:
                    affected.add(str(action.period))
            if issue.code == "plan.period_coverage":
                affected.update(
                    str(p)
                    for p, values in data["periods"].items()
                    if not values or all(v.get("kind") == "review" for v in values)
                )
            if issue.code == "plan.cash_flow":
                affected.update(
                    str(a.period) for a in plan.actions if a.kind in {"borrow", "repay", "receipt"}
                )
    return affected & set(data["periods"])


def repair_schema(schema: JSONObject, affected: set[str]) -> JSONObject:
    narrowed = deepcopy(schema)
    if affected:
        narrowed["properties"] = {
            "context_hash": narrowed["properties"]["context_hash"],
            "periods": narrowed["properties"]["periods"],
        }
        narrowed["required"] = ["context_hash", "periods"]
        narrowed["properties"]["periods"]["properties"] = {
            p: v
            for p, v in narrowed["properties"]["periods"]["properties"].items()
            if p in affected
        }
        narrowed["properties"]["periods"]["required"] = sorted(affected, key=int)
    return narrowed


def apply_decision_repair(
    original: JSONObject, replacement: JSONObject, affected: set[str]
) -> JSONObject:
    if "context_hash" in original and replacement.get("context_hash") != original["context_hash"]:
        raise ERPError("planning.context", "修正不能改变题目上下文绑定。")
    if not affected:
        return replacement
    periods = obj(replacement.get("periods"))
    if not affected <= set(periods):
        raise ERPError("planning.repair_scope", "修正遗漏了指定经营期。")
    for key, value in replacement.items():
        if key == "periods":
            continue
        if key not in original or value != original[key]:
            raise ERPError("planning.repair_scope", "修正改写了未开放的方案说明。")
    for period, values in periods.items():
        if period not in affected and original["periods"].get(period) != values:
            raise ERPError("planning.repair_scope", "修正改写了未开放的有效经营期。")
    return {**original, "periods": {**original["periods"], **{p: periods[p] for p in affected}}}
