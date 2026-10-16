"""冻结跨期承诺后并行编制分段行动，Python 合并并执行全周期校验。"""

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from math import ceil

from erp_agent.application.contracts import (
    TEXT,
    TEXTS,
    JSONObject,
    array,
    decimal,
    dumps,
    integer,
    loads,
    obj,
    object_schema,
    string,
    strings,
    to_json,
)
from erp_agent.application.human_resolution import PlanningProblem, PlanningValidationError
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.planning_context import PlanningKnowledge
from erp_agent.application.planning_decisions import compile_decisions, decision_schema
from erp_agent.application.planning_handoff import (
    calculation_handoff,
    provided_references,
    requested_calculations,
    requested_references,
)
from erp_agent.application.planning_prompts import planning_prompt
from erp_agent.application.planning_repairs import (
    apply_decision_repair,
    repair_periods,
    repair_schema,
)
from erp_agent.application.ports import LLMRequest, LLMResponse, RunControl, TaskContext
from erp_agent.application.task_budget import task_budget
from erp_agent.application.task_reporting import report_task
from erp_agent.domain.action_validation import CASH_OUTFLOW, validate_actions
from erp_agent.domain.calculations import CalculationRecord
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Plan, ProblemSnapshot
from erp_agent.domain.money import money_value

Unit = tuple[TaskContext, LLMRequest]


def period_windows(horizon: int) -> tuple[tuple[int, ...], ...]:
    count = min(4, ceil(horizon / 4))
    width, extra = divmod(horizon, count)
    result, start = [], 1
    for index in range(count):
        end = start + width + (index < extra)
        result.append(tuple(range(start, end)))
        start = end
    return tuple(result)


def route_schema(horizon: int, windows: tuple[tuple[int, ...], ...]) -> JSONObject:
    action = deepcopy(decision_schema(horizon)["$defs"]["decision"])
    action["properties"]["period"] = {"type": "integer", "minimum": 1, "maximum": horizon}
    action["required"].append("period")
    return {
        **object_schema(
            {
                "context_hash": TEXT,
                "title": TEXT,
                "summary": TEXT,
                "assumptions": TEXTS,
                "risks": TEXTS,
                "shared_actions": {
                    "type": "array",
                    "maxItems": 24,
                    "items": {"$ref": "#/$defs/decision"},
                },
                "windows": object_schema(
                    {
                        str(index + 1): object_schema(
                            {
                                "references": {
                                    **TEXTS,
                                    "description": "选择需交给此段的当前题目资料 ID；"
                                    "程序装入下游上下文，不表示已读取或计算。",
                                },
                                "constraints": TEXTS,
                                "cash_outflow_limit": {"type": ["string", "null"]},
                                "cash_unit": TEXT,
                            }
                        )
                        for index in range(len(windows))
                    }
                ),
            }
        ),
        "$defs": {"decision": action},
    }


def window_schema(horizon: int, periods: tuple[int, ...]) -> JSONObject:
    schema = decision_schema(horizon)
    schema["properties"] = {
        k: v for k, v in schema["properties"].items() if k in {"context_hash", "periods"}
    }
    schema["required"] = ["context_hash", "periods"]
    schema["properties"]["periods"] = object_schema(
        {
            str(p): {"type": "array", "maxItems": 128, "items": {"$ref": "#/$defs/decision"}}
            for p in periods
        }
    )
    return schema


def parse_response(response: LLMResponse, expected: str) -> JSONObject:
    data = obj(loads(normalize_model_json(response.content)))
    if "context_hash" not in data:
        raise ERPError("planning.structure", "分段响应缺少题目绑定。")
    if data.get("context_hash") != expected:
        raise ERPError("planning.context", "分段响应与冻结题目绑定不一致。")
    return data


def route_references(route: JSONObject | None) -> tuple[str, ...]:
    """收集已返回路线中的资料请求，结构错误留给正式校验定位。"""
    if not route:
        return ()
    result = set()
    limits = route.get("windows", {})
    entries = list(limits.values()) if isinstance(limits, dict) else []
    actions = route.get("shared_actions", [])
    if isinstance(actions, list):
        entries.extend(actions)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        refs = entry.get("references", [])
        if isinstance(refs, list):
            result.update(ref for ref in refs if isinstance(ref, str))
        condition = entry.get("condition")
        if isinstance(condition, dict) and condition.get("kind") in {"fact", "rule", "gap"}:
            ref = condition.get("reference_id")
            if isinstance(ref, str):
                result.add(ref)
    return tuple(sorted(result))


