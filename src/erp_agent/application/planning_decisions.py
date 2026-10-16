"""紧凑模型决策到规范 Action 的确定性编译边界。"""

from copy import deepcopy
from decimal import Decimal
from difflib import get_close_matches

from erp_agent.application.contracts import (
    ACTION_DETAILS,
    ACTION_SCHEMA,
    TEXT,
    TEXTS,
    JSONObject,
    array,
    decimal,
    decode_action,
    obj,
    object_schema,
    string,
    strings,
)
from erp_agent.application.human_resolution import PlanningValidationError
from erp_agent.domain.action_validation import normalize_plan
from erp_agent.domain.calculations import CalculationRecord
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Plan, ProblemSnapshot


def decision_schema(horizon: int) -> JSONObject:
    action = deepcopy(ACTION_SCHEMA)
    for field in (
        "action_id",
        "schema_version",
        "fact_ids",
        "rule_ids",
        "gap_ids",
        "missing_reason",
    ):
        action["properties"].pop(field, None)
        if field in action["required"]:
            action["required"].remove(field)
    for field, schema in {
        "key": TEXT,
        "references": TEXTS,
        "amount_calculation_id": {"type": ["string", "null"]},
        "quantity_calculation_id": {"type": ["string", "null"]},
    }.items():
        action["properties"][field] = schema
        action["required"].append(field)
    action["properties"].pop("period")
    action["required"] = ["key", "kind", "entity_id", "description", "references", "details"]
    condition = action["properties"]["condition"]
    condition["required"] = ["kind", "explanation"]
    condition["anyOf"] = [
        {
            "properties": {
                "kind": {"enum": ["always", "scenario"]},
                "reference_id": {"type": "null"},
                "operator": {"type": "null"},
                "value": {"type": "null"},
            }
        },
        {
            "properties": {
                "kind": {"enum": ["fact", "rule", "action", "gap"]},
                "reference_id": {"type": "string", "minLength": 1},
            },
            "required": ["reference_id"],
        },
    ]
    # 详情和 kind 必须成对，不能让 payment 通过其它动作的详情分支。
    variants = action["properties"]["details"]["anyOf"]
    groups = list(dict.fromkeys(ACTION_DETAILS.values()))
    action["properties"]["details"] = {"type": "object"}
    action["anyOf"] = [
        {
            "properties": {
                "kind": {
                    "enum": [kind for kind, details in ACTION_DETAILS.items() if details == group]
                },
                "details": detail,
            },
            "required": ["kind", "details"],
        }
        for group, detail in zip(groups, variants, strict=True)
    ]
    return {
        **object_schema(
            {
                "context_hash": TEXT,
                "title": TEXT,
                "summary": TEXT,
                "assumptions": TEXTS,
                "risks": TEXTS,
                "periods": object_schema(
                    {
                        str(p): {
                            "type": "array",
                            "minItems": 1,
                            "items": {"$ref": "#/$defs/decision"},
                        }
                        for p in range(1, horizon + 1)
                    }
                ),
            }
        ),
        "$defs": {"decision": action},
    }


