"""生成和补丁共用正式引用目录；规划风险说明不注册为题目事实或缺口。"""

from copy import deepcopy

from erp_agent.application.contracts import CONDITION_SCHEMA, JSONObject, object_schema
from erp_agent.domain.action_validation import OPERATORS, gap_reference
from erp_agent.domain.models import Action, ProblemSnapshot

REFERENCE_INSTRUCTION = (
    "fact_ids/rule_ids/gap_ids 只从 reference_catalog 的对应列表选择，不能自建或改写 ID。"
    "gap_ids 是题目已有问题的引用，不是给本轮风险命名的字段；无适用已有缺口时用 []。"
    "新发现的不确定性保留在风险 response、建议 explanation 或行动 missing_reason 中，"
    "说明受影响对象和应对，不要求用户补充。条件只引用目录内事实/规则/缺口或本轮行动；"
    "持续适用的风险评估可用 always，并在 explanation 描述不确定性，"
    "这不表示风险已触发或行动前提已满足。行动真实执行前提不能改成 always 来绕过，"
    "不得将未知事实改为已满足、删除真实依赖或换成无关来源。"
)


def reference_catalog(snapshot: ProblemSnapshot, actions: tuple[Action, ...] = ()) -> JSONObject:
    return {
        "fact_ids": sorted({f.id for f in snapshot.reading.problem.facts}),
        "rule_ids": sorted({r.id for r in snapshot.reading.rules.rules}),
        "gap_ids": sorted({gap_reference(i) for i in snapshot.reading.issues}),
        "entity_ids": sorted({f.entity_id for f in snapshot.reading.problem.facts} | {"company"}),
        "action_ids": list(dict.fromkeys(a.action_id for a in actions)),
    }


def reference_schema(
    schema: JSONObject, catalog: JSONObject, *, allow_new_actions: bool = True
) -> JSONObject:
    """复用枚举定义，避免在每个动作/风险/补丁中重复大型来源目录。"""
    result = deepcopy(schema)
    definitions = result.setdefault("$defs", {})
    names = {key: f"solver_{key}" for key in catalog if catalog[key]}
    for key, name in names.items():
        definitions[name] = {"type": "string", "enum": list(catalog[key])}

    def reference(key: str, *, nullable: bool = False) -> JSONObject:
        if key not in names:
            return {"type": "null"} if nullable else {"type": "string", "not": {}}
        value = {"$ref": f"#/$defs/{names[key]}"}
        return {"anyOf": [value, {"type": "null"}]} if nullable else value

    conditions = []
    for kind, key in (
        ("fact", "fact_ids"),
        ("rule", "rule_ids"),
        ("gap", "gap_ids"),
        ("action", "action_ids"),
        ("always", ""),
        ("scenario", ""),
    ):
        if kind not in {"always", "scenario", "action"} and not catalog.get(key):
            continue
        if kind == "action" and not allow_new_actions and not catalog.get(key):
            continue
        properties = deepcopy(CONDITION_SCHEMA["properties"])
        properties["kind"] = {"type": "string", "const": kind}
        if kind in {"always", "scenario"}:
            for name in ("reference_id", "operator", "value"):
                properties[name] = {"type": "null"}
            if kind == "scenario":
                properties["explanation"] = {"type": "string", "minLength": 1}
        else:
            properties["reference_id"] = (
                {"type": "string"} if kind == "action" and allow_new_actions else reference(key)
            )
            properties["operator"] = {
                "type": ["string", "null"],
                "enum": [None, *sorted(op for op in OPERATORS if op is not None)],
            }
        conditions.append(object_schema(properties))

    array_keys = {
        "fact_ids": "fact_ids",
        "rule_ids": "rule_ids",
        "gap_ids": "gap_ids",
        "related_entity_ids": "entity_ids",
        "worker_entity_ids": "entity_ids",
    }
    if not allow_new_actions:
        array_keys.update(action_ids="action_ids", depends_on="action_ids")

    def visit(node: object) -> None:
        if isinstance(node, list):
            for value in node:
                visit(value)
        elif isinstance(node, dict):
            for key, value in list(node.get("properties", {}).items()):
                if key in array_keys:
                    category = array_keys[key]
                    value["items"] = (
                        reference(category) if catalog.get(category) else {"type": "string"}
                    )
                    if not catalog.get(category):
                        value["maxItems"] = 0
                elif key == "condition":
                    node["properties"][key] = {"$ref": "#/$defs/solver_condition"}
                elif key == "entity_id" or key.endswith("_entity_id"):
                    nullable = "null" in value.get("type", [])
                    node["properties"][key] = reference("entity_ids", nullable=nullable)
                elif not allow_new_actions and key in {
                    "requires_action_id",
                    "obligation_action_id",
                }:
                    node["properties"][key] = reference(
                        "action_ids", nullable="null" in value.get("type", [])
                    )
            for value in list(node.values()):
                visit(value)

    visit(result)
    definitions["solver_condition"] = {"anyOf": conditions}
    # 局部补丁通常只修一个字段，不携带未使用的大型事实枚举或条件定义。
    used: set[str] = set()

    def collect(node: object) -> None:
        if isinstance(node, list):
            for value in node:
                collect(value)
        elif isinstance(node, dict):
            target = node.get("$ref", "")
            if isinstance(target, str) and target.startswith("#/$defs/"):
                name = target.removeprefix("#/$defs/")
                if name not in used:
                    used.add(name)
                    collect(definitions[name])
            for key, value in node.items():
                if key != "$defs":
                    collect(value)

    collect(result)
    result["$defs"] = {key: value for key, value in definitions.items() if key in used}
    if not result["$defs"]:
        result.pop("$defs")
    return result
