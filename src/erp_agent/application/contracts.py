import hashlib
import json
from dataclasses import fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TypeAlias, cast

from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Action,
    ActionCondition,
    ActionKind,
    BudgetItem,
    ExtensionDetails,
    Fact,
    FinanceDetails,
    Issue,
    KnowledgeState,
    OrderDetails,
    ParameterRange,
    Plan,
    ProblemSpec,
    ProductionDetails,
    ProgressDetails,
    ReadingResult,
    ResourceDetails,
    Rule,
    RuleSet,
    StrategyProposal,
    WorkforceDetails,
)
from erp_agent.domain.planning import PlanPreparation

JSONValue: TypeAlias = "str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]"
JSONObject: TypeAlias = dict[str, JSONValue]
CATEGORIES = (
    "initial",
    "certification",
    "market",
    "product",
    "bom",
    "material",
    "production_line",
    "worker",
    "training",
    "incentive",
    "shift",
    "finance",
    "discount",
    "demand_forecast",
    "order_pool",
    "order_contract",
    "carbon",
    "digital",
    "budget",
    "other",
)


def to_json(value: object) -> JSONValue:
    if isinstance(value, Enum):
        return to_json(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_json(item) for key, item in value.items()}
    raise TypeError(f"无法序列化 {type(value).__name__}")


def dumps(value: object) -> str:
    return json.dumps(to_json(value), ensure_ascii=False, allow_nan=False)


def loads(content: str) -> JSONValue:
    def pairs(items: list[tuple[str, JSONValue]]) -> JSONObject:
        result: JSONObject = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON 字段重复")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError(f"非法 JSON 常量 {value}")

    try:
        return cast(
            JSONValue, json.loads(content, object_pairs_hook=pairs, parse_constant=invalid_constant)
        )
    except (ValueError, RecursionError) as exc:
        raise ERPError("invalid_json", "内容不是合法 JSON。") from exc


def obj(value: JSONValue, required: set[str] | None = None) -> JSONObject:
    if not isinstance(value, dict):
        raise ERPError("invalid_contract", "字段必须是对象。")
    if required is not None and set(value) != required:
        raise ERPError("invalid_contract", f"对象字段不匹配，要求：{', '.join(sorted(required))}")
    return value


def array(value: JSONValue) -> list[JSONValue]:
    if not isinstance(value, list):
        raise ERPError("invalid_contract", "字段必须是数组。")
    return value


def string(value: JSONValue, maximum: int = 50_000) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ERPError("invalid_contract", "字段必须是长度受限的字符串。")
    return value