def compile_decisions(
    data: JSONObject,
    snapshot: ProblemSnapshot,
    context_hash: str,
    calculations: tuple[CalculationRecord, ...],
    available_references: set[str] | None = None,
    *,
    required_periods: tuple[int, ...] | None = None,
) -> Plan:
    if "context_hash" not in data:
        raise ERPError("planning.structure", "方案缺少题目上下文绑定。")
    if data.get("context_hash") != context_hash:
        raise ERPError("planning.context", "方案响应与冻结题目上下文不一致。")
    horizon = (snapshot.reading.problem.years or 0) * (
        snapshot.reading.problem.periods_per_year or 0
    )
    if horizon < 1:
        raise ERPError("planning.horizon", "题目没有有效经营周期。")
    periods = obj(data["periods"])
    expected = required_periods if required_periods is not None else tuple(range(1, horizon + 1))
    if any(p < 1 or p > horizon for p in expected):
        raise ERPError("planning.periods", "分段经营期超出真实周期。")
    if set(periods) != {str(p) for p in expected}:
        raise ERPError("planning.periods", "方案必须包含全部真实经营期。")
    facts = {f.id for f in snapshot.reading.problem.facts}
    rules = {r.id for r in snapshot.reading.rules.rules}
    gaps = {f"{i.code}:{i.target}" for i in snapshot.reading.issues}
    records = {record.id: record for record in calculations}
    entries = []
    keys = {}
    for p in sorted(expected):
        values = array(periods[str(p)])
        if not values or len(values) > 128:
            raise ERPError("planning.periods", f"periods.{p} 业务行动为空或过多。")
        for raw in values:
            decision = obj(raw)
            key = string(decision["key"], 100)
            if not key.strip() or key in keys:
                raise ERPError("planning.keys", f"periods.{p} 行动 key 为空或重复。")
            keys[key] = f"a-{context_hash[:10]}-{key}"
            entries.append((p, decision))
    if len(entries) > 5000:
        raise ERPError("planning.limit", "行动数量超限。")
    actions, used = [], set()

    def compile_entry(period: int, decision: JSONObject) -> None:
        raw = {
            k: v
            for k, v in decision.items()
            if k
            not in {
                "key",
                "references",
                "amount_calculation_id",
                "quantity_calculation_id",
            }
        }
        defaults = {
            "amount": None,
            "quantity": None,
            "quantity_unit": "",
            "amount_unit": "",
            "amount_meaning": "none" if raw.get("kind") == "review" else "unknown",
            "related_entity_ids": [],
            "depends_on": [],
            "completion_period": None,
            "payment_period": None,
            "condition": {
                "kind": "always",
                "reference_id": None,
                "operator": None,
                "value": None,
                "explanation": "",
            },
        }
        raw = {**defaults, **raw}
        raw["kind"] = string(raw["kind"])
        # 明确的付款动作即使标为费用，也必须进入现金折叠；仅规范本次模型决策。
        if raw["kind"] == "payment" and raw["amount_meaning"] == "cost":
            raw["amount_meaning"] = "payment"
        refs = list(strings(decision["references"]))
        for field in ("amount", "quantity"):
            calculation_id = decision.get(field + "_calculation_id")
            if calculation_id is None:
                continue
            record = records.get(string(calculation_id))
            if record is None or record.context_hash != context_hash:
                raise ERPError("planning.calculation", f"periods.{period} 引用不存在的公式结果。")
            value = Decimal(record.value)
            if field == "quantity" and value != value.to_integral_value():
                raise ERPError("planning.calculation", "数量公式结果必须为整数。")
            if raw[field] is not None and decimal(str(raw[field])) != value:
                raise ERPError("planning.calculation", "行动数值与公式结果不一致。")
            calculation_period = (
                (raw.get("payment_period") or period) if field == "amount" else period
            )
            if record.period is not None and record.period != calculation_period:
                raise ERPError("planning.calculation", "行动期间与公式结果不一致。")
            if raw[field + "_unit"] != record.unit:
                raise ERPError("planning.calculation", "行动单位与公式结果不一致。")
            raw[field] = str(value) if field == "amount" else int(value)
            refs.extend(record.fact_ids)
            if record.rule_id:
                refs.append(record.rule_id)
            used.add(record.id)
        condition = {
            "reference_id": None,
            "operator": None,
            "value": None,
            **obj(raw["condition"]),
        }
        raw["condition"] = condition
        # 模型把没有正式缺口 ID 的执行前提放进 gap 时，保留为显式情景，不补造 ID。
        if (
            condition.get("kind") == "gap"
            and all(condition.get(k) is None for k in ("reference_id", "operator", "value"))
            and isinstance(condition.get("explanation"), str)
            and condition["explanation"].strip()
        ):
            condition = {**condition, "kind": "scenario"}
            raw["condition"] = condition
        if condition.get("kind") in ("fact", "rule", "gap"):
            if (
                not isinstance(condition.get("reference_id"), str)
                or not condition["reference_id"].strip()
            ):
                raise ERPError(
                    "planning.condition",
                    "condition.reference_id 必须引用该类型的真实 ID；"
                    "无正式引用的执行前提用 scenario 并说明情景。",
                )
            refs.append(condition["reference_id"])
        unknown = set(refs) - facts - rules - gaps
        if unknown:
            candidates = {
                ref: get_close_matches(ref, sorted(facts | rules | gaps), n=2, cutoff=0.85)
                for ref in sorted(unknown)[:6]
            }
            raise PlanningValidationError(
                "planning.references",
                f"references 含未知题目引用：{sorted(unknown)[:6]}；"
                f"可按内容核对的目录候选（未自动替换）：{candidates}；"
                "按行动对象/工资等参数查询真实目录，不得从实体 ID 拼接 fact ID。",
                ({"field": "references", "unknown_ids": sorted(unknown)[:12]},),
            )
        if available_references is not None and not set(refs) <= available_references:
            raise ERPError(
                "planning.not_loaded",
                f"periods.{period} · {decision['key']}.references 尚未读取："
                + ", ".join(sorted(set(refs) - available_references)[:12]),
            )
        dependencies = strings(raw["depends_on"])
        if any(key not in keys for key in dependencies):
            raise ERPError("planning.dependencies", f"periods.{period} 含未知前置行动 key。")
        raw.update(
            {
                "schema_version": "2",
                "action_id": keys[decision["key"]],
                "period": period,
                "fact_ids": list(dict.fromkeys(r for r in refs if r in facts)),
                "rule_ids": list(dict.fromkeys(r for r in refs if r in rules)),
                "gap_ids": list(dict.fromkeys(r for r in refs if r in gaps)),
                "depends_on": [keys[key] for key in dependencies],
                "missing_reason": "未提供的数值保持未知；不涉及的金额或数量不作计算。",
            }
        )
        details = obj(raw["details"])
        _, detail_keys = ACTION_DETAILS.get(raw["kind"], (None, ()))
        optional = {
            "counterparty_entity_id",
            "line_entity_id",
            "program_entity_id",
            "obligation_action_id",
        }
        if raw["kind"] in {"bid", "contract", "delivery", "receipt"}:
            optional.add("product_entity_id")
        # 仅补契约允许的空关联；未知业务实体和错误字段仍报错。
        details = {**{k: None for k in detail_keys if k in optional}, **details}
        raw["details"] = details
        if detail_keys and set(details) != set(detail_keys):
            raise ERPError(
                "planning.details",
                f"periods.{period} · {decision['key']}.details：{raw['kind']} 要求字段 "
                f"{', '.join(detail_keys)}；缺少 {sorted(set(detail_keys) - set(details))}，"
                f"多余 {sorted(set(details) - set(detail_keys))}。",
            )
        obligation = details.get("obligation_action_id")
        if obligation is not None:
            obligation = string(obligation)
        if obligation in keys:
            raw["details"] = {
                **details,
                "obligation_action_id": keys[details["obligation_action_id"]],
            }
        raw["condition"] = obj(raw["condition"])
        if raw["condition"].get("kind") == "action":
            reference = raw["condition"].get("reference_id")
            if reference is not None:
                reference = string(reference)
            if reference in keys:
                raw["condition"] = {**raw["condition"], "reference_id": keys[reference]}
        if raw["kind"] == "review":
            raw["completion_period"] = raw["payment_period"] = None
        try:
            actions.append(decode_action(raw))
        except ERPError as error:
            raise ERPError(
                error.code, f"periods.{period} · {decision['key']}：{error.message}"
            ) from error

    diagnostics = []
    structured = []
    entity_names = {f.entity_id: f.entity_name for f in snapshot.reading.problem.facts}
    for period, decision in entries:
        try:
            compile_entry(period, decision)
        except (ERPError, KeyError) as error:
            code = error.code if isinstance(error, ERPError) else "planning.structure"
            message = (
                error.message if isinstance(error, ERPError) else f"缺少字段 {error.args[0]}。"
            )
            diagnostics.append((code, f"periods.{period} · {decision.get('key', '?')}：{message}"))
            per_year = snapshot.reading.problem.periods_per_year or 4
            location = {
                "code": code,
                "period": period,
                "key": decision.get("key", "?"),
                "period_label": f"第 {(period - 1) // per_year + 1} 年 · "
                f"第 {(period - 1) % per_year + 1} 季",
                "description": str(decision.get("description", "经营操作"))[:240],
                "entity_name": entity_names.get(
                    str(decision.get("entity_id")),
                    "公司" if decision.get("entity_id") == "company" else "经营对象",
                ),
                "entity_id": str(decision.get("entity_id", "")),
            }
            if isinstance(error, PlanningValidationError):
                structured.extend({**location, **issue} for issue in error.issues)
            else:
                structured.append(location)
    if diagnostics:
        raise PlanningValidationError(
            diagnostics[0][0],
            "；".join(message for _, message in diagnostics[:24]),
            tuple(structured[:24]),
        )
    assumptions = (
        strings(data["assumptions"])
        + tuple(r.assumption for r in calculations if r.id in used and r.assumption)
        + tuple(
            f"条件执行 {a.action_id}：{a.condition.explanation}"
            for a in actions
            if a.condition.kind == "scenario"
        )
    )
    return normalize_plan(
        snapshot,
        Plan(
            string(data["title"]),
            string(data["summary"]),
            tuple(actions),
            tuple(dict.fromkeys(assumptions)),
            strings(data["risks"]),
        ),
    )