def qualify(
    actions: dict[str, list[JSONObject]], prefix: str, shared: set[str]
) -> dict[str, list[JSONObject]]:
    keys = {string(obj(a)["key"], 100) for values in actions.values() for a in values}
    mapping = {key: key if key.startswith(prefix) else prefix + key for key in keys}
    if keys & shared:
        raise ERPError("planning.shared_action", "分段不能重复或改写冻结的跨期承诺。")
    result = deepcopy(actions)
    for period, values in result.items():
        for a in values:
            a["key"] = mapping[a["key"]]
            dependencies = list(strings(a.get("depends_on", [])))
            details = obj(a["details"])
            obligation = details.get("obligation_action_id")
            if obligation is not None:
                obligation = string(obligation)
            condition = obj(a.get("condition", {}))
            references = dependencies + ([obligation] if obligation else [])
            if condition.get("kind") == "action" and condition.get("reference_id"):
                references.append(string(condition["reference_id"]))
            unknown = set(references) - keys - shared
            if unknown:
                raise ERPError(
                    "planning.dependencies",
                    f"periods.{period} 引用未冻结的跨段行动：{sorted(unknown)[:6]}",
                )
            a["depends_on"] = [mapping.get(key, key) for key in dependencies]
            if obligation:
                details["obligation_action_id"] = mapping.get(obligation, obligation)
            if condition.get("kind") == "action":
                key = condition.get("reference_id")
                condition["reference_id"] = mapping.get(key, key)
    return result


@dataclass(frozen=True)
class WindowResult:
    plan: Plan
    response: LLMResponse
    cache_entries: tuple[tuple[Unit, LLMResponse], ...]


