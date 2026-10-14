import re

from erp_agent.application.contracts import (
    ACTION_DETAILS,
    ACTION_SCHEMA,
    CONDITION_SCHEMA,
    SPECIALIST_SCHEMA,
    JSONObject,
    JSONValue,
    array,
    decode_action,
    integer,
    obj,
    string,
    strings,
    to_json,
)
from erp_agent.domain.action_validation import OPERATORS, validate_actions
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Action, Plan, ProblemSnapshot


class SpecialistValidationError(ERPError):
    def __init__(self, diagnostics: list[JSONObject]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__(
            str(diagnostics[0]["error_code"]),
            "；".join(f"{d['target']}：{d['message']}" for d in diagnostics[:8]),
        )


def decode_specialist(
    value: JSONObject,
    snapshot: ProblemSnapshot,
    *,
    candidate_actions: tuple[Action, ...] = (),
    role: str | None = None,
) -> JSONObject:
    data = obj(value, set(SPECIALIST_SCHEMA["properties"]))
    status = string(data["status"])
    reason = string(data["reason"], 1200)
    if status not in {"applicable", "partial", "not_applicable", "missing_input", "failed"}:
        raise ERPError("solver_status", "协作任务状态无效。")
    if status in {"missing_input", "failed"}:
        raise ERPError("solver_" + status, reason or "任务未提供可用结果。")
    if status == "not_applicable" and not reason.strip():
        raise ERPError("solver_status", "不适用结果必须明确说明原因。")
    if status == "partial" and not reason.strip():
        raise ERPError("solver_status", "部分建议必须说明剩余缺口及受影响范围。")
    diagnostics: list[JSONObject] = []
    proposed_items = []
    for index, value in enumerate(array(data["candidate_actions"])):
        try:
            proposed_items.append(decode_action(value))
        except ERPError as error:
            target = f"candidate_actions.{index}"
            if isinstance(value, dict) and set(value) == set(ACTION_SCHEMA["properties"]):
                kind = value.get("kind")
                if error.code == "invalid_action_type":
                    target += ".kind"
                elif value.get("schema_version") != "2":
                    target += ".schema_version"
                elif not isinstance(value.get("condition"), dict) or set(value["condition"]) != set(
                    CONDITION_SCHEMA["properties"]
                ):
                    target += ".condition"
                elif isinstance(kind, str) and kind in ACTION_DETAILS:
                    keys = set(ACTION_DETAILS[kind][1])
                    if not isinstance(value.get("details"), dict) or set(value["details"]) != keys:
                        target += ".details"
            diagnostics.append(
                {"error_code": error.code, "target": target, "message": error.message}
            )
    proposed = tuple(proposed_items)
    if role == "budget" and array(data["candidate_actions"]):
        raise ERPError("solver_role_scope", "模板需求角色不能生成经营行动。")
    all_actions = candidate_actions + proposed
    plan = Plan("协作候选", "", all_actions, (), ())
    action_issues = (
        validate_actions(snapshot, plan, coverage=False, ordering=False)
        if all_actions and not diagnostics
        else ()
    )
    if action_issues:
        for issue in action_issues:
            parts = issue.target.split(".")
            target = issue.target
            if len(parts) > 1 and parts[0] == "actions" and parts[1].isdigit():
                local_index = int(parts[1]) - len(candidate_actions)
                if local_index >= 0:
                    target = ".".join(["candidate_actions", str(local_index), *parts[2:]])
            diagnostics.append({**to_json(issue), "error_code": "solver_action", "target": target})
    facts = {f.id for f in snapshot.reading.problem.facts}
    rules = {r.id for r in snapshot.reading.rules.rules}
    entities = {
        f.entity_id
        for f in snapshot.reading.problem.facts
        if role != "budget" or f.category != "order_pool"
    } | {"company"}
    gaps = {f"{i.code}:{i.target}" for i in snapshot.reading.issues}
    identifiers = {a.action_id for a in all_actions} | {
        obj(value).get("action_id")
        for value in array(data["candidate_actions"])
        if isinstance(value, dict) and isinstance(value.get("action_id"), str)
    }
    horizon = (snapshot.reading.problem.years or 4) * (
        snapshot.reading.problem.periods_per_year or 4
    )
    counts = {name: len(array(data[name])) for name in ("recommendations", "dependencies", "risks")}
    if counts["recommendations"] > 24 or counts["dependencies"] > 16 or counts["risks"] > 16:
        raise ERPError("solver_limit", "协作结果超过当前职责的结论数量上限。")
    if status == "applicable" and not (proposed or any(counts.values())):
        raise ERPError("solver_empty", "空结果必须说明不适用、缺少输入或失败，不能标为有效建议。")
    if status == "not_applicable" and (proposed or any(counts.values())):
        raise ERPError("solver_status", "不适用结果不能同时提供行动或结论。")
    if status == "partial" and not (proposed or counts["recommendations"]):
        raise ERPError("solver_empty", "部分结果必须提供有依据的行动或建议，不能只有缺失说明。")

    def validate_entry(
        item: JSONValue, expected: set[str], name: str, target: str, seen: set[str]
    ) -> None:
        entry = obj(item, expected)
        for key, allowed in (
            ("fact_ids", facts),
            ("rule_ids", rules),
            ("gap_ids", gaps),
            ("action_ids", identifiers),
        ):
            for identifier in strings(entry.get(key, [])):
                if identifier not in allowed:
                    raise ERPError("solver_source", f"{target}.{key} 引用未知 ID {identifier}。")
        if "entity_id" in entry and string(entry["entity_id"]) not in entities:
            raise ERPError(
                "solver_source",
                f"{target}.entity_id 引用未知实体 {entry['entity_id']}；"
                "请使用规范实体 ID，模板/工作表/输入组不是实体。",
            )
        for period in array(entry.get("periods", [])):
            integer(period, 1, horizon)
        if "id" in entry:
            identifier = string(entry["id"])
            if not identifier.strip() or identifier in seen:
                raise ERPError("solver_source", f"{target}.id 为空或重复。")
            seen.add(identifier)
        if name == "dependencies":
            dependent = string(entry["action_id"])
            required = string(entry["requires_action_id"])
            if dependent not in identifiers or required not in identifiers or dependent == required:
                raise ERPError("solver_source", f"{target} 的行动依赖无效。")
            if entry["required_period"] is not None:
                integer(entry["required_period"], 1, horizon)
        if name == "recommendations" and not string(entry["explanation"]).strip():
            raise ERPError("solver_source", f"{target}.explanation 为空。")
        if name == "risks":
            condition = obj(entry["condition"], set(CONDITION_SCHEMA["properties"]))
            for value in condition.values():
                if value is not None:
                    string(value)
            allowed = {"fact": facts, "rule": rules, "action": identifiers, "gap": gaps}
            kind = string(condition.get("kind"))
            if kind != "always" and (
                kind not in allowed or condition.get("reference_id") not in allowed[kind]
            ):
                raise ERPError("solver_source", f"{target}.condition 引用无效。")
            operator = condition.get("operator")
            if operator not in OPERATORS or (
                kind == "always"
                and any(condition.get(key) for key in ("reference_id", "operator", "value"))
            ):
                raise ERPError("solver_source", f"{target}.condition 条件结构无效。")
            if not string(entry["response"]).strip():
                raise ERPError("solver_source", f"{target}.response 不能为空。")

    for name in ("recommendations", "dependencies", "risks"):
        expected = set(obj(SPECIALIST_SCHEMA["properties"])[name]["items"]["properties"])
        seen = set()
        for index, item in enumerate(array(data[name])):
            target = f"{name}.{index}"
            try:
                validate_entry(item, expected, name, target, seen)
            except ERPError as error:
                match = re.search(
                    r"(?:recommendations|dependencies|risks)\.\d+(?:\.\w+)*", error.message
                )
                diagnostics.append(
                    {
                        "error_code": error.code,
                        "target": match.group() if match else target,
                        "message": error.message,
                    }
                )
    if diagnostics:
        raise SpecialistValidationError(diagnostics)
    if status == "partial":
        anchored = any(a.fact_ids or a.rule_ids or a.gap_ids for a in proposed) or any(
            entry.get("fact_ids")
            or entry.get("rule_ids")
            or entry.get("gap_ids")
            or set(strings(entry.get("action_ids", []))).intersection(
                a.action_id for a in candidate_actions
            )
            for entry in (obj(v) for v in array(data["recommendations"]))
        )
        if not anchored:
            raise ERPError("solver_source", "部分结果必须引用已有事实、规则、缺口或上游行动。")
    return data
