import json
from dataclasses import fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TypeAlias, cast

from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Action,
    BudgetItem,
    Fact,
    Issue,
    KnowledgeState,
    ParameterRange,
    Plan,
    ProblemSpec,
    ReadingResult,
    Rule,
    RuleSet,
    StrategyProposal,
)

JSONValue: TypeAlias = "str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]"
JSONObject: TypeAlias = dict[str, JSONValue]
CATEGORIES = (
    "initial", "certification", "market", "product", "bom", "material", "production_line",
    "worker", "training", "incentive", "shift", "finance", "discount", "demand_forecast",
    "order_pool", "order_contract", "carbon", "digital", "budget", "other",
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
        return cast(JSONValue, json.loads(content, object_pairs_hook=pairs,
                                         parse_constant=invalid_constant))
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
    if (not result.is_finite() or result.copy_abs() > Decimal("1e15")
            or result.as_tuple().exponent < -12):
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
        data = obj(value, {"id", "category", "entity_id", "entity_name", "key", "value",
                           "value_type", "unit", "state", "evidence_ids", "conversion",
                           "explanation"})
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
        facts.append(Fact(
            string(data["id"]), category, string(data["entity_id"]),
            string(data["entity_name"]), string(data["key"]), raw, kind,
            string(data["unit"]), state(data["state"]), strings(data["evidence_ids"]),
            conversion, string(data["explanation"]),
        ))
    rules: list[Rule] = []
    for value in array(root["rules"]):
        data = obj(value, {"id", "name", "expression", "state", "evidence_ids", "explanation"})
        rules.append(Rule(string(data["id"]), string(data["name"]),
                          string(data["expression"]), state(data["state"]),
                          strings(data["evidence_ids"]), string(data["explanation"])))
    issues: list[Issue] = []
    for value in array(root["issues"]):
        data = obj(value, {"code", "severity", "target", "message", "evidence_ids"})
        severity = string(data["severity"])
        if severity not in {"error", "warning", "info"}:
            raise ERPError("invalid_contract", "问题严重度无效。")
        issues.append(Issue(string(data["code"]), severity, string(data["target"]),
                            string(data["message"]), strings(data["evidence_ids"])))
    if len(facts) > 10000 or len(rules) > 1000 or len(issues) > 10000:
        raise ERPError("invalid_contract", "字段或规则数量超过限制。")
    return ReadingResult(ProblemSpec(string(root["title"]), tuple(facts)),
                         RuleSet(tuple(rules)), tuple(issues), model, raw_response=content)


def editable_reading(reading: ReadingResult) -> JSONObject:
    return {"title": reading.problem.title, "facts": to_json(reading.problem.facts),
            "rules": to_json(reading.rules.rules), "issues": to_json(reading.issues)}


def decode_plan(value: JSONValue) -> Plan:
    data = obj(value)
    required = {"title", "summary", "actions", "assumptions", "risks"}
    if set(data) not in (required, required | {"budget_items"}):
        raise ERPError("invalid_contract", "方案字段不符合要求。")
    actions: list[Action] = []
    for value in array(data["actions"]):
        action = obj(value, {"period", "kind", "entity_id", "amount", "quantity", "description"})
        actions.append(Action(
            integer(action["period"]), string(action["kind"]), string(action["entity_id"]),
            None if action["amount"] is None else decimal(action["amount"]),
            None if action["quantity"] is None else integer(action["quantity"], 0, 10**12),
            string(action["description"]),
        ))
    if len(actions) > 5000:
        raise ERPError("invalid_contract", "方案行动过多。")
    budget_items = []
    for value in array(data.get("budget_items", [])):
        item = obj(value, {"template_id", "sheet", "cell", "label", "period", "unit", "value",
                           "action_indices", "explanation"})
        budget_items.append(BudgetItem(
            string(item["template_id"], 100), string(item["sheet"], 100), string(item["cell"], 20),
            string(item["label"]), None if item["period"] is None else integer(item["period"]),
            string(item["unit"]), None if item["value"] is None else string(item["value"]),
            tuple(integer(i, 0, max(0, len(actions) - 1)) for i in array(item["action_indices"])),
            string(item["explanation"]),
        ))
    if len(budget_items) > 10000:
        raise ERPError("invalid_contract", "预算条目过多。")
    return Plan(string(data["title"]), string(data["summary"]), tuple(actions),
                strings(data["assumptions"]), strings(data["risks"]), tuple(budget_items))


def decode_proposal(content: str, model: str) -> StrategyProposal:
    root = obj(loads(content), {"plan", "parameters"})
    plan = decode_plan(root["plan"])
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
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT: JSONObject = {"type": "string"}
NULLABLE_TEXT: JSONObject = {"type": ["string", "null"]}
TEXTS: JSONObject = {"type": "array", "items": TEXT}
STATE: JSONObject = {"type": "string", "enum": [s.value for s in KnowledgeState]}
FACT_SCHEMA = object_schema({
    "id": TEXT, "category": {"type": "string", "enum": list(CATEGORIES)},
    "entity_id": TEXT, "entity_name": TEXT, "key": TEXT, "value": NULLABLE_TEXT,
    "value_type": {"type": "string", "enum": ["number", "integer", "text"]},
    "unit": TEXT, "state": STATE, "evidence_ids": TEXTS,
    "conversion": {"type": "string", "enum": ["identity", "percent_to_ratio", "wan_to_yuan"]},
    "explanation": TEXT,
})
RULE_SCHEMA = object_schema({"id": TEXT, "name": TEXT, "expression": TEXT, "state": STATE,
                             "evidence_ids": TEXTS, "explanation": TEXT})
ISSUE_SCHEMA = object_schema({"code": TEXT,
                              "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                              "target": TEXT, "message": TEXT, "evidence_ids": TEXTS})
READING_SCHEMA = object_schema({"title": TEXT, "facts": {"type": "array", "items": FACT_SCHEMA},
                                "rules": {"type": "array", "items": RULE_SCHEMA},
                                "issues": {"type": "array", "items": ISSUE_SCHEMA}})
ACTION_SCHEMA = object_schema({"period": {"type": "integer"}, "kind": TEXT,
                               "entity_id": TEXT, "amount": NULLABLE_TEXT,
                               "quantity": {"type": ["integer", "null"]}, "description": TEXT})
PROPOSAL_SCHEMA = object_schema({
    "plan": object_schema({"title": TEXT, "summary": TEXT,
                            "actions": {"type": "array", "items": ACTION_SCHEMA},
                            "assumptions": TEXTS, "risks": TEXTS,
                            "budget_items": {"type": "array", "items": object_schema({
                                "template_id": TEXT, "sheet": TEXT, "cell": TEXT,
                                "label": TEXT, "period": {"type": ["integer", "null"]},
                                "unit": TEXT, "value": NULLABLE_TEXT,
                                "action_indices": {"type": "array", "items": {"type": "integer"}},
                                "explanation": TEXT,
                            })}}),
    "parameters": {"type": "array", "items": object_schema({
        "action_index": {"type": "integer"},
        "field": {"type": "string", "enum": ["amount", "quantity"]},
        "minimum": TEXT, "maximum": TEXT, "step": TEXT,
    })},
})