def generate_windows(
    snapshot: ProblemSnapshot,
    knowledge: PlanningKnowledge,
    base: Unit,
    calculations: dict[str, CalculationRecord],
    execute: Callable[[Unit], LLMResponse],
    control: RunControl,
    budget_calls: int,
    unit_choices: dict[str, str] | None = None,
) -> WindowResult:
    from erp_agent.application.solver import DraftPlanEvaluator
    from erp_agent.application.solver_period_output import check_planning_content

    horizon = snapshot.reading.problem.years * snapshot.reading.problem.periods_per_year
    windows = period_windows(horizon)
    _, original = base
    caches = []
    route_material = obj(obj(loads(original.user))["context"])
    refs = {v["id"] for k in ("facts", "rules", "issues") for v in route_material[k]}
    allowed = {v["id"] for k in ("facts", "rules", "issues") for v in knowledge.data[k]}

    def collect(response: LLMResponse) -> None:
        refs.update(response.accessed_reference_ids)
        for record in response.calculations:
            if record.context_hash != knowledge.hash:
                raise ERPError("planning.context", "分段计算引用其它题目或版本。")
            if record.id in calculations and calculations[record.id] != record:
                raise ERPError("planning.calculation", f"分段计算 ID 冲突：{record.id}")
            calculations[record.id] = record
            refs.update(record.fact_ids)

    def call(unit: Unit) -> LLMResponse:
        response = execute(unit)
        collect(response)
        return response

    def bound(schema: JSONObject) -> str:
        schema["properties"]["context_hash"] = {"type": "string", "const": knowledge.hash}
        return dumps(schema)

    route_task = TaskContext(
        "solver:synthesis:route",
        "Solver",
        "统筹经营路线与跨期承诺",
        snapshot.reading.problem.title,
        "solver",
    )
    route_request = replace(
        original,
        system=planning_prompt("route"),
        task=route_task,
        schema_name="erp_planning_route",
        schema_json=bound(route_schema(horizon, windows)),
        max_output_tokens=6000,
        user=dumps(
            {
                **obj(loads(original.user)),
                "planning_windows": [list(w) for w in windows],
                "instruction": "只确定产品/资源/资金路线、跨段共享行动与各段支出上限/约束，"
                "不编制每期操作清单。共享行动只限跨段依赖的投资、融资、交付或偿债等，最多24项。"
                "共享 key 使用 shared- 前缀；共享行动只能依赖共享行动。"
                "各段额度是拟定分配，未知可为 null，不能把未知初始现金补零。",
            }
        ),
        tool_context={
            **original.tool_context,
            "reserved_model_calls": 1 if task_budget(control, route_task).max_calls > 1 else 0,
        },
    )
    route_unit = (route_task, route_request)
    route = None
    for attempt in range(2):
        response = call(route_unit if not attempt else (route_task, route_request))
        try:
            route = obj(
                parse_response(response, knowledge.hash),
                {
                    "context_hash",
                    "title",
                    "summary",
                    "assumptions",
                    "risks",
                    "shared_actions",
                    "windows",
                },
            )
            for key in ("title", "summary"):
                if not string(route[key], 2400).strip():
                    raise ERPError("planning.route", f"路线 {key} 不能为空。")
            strings(route["assumptions"]), strings(route["risks"])
            limits = obj(route["windows"])
            if set(limits) != {str(i + 1) for i in range(len(windows))}:
                raise ERPError("planning.route", "路线必须给出全部分段的约束。")
            unknown = set(route_references(route)) - allowed
            if unknown:
                raise ERPError(
                    "planning.references", "路线含未知题目引用：" + ", ".join(sorted(unknown)[:12])
                )
            # windows.references 是下游检索选择，实际行动的依据才要求本角色读过。
            # 否则修正时新增的合法资料选择会再次耗尽唯一修正，形成交接死循环。
            unread = set(requested_references(route["shared_actions"])) - refs
            if unread:
                raise ERPError(
                    "planning.not_loaded",
                    "shared_actions.references：跨期行动引用未读取资料，需核对 ID："
                    + ", ".join(sorted(unread)[:12]),
                )
            for limit in limits.values():
                limit = obj(limit, {"references", "constraints", "cash_outflow_limit", "cash_unit"})
                strings(limit["constraints"])
                strings(limit["references"])
                if limit["cash_outflow_limit"] is not None:
                    if (
                        decimal(limit["cash_outflow_limit"]) < 0
                        or not string(limit["cash_unit"]).strip()
                    ):
                        raise ERPError("planning.route", "分段支出限额须非负并带单位。")
                    money_value(
                        decimal(limit["cash_outflow_limit"]), limit["cash_unit"], limit["cash_unit"]
                    )
            shared = {str(p): [] for p in range(1, horizon + 1)}
            values = array(route["shared_actions"])
            if len(values) > 24:
                raise ERPError("planning.route", "跨段共享行动最多24项。")
            for raw in values:
                action = dict(obj(raw))
                period = integer(action.pop("period"), maximum=horizon)
                shared[str(period)].append(action)
            shared = qualify(shared, "shared-", set())
            route["shared_actions"] = [
                {**a, "period": int(p)} for p, actions in shared.items() for a in actions
            ]
            shared_keys = {a["key"] for a in route["shared_actions"]}
            # 共享行动同样解码校验；不要求它们独自覆盖每期。
            shared_data = {
                **route,
                "periods": {p: actions for p, actions in shared.items() if actions},
            }
            if values:
                shared_plan = compile_decisions(
                    shared_data,
                    snapshot,
                    knowledge.hash,
                    tuple(calculations.values()),
                    refs,
                    required_periods=tuple(int(p) for p in shared_data["periods"]),
                )
                issues = tuple(
                    i
                    for i in validate_actions(snapshot, shared_plan, coverage=False)
                    if i.severity == "error"
                )
                if issues:
                    raise ERPError(
                        "planning.route",
                        "跨期承诺无效：" + "；".join(i.message for i in issues[:12]),
                    )
            break
        except (ERPError, KeyError) as error:
            if attempt or isinstance(error, ERPError) and error.code == "planning.context":
                message = (
                    f"路线缺少字段 {error.args[0]}。"
                    if isinstance(error, KeyError)
                    else error.message
                )
                report_task(control, route_task, "failed", error=message)
                raise PlanningProblem(
                    "planning.route" if isinstance(error, KeyError) else error.code,
                    message,
                    route_task.id,
                    {"validation_issues": list(error.issues)}
                    if isinstance(error, PlanningValidationError)
                    else None,
                ) from error
            evidence = knowledge.reference_batch(
                tuple(ref for ref in route_references(route) if ref in allowed and ref not in refs)
            )
            refs.update(v["id"] for k in ("facts", "rules", "issues") for v in evidence[k])
            route_request = replace(
                route_request,
                tool_context={
                    **route_request.tool_context,
                    "calculation_records": to_json(tuple(calculations.values())),
                    "accessed_reference_ids": sorted(refs),
                    "reserved_model_calls": 0,
                },
                user=dumps(
                    {
                        **obj(loads(route_request.user)),
                        "diagnostics": str(error),
                        "previous_route": route,
                        "reference_evidence": evidence,
                        **calculation_handoff(
                            calculations, requested_calculations(route) & calculations.keys()
                        ),
                        "repair_instruction": "核对 reference_evidence 中按 ID 补取的"
                        "当前题目原始规范记录，"
                        "据此修正路线和引用，不展开逐期计划。未补齐的资料仍须按 ID 查询；"
                        "未知 ID 不能当作缺失事实或换绑其它题目。",
                    }
                ),
            )
    caches.append(
        (
            route_unit,
            replace(
                response,
                content=dumps(route),
                accessed_reference_ids=tuple(sorted(refs)),
            ),
        )
    )
    report_task(control, route_task, "succeeded", model=response.model)
    # 路线及已校验事实才进入各段，原分析不再逐段重复发送。
    route_material = obj(obj(loads(original.user))["context"])
    refs.update(v["id"] for k in ("facts", "rules", "issues") for v in route_material[k])
    shared_calculations = requested_calculations(route["shared_actions"])
    calculation_payload = calculation_handoff(calculations, shared_calculations)
    seed_records = calculation_payload["calculation_results"]
    units = []
    for index, periods in enumerate(windows):
        window_id = str(index + 1)
        limit = obj(limits[window_id])
        relevant = set(strings(limit["references"]))
        relevant.update(requested_references(route["shared_actions"]))
        for identifier in shared_calculations:
            record = calculations[identifier]
            relevant.update(record.fact_ids)
            if record.rule_id:
                relevant.add(record.rule_id)
        try:
            handoff = knowledge.project_handoff("coordinator", tuple(sorted(relevant)))
        except ERPError as error:
            raise ERPError(
                error.code, f"windows.{window_id}.references：{error.message}"
            ) from error
        task = TaskContext(
            f"solver:synthesis:window:{window_id}",
            "Solver",
            f"统筹分段决策 · 第{periods[0]}–{periods[-1]}期",
            snapshot.reading.problem.title,
            "solver",
        )
        request = replace(
            original,
            system=planning_prompt("window"),
            task=task,
            schema_json=bound(window_schema(horizon, periods)),
            max_output_tokens=8000,
            distribute=True,
            user=dumps(
                {
                    **handoff,
                    **calculation_payload,
                    "generation": obj(loads(original.user))["generation"],
                    "planning_policy": obj(loads(original.user))["planning_policy"],
                    "assigned_periods": list(periods),
                    "key_prefix": f"w{window_id}-",
                    "route": {k: v for k, v in route.items() if k != "windows"},
                    "window_constraints": limit,
                    "cash_limits": [
                        {
                            "periods": list(w),
                            "limit": limits[str(i + 1)]["cash_outflow_limit"],
                            "unit": limits[str(i + 1)]["cash_unit"],
                        }
                        for i, w in enumerate(windows)
                    ],
                    "instruction": "仅返回 assigned_periods，遵守冻结路线/共享行动和本段支出限额。"
                    "共享行动由程序插入，不能重复或改写。只编制本段其它操作；"
                    "共享行动已覆盖的期可返回空数组。跨段依赖只能引用 shared- key，"
                    "不能猜测其它段的局部行动或计算 ID。计算 ID 用本段 key_prefix 开头。"
                    "context.unloaded_reference_ids 中的资料尚未读取，按需使用 "
                    "planning_lookup 按 ID 分批查询；reference_evidence 已完整提供，无需重查。"
                    "需要旧方案动作时按需查询 previous_actions。",
                }
            ),
            tool_context={
                **original.tool_context,
                "calculation_records": seed_records,
                "available_calculation_records": to_json(tuple(calculations.values())),
                "reserved_model_calls": 1 if task_budget(control, task).max_calls > 1 else 0,
                "accessed_reference_ids": sorted(provided_references(handoff)),
            },
        )
        units.append((task, request))
    # 工作线程不修改共享计算字典，等所有响应到齐后按期间顺序归并。
    with ThreadPoolExecutor(max_workers=min(4, len(units))) as executor:
        responses = list(executor.map(execute, units))
    for unit, result in zip(units, responses, strict=True):
        try:
            collect(result)
        except ERPError as error:
            report_task(control, unit[0], "failed", error=error.message)
            raise
    data = {
        **{k: route[k] for k in ("context_hash", "title", "summary", "assumptions", "risks")},
        "periods": {},
    }
    repaired: set[int] = set()
    plan = None
    while True:
        owner = None
        affected: set[str] = set()
        try:
            data["periods"] = deepcopy(shared)
            for index, ((_, request), result) in enumerate(zip(units, responses, strict=True)):
                owner = index
                part = obj(parse_response(result, knowledge.hash), {"context_hash", "periods"})
                values = obj(part["periods"])
                expected = {str(p) for p in windows[index]}
                if set(values) != expected:
                    raise ERPError(
                        "planning.window_scope",
                        f"分段只能返回指定经营期 {sorted(expected, key=int)}。",
                    )
                qualified = qualify(
                    {p: [obj(a) for a in array(v)] for p, v in values.items()},
                    f"w{index + 1}-",
                    shared_keys,
                )
                for period, actions in qualified.items():
                    data["periods"][period].extend(actions)
                material = obj(obj(loads(request.user))["context"])
                refs.update(v["id"] for k in ("facts", "rules", "issues") for v in material[k])
                local_refs = (
                    {v["id"] for k in ("facts", "rules", "issues") for v in material[k]}
                    | set(result.accessed_reference_ids)
                    | set(request.tool_context["accessed_reference_ids"])
                )
                local = deepcopy(shared)
                for period, actions in qualified.items():
                    local[period].extend(actions)
                nonempty = {p: actions for p, actions in local.items() if actions}
                local_records = {
                    **{
                        r["id"]: calculations[r["id"]]
                        for r in request.tool_context["calculation_records"]
                    },
                    **{r.id: r for r in result.calculations},
                }
                compile_decisions(
                    {**data, "periods": nonempty},
                    snapshot,
                    knowledge.hash,
                    tuple(local_records.values()),
                    local_refs,
                    required_periods=tuple(int(p) for p in nonempty),
                )
            owner = None
            plan = compile_decisions(
                data, snapshot, knowledge.hash, tuple(calculations.values()), refs
            )
            if unit_choices:
                notes = tuple(
                    f"用户确认金额单位：{a.action_id} 的 {a.amount} {a.amount_unit} "
                    f"按 {unit_choices[a.action_id]} 理解，数值不变。"
                    for a in plan.actions
                    if a.action_id in unit_choices
                )
                plan = replace(
                    plan,
                    actions=tuple(
                        replace(a, amount_unit=unit_choices[a.action_id])
                        if a.action_id in unit_choices
                        else a
                        for a in plan.actions
                    ),
                    assumptions=tuple(dict.fromkeys(plan.assumptions + notes)),
                )
            check_planning_content(plan)
            issues = tuple(
                i
                for i in DraftPlanEvaluator().evaluate(snapshot, plan).issues
                if i.severity == "error" and i.code.startswith("plan.")
            )
            if issues:
                affected = repair_periods(data, "", plan, issues)
                if affected:
                    owner = next(
                        (
                            i
                            for i, group in enumerate(windows)
                            if any(str(p) in affected for p in group)
                        ),
                        None,
                    )
                raise ERPError("planning.validation", "；".join(i.message for i in issues[:12]))
            for index, periods in enumerate(windows):
                limit = limits[str(index + 1)]
                if limit["cash_outflow_limit"] is None:
                    continue
                payments = [
                    a
                    for a in plan.actions
                    if (a.payment_period or a.period) in periods
                    and a.amount_meaning in CASH_OUTFLOW
                    and a.amount is not None
                ]
                converted, invalid = [], []
                for action in payments:
                    try:
                        converted.append(
                            money_value(action.amount, action.amount_unit, limit["cash_unit"])
                        )
                    except ERPError:
                        invalid.append(
                            {
                                "action_id": action.action_id,
                                "period": action.period,
                                "description": action.description[:160],
                                "amount": str(action.amount),
                                "unit": action.amount_unit,
                            }
                        )
                if invalid:
                    calculated = {
                        f"a-{knowledge.hash[:10]}-{action['key']}"
                        for actions in data["periods"].values()
                        for action in actions
                        if action.get("amount_calculation_id")
                    }
                    owner = index
                    raise PlanningProblem(
                        "planning.cash_unit",
                        f"分段{index + 1}现金单位无法换算为“{limit['cash_unit']}”："
                        + "；".join(
                            f"{a['action_id']}：{a['amount']} {a['unit']}" for a in invalid
                        ),
                        units[index][0].id,
                        {
                            "cash_unit": limit["cash_unit"],
                            "cash_limit": limit["cash_outflow_limit"],
                            "actions": invalid[:24],
                            "affected_count": len(invalid),
                            "unit_editable": not any(
                                a["action_id"].startswith(f"a-{knowledge.hash[:10]}-shared-")
                                or a["action_id"] in calculated
                                for a in invalid
                            )
                            and len(invalid) <= 24,
                        },
                    )
                if sum(converted) > decimal(limit["cash_outflow_limit"]):
                    owner = index
                    raise ERPError(
                        "planning.cash_limit", f"分段{index + 1}现金支出超过冻结路线限额。"
                    )
            break
        except (ERPError, KeyError) as error:
            if isinstance(error, ERPError) and error.code == "planning.context":
                report_task(
                    control,
                    units[owner][0] if owner is not None else base[0],
                    "failed",
                    error=error.message,
                )
                raise
            if owner is None:
                matches = re.findall(r"periods\.(\d+)", str(error))
                owner = next(
                    (i for i, group in enumerate(windows) if any(int(p) in group for p in matches)),
                    None,
                )
            if owner is None or owner in repaired:
                report_task(
                    control,
                    units[owner][0] if owner is not None else base[0],
                    "failed",
                    error=str(error),
                )
                raise PlanningProblem(
                    error.code
                    if isinstance(error, (PlanningProblem, PlanningValidationError))
                    else "planning.window_validation",
                    "分段合并校验失败：" + str(error),
                    units[owner][0].id if owner is not None else base[0].id,
                    error.details
                    if isinstance(error, PlanningProblem)
                    else {"validation_issues": list(error.issues)}
                    if isinstance(error, PlanningValidationError)
                    else None,
                ) from error
            repaired.add(owner)
            task, request = units[owner]
            try:
                original_part = parse_response(responses[owner], knowledge.hash)
            except ERPError:
                original_part = {"context_hash": knowledge.hash, "periods": {}}
            affected.update(re.findall(r"periods\.(\d+)", str(error)))
            affected &= {str(p) for p in windows[owner]}
            if not affected:
                affected = {str(p) for p in windows[owner]}
            prior = responses[owner]
            validation_issues = error.issues if isinstance(error, PlanningValidationError) else ()
            own_refs = provided_references(obj(loads(request.user))) | set(
                prior.accessed_reference_ids
            )
            evidence = knowledge.reference_batch(
                tuple(ref for ref in requested_references(original_part) if ref not in own_refs)
                + knowledge.repair_candidates(original_part, validation_issues)
            )
            own_refs.update(provided_references({"reference_evidence": evidence}))
            own_records = {
                **{
                    r["id"]: calculations[r["id"]]
                    for r in request.tool_context["calculation_records"]
                },
                **{r.id: r for r in prior.calculations},
            }
            repair = replace(
                request,
                schema_json=dumps(repair_schema(obj(loads(request.schema_json)), affected)),
                tool_context={
                    **request.tool_context,
                    "reserved_model_calls": 0,
                    "calculation_records": to_json(tuple(own_records.values())),
                    "accessed_reference_ids": sorted(own_refs),
                },
                user=dumps(
                    {
                        **obj(loads(request.user)),
                        "allowed_periods": sorted(affected, key=int),
                        "previous_decisions": original_part,
                        "reference_evidence": evidence,
                        **calculation_handoff(
                            own_records, requested_calculations(original_part) & own_records.keys()
                        ),
                        "diagnostics": str(error),
                        "validation_issues": list(validation_issues),
                        "repair_instruction": "仅修正本段行动，冻结路线、跨段承诺"
                        "及其它段均不可修改。",
                    }
                ),
            )
            revised = call((task, repair))
            fixed = apply_decision_repair(
                original_part, parse_response(revised, knowledge.hash), affected
            )
            own_records.update({r.id: r for r in revised.calculations})
            responses[owner] = replace(
                revised,
                content=dumps(fixed),
                calculations=tuple(own_records.values()),
                accessed_reference_ids=tuple(
                    sorted(own_refs | set(revised.accessed_reference_ids))
                ),
            )
    for unit, result in zip(units, responses, strict=True):
        report_task(control, unit[0], "succeeded", model=result.model)
        caches.append((unit, result))
    return WindowResult(plan, replace(responses[-1], content=dumps(data)), tuple(caches))