def integer(value: JSONValue, minimum: int = 1, maximum: int = 10000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ERPError("invalid_contract", f"整数必须在 {minimum} 到 {maximum} 之间。")
    return value


def strings(value: JSONValue) -> tuple[str, ...]:
    return tuple(string(item) for item in array(value))


def decimal(value: JSONValue) -> Decimal:
    text = string(value)
    if len(text) > 100:
        raise ERPError("invalid_contract", "数值字符串过长。")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ERPError("invalid_contract", "数值字符串无效。") from exc
    if result.is_finite():
        sign, digits, exponent = result.as_tuple()
        while exponent < 0 and len(digits) > 1 and digits[-1] == 0:
            digits, exponent = digits[:-1], exponent + 1
        result = Decimal((sign, digits, exponent))
        if result.is_zero():
            result = Decimal(0)
    if (
        not result.is_finite()
        or result.copy_abs() > Decimal("1e15")
        or result.as_tuple().exponent < -12
    ):
        raise ERPError("invalid_contract", "数值超出允许范围。")
    return result


def state(value: JSONValue) -> KnowledgeState:
    try:
        return KnowledgeState(string(value))
    except ValueError as exc:
        raise ERPError("invalid_contract", "未知字段状态。") from exc


def decode_reading(content: str, model: str) -> ReadingResult:
    root = obj(loads(content), {"title", "facts", "rules", "issues"})
    facts: list[Fact] = []
    for value in array(root["facts"]):
        data = obj(
            value,
            {
                "id",
                "category",
                "entity_id",
                "entity_name",
                "key",
                "value",
                "value_type",
                "unit",
                "state",
                "evidence_ids",
                "conversion",
                "explanation",
            },
        )
        category, kind = string(data["category"]), string(data["value_type"])
        if category not in CATEGORIES or kind not in {"number", "text", "integer"}:
            raise ERPError("invalid_contract", "字段分类或数值类型无效。")
        conversion = string(data["conversion"])
        if conversion not in {"identity", "percent_to_ratio", "wan_to_yuan"}:
            raise ERPError("invalid_contract", "未知单位转换。")
        raw = None if data["value"] is None else string(data["value"])
        if raw is not None and kind in {"number", "integer"}:
            number = decimal(raw)
            if kind == "integer" and number != number.to_integral_value():
                raise ERPError("invalid_contract", "整数值不能包含小数。")
        facts.append(
            Fact(
                string(data["id"]),
                category,
                string(data["entity_id"]),
                string(data["entity_name"]),
                string(data["key"]),
                raw,
                kind,
                string(data["unit"]),
                state(data["state"]),
                strings(data["evidence_ids"]),
                conversion,
                string(data["explanation"]),
            )
        )
    rules: list[Rule] = []
    for value in array(root["rules"]):
        data = obj(value, {"id", "name", "expression", "state", "evidence_ids", "explanation"})
        rules.append(
            Rule(
                string(data["id"]),
                string(data["name"]),
                string(data["expression"]),
                state(data["state"]),
                strings(data["evidence_ids"]),
                string(data["explanation"]),
            )
        )
    issues: list[Issue] = []
    for value in array(root["issues"]):
        data = obj(value)
        required_issue = {"code", "severity", "target", "message", "evidence_ids"}
        metadata = {"field_path", "action_id", "budget_id", "source_ids", "impact"}
        if not required_issue <= set(data) or set(data) - required_issue - metadata:
            raise ERPError("invalid_contract", "问题字段不符合要求。")
        severity = string(data["severity"])
        if severity not in {"error", "warning", "info"}:
            raise ERPError("invalid_contract", "问题严重度无效。")
        issues.append(
            Issue(
                string(data["code"]),
                severity,
                string(data["target"]),
                string(data["message"]),
                strings(data["evidence_ids"]),
                string(data.get("field_path", "")),
                string(data.get("action_id", "")),
                string(data.get("budget_id", "")),
                strings(data.get("source_ids", [])),
                strings(data.get("impact", [])),
            )
        )
    if len(facts) > 10000 or len(rules) > 1000 or len(issues) > 10000:
        raise ERPError("invalid_contract", "字段或规则数量超过限制。")
    return ReadingResult(
        ProblemSpec(string(root["title"]), tuple(facts)),
        RuleSet(tuple(rules)),
        tuple(issues),
        model,
        raw_response=content,
    )


def editable_reading(reading: ReadingResult) -> JSONObject:
    return {
        "title": reading.problem.title,
        "facts": to_json(reading.problem.facts),
        "rules": to_json(reading.rules.rules),
        "issues": to_json(reading.issues),
    }


def decode_action(value: JSONValue, *, legacy_index: int | None = None) -> Action:
    data = obj(value)
    legacy = {"period", "kind", "entity_id", "amount", "quantity", "description"}
    if set(data) == legacy:
        if legacy_index is None:
            raise ERPError("invalid_contract", "新行动必须提供稳定 ID 与结构化依据。")
        digest = hashlib.sha256(dumps(data).encode()).hexdigest()[:20]
        return Action(
            integer(data["period"]),
            string(data["kind"]),
            string(data["entity_id"]),
            None if data["amount"] is None else decimal(data["amount"]),
            None if data["quantity"] is None else integer(data["quantity"], 0, 10**12),
            string(data["description"]),
            action_id=f"legacy-{legacy_index}-{digest}",
        )
    if data.get("schema_version") == "1" and legacy_index is not None:
        original = {key: data[key] for key in legacy}
        decoded = decode_action(original, legacy_index=legacy_index)
        from dataclasses import replace

        return replace(decoded, action_id=string(data.get("action_id", decoded.action_id)))
    expected = set(ACTION_SCHEMA["properties"])
    obj(data, expected)
    try:
        kind = ActionKind(string(data["kind"]))
    except ValueError as exc:
        raise ERPError("invalid_action_type", "行动类型无效；扩展动作须用 extension。") from exc
    if data["schema_version"] != "2":
        raise ERPError("invalid_contract", "新行动 schema_version 必须为 2。")
    condition = obj(data["condition"], set(CONDITION_SCHEMA["properties"]))
    details = obj(data["details"])
    cls, keys = ACTION_DETAILS[kind.value]
    obj(details, set(keys))
    values = {}
    for key in keys:
        item = details[key]
        if (
            item is None
            and key
            not in {
                "counterparty_entity_id",
                "line_entity_id",
                "program_entity_id",
                "obligation_action_id",
            }
            and not (key == "product_entity_id" and cls is OrderDetails)
        ):
            raise ERPError("invalid_action_details", f"{kind.value} 的 {key} 不能为空。")
        values[key] = (
            strings(item)
            if key == "worker_entity_ids"
            else None
            if item is None
            else string(item, 300)
        )
    amount_meaning = string(data["amount_meaning"])
    if amount_meaning not in AMOUNT_MEANINGS:
        raise ERPError("invalid_action_amount", "行动金额含义无效。")
    if string(condition["kind"]) not in {"always", "scenario", "fact", "action", "rule", "gap"}:
        raise ERPError("invalid_action_condition", "行动条件类型无效。")
    return Action(
        integer(data["period"]),
        kind.value,
        string(data["entity_id"], 300),
        None if data["amount"] is None else decimal(data["amount"]),
        None if data["quantity"] is None else integer(data["quantity"], 0, 10**12),
        string(data["description"], 1200),
        action_id=string(data["action_id"], 300),
        quantity_unit=string(data["quantity_unit"], 100),
        amount_unit=string(data["amount_unit"], 100),
        amount_meaning=amount_meaning,
        related_entity_ids=strings(data["related_entity_ids"]),
        depends_on=strings(data["depends_on"]),
        fact_ids=strings(data["fact_ids"]),
        rule_ids=strings(data["rule_ids"]),
        gap_ids=strings(data["gap_ids"]),
        completion_period=None
        if data["completion_period"] is None
        else integer(data["completion_period"]),
        payment_period=None if data["payment_period"] is None else integer(data["payment_period"]),
        missing_reason=string(data["missing_reason"], 1200),
        condition=ActionCondition(
            **{key: None if val is None else string(val) for key, val in condition.items()}
        ),
        details=cls(**values),
        schema_version="2",
    )


def decode_plan(value: JSONValue, *, allow_legacy: bool = True) -> Plan:
    data = obj(value)
    required = {"title", "summary", "actions", "assumptions", "risks"}
    if not required <= set(data) or set(data) - required - {"budget_items", "preparation"}:
        raise ERPError("invalid_contract", "方案字段不符合要求。")
    decoded_actions = []
    for index, value in enumerate(array(data["actions"])):
        try:
            decoded_actions.append(
                decode_action(value, legacy_index=index if allow_legacy else None)
            )
        except ERPError as error:
            identifier = value.get("action_id", "unknown") if isinstance(value, dict) else "unknown"
            raise ERPError(
                error.code, f"actions.{index} · {identifier}：{error.message}", error.status
            ) from error
    actions = tuple(decoded_actions)
    if len(actions) > 5000:
        raise ERPError("invalid_contract", "方案行动过多。")
    budget_items = []
    for value in array(data.get("budget_items", [])):
        item = obj(value)
        legacy = {
            "template_id",
            "sheet",
            "cell",
            "label",
            "period",
            "unit",
            "value",
            "action_indices",
            "explanation",
        }
        if not legacy <= set(item) or set(item) - legacy - {
            "action_ids",
            "source_ids",
            "conversion",
            "gap_id",
        }:
            raise ERPError("invalid_contract", "预算条目字段不符合要求。")
        indices = tuple(
            integer(i, 0, max(0, len(actions) - 1)) for i in array(item["action_indices"])
        )
        action_ids = strings(item.get("action_ids", [])) or tuple(
            actions[i].action_id for i in indices
        )
        budget_items.append(
            BudgetItem(
                string(item["template_id"], 100),
                string(item["sheet"], 100),
                string(item["cell"], 20),
                string(item["label"]),
                None if item["period"] is None else integer(item["period"]),
                string(item["unit"]),
                None if item["value"] is None else string(item["value"]),
                indices,
                string(item["explanation"]),
                action_ids,
                strings(item.get("source_ids", [])),
                string(item.get("conversion", "identity")),
                None if item.get("gap_id") is None else string(item["gap_id"]),
            )
        )
    if len(budget_items) > 10000:
        raise ERPError("invalid_contract", "预算条目过多。")
    return Plan(
        string(data["title"]),
        string(data["summary"]),
        actions,
        strings(data["assumptions"]),
        strings(data["risks"]),
        tuple(budget_items),
        _decode_preparation(data.get("preparation")),
    )


def _decode_preparation(value: JSONValue) -> "PlanPreparation | None":
    from erp_agent.application.planning_codec import decode_preparation

    return decode_preparation(obj(value)) if value is not None else None


def decode_proposal(content: str, model: str, *, allow_legacy: bool = True) -> StrategyProposal:
    root = obj(loads(content), {"plan", "parameters"})
    plan = decode_plan(root["plan"], allow_legacy=allow_legacy)
    actions = plan.actions
    parameters: list[ParameterRange] = []
    for value in array(root["parameters"]):
        parameter = obj(value, {"action_index", "field", "minimum", "maximum", "step"})
        index = integer(parameter["action_index"], 0, max(0, len(actions) - 1))
        key = string(parameter["field"])
        low, high, step = (decimal(parameter[k]) for k in ("minimum", "maximum", "step"))
        if not actions or key not in {"amount", "quantity"} or low < 0 or high < low or step <= 0:
            raise ERPError("invalid_contract", "搜索参数范围无效。")
        baseline = getattr(actions[index], key)
        if baseline is None or not low <= baseline <= high:
            raise ERPError("invalid_contract", "搜索参数必须包含基线值且引用现有字段。")
        if key == "quantity" and any(n != n.to_integral_value() for n in (low, high, step)):
            raise ERPError("invalid_contract", "数量搜索范围和步长必须为整数。")
        parameters.append(ParameterRange(index, key, low, high, step))
    if len({(p.action_index, p.field) for p in parameters}) != len(parameters):
        raise ERPError("invalid_contract", "搜索参数重复。")
    return StrategyProposal(plan, tuple(parameters), content, model)


def object_schema(properties: JSONObject) -> JSONObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT: JSONObject = {"type": "string"}
NULLABLE_TEXT: JSONObject = {"type": ["string", "null"]}
TEXTS: JSONObject = {"type": "array", "items": TEXT}
STATE: JSONObject = {"type": "string", "enum": [s.value for s in KnowledgeState]}
FACT_SCHEMA = object_schema(
    {
        "id": TEXT,
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "entity_id": TEXT,
        "entity_name": TEXT,
        "key": TEXT,
        "value": NULLABLE_TEXT,
        "value_type": {"type": "string", "enum": ["number", "integer", "text"]},
        "unit": TEXT,
        "state": STATE,
        "evidence_ids": TEXTS,
        "conversion": {"type": "string", "enum": ["identity", "percent_to_ratio", "wan_to_yuan"]},
        "explanation": TEXT,
    }
)
RULE_SCHEMA = object_schema(
    {
        "id": TEXT,
        "name": TEXT,
        "expression": TEXT,
        "state": STATE,
        "evidence_ids": TEXTS,
        "explanation": TEXT,
    }
)
ISSUE_SCHEMA = object_schema(
    {
        "code": TEXT,
        "severity": {"type": "string", "enum": ["error", "warning", "info"]},
        "target": TEXT,
        "message": TEXT,
        "evidence_ids": TEXTS,
    }
)
READING_SCHEMA = object_schema(
    {
        "title": TEXT,
        "facts": {"type": "array", "items": FACT_SCHEMA},
        "rules": {"type": "array", "items": RULE_SCHEMA},
        "issues": {"type": "array", "items": ISSUE_SCHEMA},
    }
)
AMOUNT_MEANINGS = (
    "payment",
    "receipt",
    "cost",
    "revenue",
    "financing_principal",
    "repayment_principal",
    "reference",
    "none",
    "unknown",
)
CONDITION_SCHEMA = object_schema(
    {
        "kind": {"type": "string", "enum": ["always", "scenario", "fact", "action", "rule", "gap"]},
        "reference_id": NULLABLE_TEXT,
        "operator": NULLABLE_TEXT,
        "value": NULLABLE_TEXT,
        "explanation": TEXT,
    }
)
ACTION_DETAILS = {
    **{
        kind: (ResourceDetails, ("resource_entity_id", "counterparty_entity_id"))
        for kind in ("purchase", "arrival")
    },
    **{
        kind: (ProductionDetails, ("product_entity_id", "line_entity_id", "worker_entity_ids"))
        for kind in ("start_production", "complete_production")
    },
    **{
        kind: (OrderDetails, ("order_entity_id", "product_entity_id"))
        for kind in ("bid", "contract", "delivery", "receipt")
    },
    **{
        kind: (FinanceDetails, ("finance_entity_id", "obligation_action_id"))
        for kind in ("borrow", "repay", "payment")
    },
    **{
        kind: (WorkforceDetails, ("worker_entity_id", "program_entity_id"))
        for kind in ("recruit", "train", "dismiss")
    },
    **{
        kind: (ProgressDetails, ("target_entity_id",))
        for kind in ("research", "certify", "market_open", "install", "review")
    },
    **{kind: (ExtensionDetails, ("type_name", "reason")) for kind in ("extension", "unknown")},
}
ACTION_COMMON = {
    "action_id": TEXT,
    "schema_version": {"type": "string", "const": "2"},
    "period": {"type": "integer", "minimum": 1},
    "entity_id": TEXT,
    "amount": NULLABLE_TEXT,
    "quantity": {"type": ["integer", "null"], "minimum": 0},
    "description": TEXT,
    "quantity_unit": TEXT,
    "amount_unit": TEXT,
    "amount_meaning": {"type": "string", "enum": list(AMOUNT_MEANINGS)},
    "related_entity_ids": TEXTS,
    "depends_on": TEXTS,
    "fact_ids": TEXTS,
    "rule_ids": TEXTS,
    "gap_ids": TEXTS,
    "completion_period": {"type": ["integer", "null"], "minimum": 1},
    "payment_period": {"type": ["integer", "null"], "minimum": 1},
    "missing_reason": TEXT,
    "condition": CONDITION_SCHEMA,
}
ACTION_SCHEMA = object_schema(
    {
        **ACTION_COMMON,
        "kind": {"type": "string", "enum": [k.value for k in ActionKind]},
        "details": {
            "anyOf": [
                object_schema(
                    {
                        key: TEXTS
                        if key == "worker_entity_ids"
                        else NULLABLE_TEXT
                        if key
                        in {
                            "counterparty_entity_id",
                            "line_entity_id",
                            "product_entity_id",
                            "program_entity_id",
                            "obligation_action_id",
                        }
                        else TEXT
                        for key in keys
                    }
                )
                for _, keys in dict.fromkeys(ACTION_DETAILS.values())
            ]
        },
    }
)
PROPOSAL_SCHEMA = object_schema(
    {
        "plan": object_schema(
            {
                "title": TEXT,
                "summary": TEXT,
                "actions": {"type": "array", "items": ACTION_SCHEMA},
                "assumptions": TEXTS,
                "risks": TEXTS,
                "budget_items": {
                    "type": "array",
                    "items": object_schema(
                        {
                            "template_id": TEXT,
                            "sheet": TEXT,
                            "cell": TEXT,
                            "label": TEXT,
                            "period": {"type": ["integer", "null"]},
                            "unit": TEXT,
                            "value": NULLABLE_TEXT,
                            "action_indices": {"type": "array", "items": {"type": "integer"}},
                            "explanation": TEXT,
                        }
                    ),
                },
            }
        ),
        "parameters": {
            "type": "array",
            "items": object_schema(
                {
                    "action_index": {"type": "integer"},
                    "field": {"type": "string", "enum": ["amount", "quantity"]},
                    "minimum": TEXT,
                    "maximum": TEXT,
                    "step": TEXT,
                }
            ),
        },
    }
)

PERIODS = {"type": "array", "items": {"type": "integer", "minimum": 1}}
RECOMMENDATION_SCHEMA = object_schema(
    {
        "id": TEXT,
        "entity_id": TEXT,
        "periods": PERIODS,
        "action_ids": TEXTS,
        "fact_ids": TEXTS,
        "rule_ids": TEXTS,
        "gap_ids": TEXTS,
        "explanation": TEXT,
    }
)
DEPENDENCY_SCHEMA = object_schema(
    {
        "action_id": TEXT,
        "requires_action_id": TEXT,
        "required_period": {"type": ["integer", "null"], "minimum": 1},
        "fact_ids": TEXTS,
        "rule_ids": TEXTS,
        "gap_ids": TEXTS,
    }
)
RISK_SCHEMA = object_schema(
    {
        "id": TEXT,
        "periods": PERIODS,
        "action_ids": TEXTS,
        "gap_ids": TEXTS,
        "fact_ids": TEXTS,
        "rule_ids": TEXTS,
        "condition": CONDITION_SCHEMA,
        "response": TEXT,
    }
)
SPECIALIST_SCHEMA = object_schema(
    {
        "status": {
            "type": "string",
            "enum": ["applicable", "partial", "not_applicable", "missing_input", "failed"],
        },
        "reason": TEXT,
        "candidate_actions": {"type": "array", "items": ACTION_SCHEMA},
        "recommendations": {"type": "array", "items": RECOMMENDATION_SCHEMA, "maxItems": 24},
        "dependencies": {"type": "array", "items": DEPENDENCY_SCHEMA, "maxItems": 16},
        "risks": {"type": "array", "items": RISK_SCHEMA, "maxItems": 16},
    }
)
PATCH_SCHEMA = object_schema(
    {
        "patches": {
            "type": "array",
            "maxItems": 24,
            "items": object_schema(
                {
                    "action_id": TEXT,
                    "replacement": ACTION_SCHEMA,
                }
            ),
        },
    }
)


def field_patch_schema(schema: JSONObject, allowed: set[str] | None) -> JSONObject:
    if allowed is None:
        return schema
    properties = obj(schema["properties"])
    return object_schema({name: properties[name] for name in sorted(allowed)})


def merge_patch_fields(
    original: JSONObject, replacement: JSONObject, allowed: set[str] | None, identity: str
) -> JSONObject:
    if (
        identity not in original
        or replacement.get(identity, original[identity]) != original[identity]
    ):
        raise ERPError("patch_scope", "补丁不得改写稳定对象 ID。")
    if allowed is None:
        if replacement.get(identity) != original[identity]:
            raise ERPError("patch_scope", "完整结构补丁必须保留稳定对象 ID。")
        return replacement
    if not replacement or not any(name in allowed for name in replacement):
        raise ERPError("patch_empty", "补丁没有提供定位字段的修正。")
    for name in set(original) | set(replacement):
        if name not in allowed and name in replacement and original.get(name) != replacement[name]:
            raise ERPError("patch_scope", f"补丁不能修改未定位字段 {name}。")
    return {**original, **replacement}


def apply_action_patch(
    plan: Plan,
    value: JSONValue,
    allowed_ids: set[str],
    allowed_fields: dict[str, set[str]] | None = None,
) -> Plan:
    from dataclasses import replace

    data = obj(value, {"patches"})
    updates = {}
    for item in array(data["patches"]):
        entry = obj(item, {"action_id", "replacement"})
        identifier = string(entry["action_id"])
        if identifier not in allowed_ids or identifier in updates:
            raise ERPError("patch_scope", "补丁引用超出本次错误范围或重复行动。")
        original = next((a for a in plan.actions if a.action_id == identifier), None)
        if original is None:
            raise ERPError("patch_scope", "补丁引用不存在的行动。")
        replacement = merge_patch_fields(
            obj(to_json(original)),
            obj(entry["replacement"]),
            allowed_fields.get(identifier, set()) if allowed_fields is not None else None,
            "action_id",
        )
        action = decode_action(replacement)
        if allowed_fields is not None:
            allowed = allowed_fields.get(identifier, set())
            for field in fields(action):
                if (
                    getattr(original, field.name) != getattr(action, field.name)
                    and field.name not in allowed
                ):
                    raise ERPError(
                        "patch_scope", f"补丁不得修改未报错字段 {identifier}.{field.name}。"
                    )
        if allowed_fields and "period" in allowed_fields.get(identifier, set()):
            if any(
                getattr(original, name) is not None and getattr(action, name) is None
                for name in ("completion_period", "payment_period")
            ):
                raise ERPError("patch_scope", "时序补丁不能清空已知完成期或支付期。")
        updates[identifier] = action
    if not updates:
        raise ERPError("patch_empty", "未提供任何受限修正。")
    return replace(plan, actions=tuple(updates.get(a.action_id, a) for a in plan.actions))
