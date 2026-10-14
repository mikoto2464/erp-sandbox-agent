from dataclasses import fields, is_dataclass

from erp_agent.domain.models import ActionKind, Issue, Plan, ProblemSnapshot

OPERATORS = {None, "eq", "ne", "gt", "gte", "lt", "lte", "exists", "completed"}
PAYMENTS = {
    "payment",
    "purchase",
    "repay",
    "recruit",
    "research",
    "train",
    "certify",
    "market_open",
    "install",
    "dismiss",
    "start_production",
}


def gap_reference(issue: Issue) -> str:
    return f"{issue.code}:{issue.target}"


def validate_actions(
    snapshot: ProblemSnapshot, plan: Plan, *, coverage: bool = True, ordering: bool = True
) -> tuple[Issue, ...]:
    problem = snapshot.reading.problem
    horizon = (
        problem.years * problem.periods_per_year
        if problem.years and problem.periods_per_year
        else None
    )
    entities = {f.entity_id for f in problem.facts} | {"company"}
    facts = {f.id: f for f in problem.facts}
    rules = {r.id: r for r in snapshot.reading.rules.rules}
    gaps = {gap_reference(i) for i in snapshot.reading.issues}
    actions = {a.action_id: a for a in plan.actions}
    indices = {a.action_id: i for i, a in enumerate(plan.actions)}
    issues: list[Issue] = []

    def error(code: str, index: int, field: str, message: str) -> None:
        action = plan.actions[index]
        evidence = tuple(
            dict.fromkeys(e for f in action.fact_ids if f in facts for e in facts[f].evidence_ids)
        )
        issues.append(
            Issue(
                "plan." + code,
                "error",
                f"actions.{index}.{field}",
                f"行动 {action.action_id} · {field}：{message}",
                evidence,
                field_path=f"actions.{index}.{field}",
                action_id=action.action_id,
                source_ids=action.fact_ids + action.rule_ids,
                impact=tuple(
                    a.action_id
                    for a in plan.actions
                    if a.action_id == action.action_id or action.action_id in a.depends_on
                ),
            )
        )

    if not plan.actions:
        issues.append(Issue("plan.empty", "error", "plan.actions", "方案没有行动。"))
    if coverage and horizon:
        missing = sorted(set(range(1, horizon + 1)) - {a.period for a in plan.actions})
        outside = [a for a in plan.actions if not 1 <= a.period <= horizon]
        if missing or outside:
            detail = []
            if missing:
                detail.append(
                    "缺失经营期："
                    + "、".join(
                        f"{p}（第{(p - 1) // problem.periods_per_year + 1}年"
                        f"第{(p - 1) % problem.periods_per_year + 1}期）"
                        for p in missing[:24]
                    )
                    + (f"；另有 {len(missing) - 24} 期" if len(missing) > 24 else "")
                )
            if outside:
                detail.append(
                    "越界行动：" + "、".join(f"{a.action_id}={a.period}" for a in outside[:8])
                )
            issues.append(
                Issue(
                    "plan.period_coverage",
                    "error",
                    "plan.actions",
                    "行动必须覆盖经营周期的每一期且不能越界。" + "；".join(detail),
                )
            )
    seen: set[str] = set()
    for index, action in enumerate(plan.actions):
        if not action.action_id.strip() or action.action_id in seen:
            error("duplicate_id", index, "action_id", "ID为空或重复。")
        seen.add(action.action_id)
        if (
            type(action.period) is not int
            or action.period < 1
            or (horizon and action.period > horizon)
        ):
            error("period", index, "period", "行动期间无效或超出经营周期。")
        if ordering and index and action.period < plan.actions[index - 1].period:
            error("action_order", index, "period", "行动必须按经营期递增排列。")
        if action.entity_id not in entities:
            error("entity", index, "entity_id", "行动引用未知实体。")
        if action.amount is not None and (not action.amount.is_finite() or action.amount < 0):
            error("amount", index, "amount", "行动金额必须是非负有限数。")
        if action.quantity is not None and (
            type(action.quantity) is not int or action.quantity < 0
        ):
            error("quantity", index, "quantity", "数量必须是非负整数。")
        # 历史文字不提升为新契约事实。
        if action.schema_version != "2":
            continue
        if action.kind not in {k.value for k in ActionKind}:
            error("kind", index, "kind", "未支持类型必须显式使用 extension 或 unknown。")
        if action.amount is not None and (
            not action.amount_unit or action.amount_meaning in {"none", "unknown"}
        ):
            error("amount_unit", index, "amount", "有金额必须明确单位与金额含义。")
        if action.quantity is not None and not action.quantity_unit:
            error("quantity_unit", index, "quantity_unit", "有数量必须明确单位。")
        if (action.amount is not None or action.quantity is not None) and not (
            action.fact_ids or action.rule_ids
        ):
            error("numeric_source", index, "fact_ids", "数量或金额必须关联依据，不可只写入说明。")
        if (action.amount is None or action.quantity is None) and not action.missing_reason.strip():
            error("missing_reason", index, "missing_reason", "未知或不适用数值须说明原因。")
        if action.kind == "review" and (
            action.amount is not None
            or action.quantity is not None
            or action.amount_meaning != "none"
        ):
            error("review_value", index, "amount_meaning", "检查不产生金额或数量。")
        if action.kind in {"bid", "contract", "delivery"} and action.amount_meaning in {
            "receipt",
            "payment",
        }:
            error("cash_meaning", index, "amount_meaning", "竞单、合同或交付不能冒充现金收付。")
        if action.kind == "receipt" and action.amount_meaning not in {"receipt", "unknown"}:
            error("cash_meaning", index, "amount_meaning", "收款金额必须表示 receipt。")
        if action.kind == "borrow" and action.amount_meaning not in {
            "financing_principal",
            "unknown",
        }:
            error("cash_meaning", index, "amount_meaning", "借款金额必须表示融资本金。")
        if action.kind == "repay" and action.amount_meaning not in {
            "repayment_principal",
            "payment",
            "unknown",
        }:
            error("cash_meaning", index, "amount_meaning", "还款不能表示成本、收入或参考金额。")
        if action.amount_meaning == "payment" and action.kind not in PAYMENTS:
            error("cash_meaning", index, "amount_meaning", "该动作不是付款动作。")
        for key, references, allowed in (
            ("fact_ids", action.fact_ids, facts),
            ("rule_ids", action.rule_ids, rules),
            ("gap_ids", action.gap_ids, gaps),
            ("related_entity_ids", action.related_entity_ids, entities),
        ):
            for reference in references:
                if reference not in allowed:
                    error("reference", index, key, f"未知引用 {reference}。")
        for key in ("completion_period", "payment_period"):
            period = getattr(action, key)
            if period is not None and (
                type(period) is not int or period < action.period or (horizon and period > horizon)
            ):
                error("time", index, key, "期间早于动作开始或超出经营周期。")
            if period is not None and not (action.fact_ids or action.rule_ids):
                error("time_source", index, key, "已知期间必须关联事实或规则。")
        if is_dataclass(action.details):
            for field in fields(action.details):
                value = getattr(action.details, field.name)
                if "entity_id" in field.name:
                    references = value if isinstance(value, tuple) else (value,) if value else ()
                    for reference in references:
                        if reference not in entities:
                            error(
                                "detail_entity",
                                index,
                                "details." + field.name,
                                f"未知实体 {reference}。",
                            )
                if field.name == "obligation_action_id" and value and value not in actions:
                    error("detail_action", index, "details." + field.name, f"未知行动 {value}。")
        else:
            error("details", index, "details", "缺少该动作的类型特定详情。")
        for dependency in action.depends_on:
            previous = actions.get(dependency)
            if previous is None or dependency == action.action_id:
                error("dependency", index, "depends_on", f"无效前置行动 {dependency}。")
            elif (previous.completion_period or previous.period) > action.period or (
                ordering and previous.period == action.period and indices[dependency] >= index
            ):
                error(
                    "dependency_time",
                    index,
                    "depends_on",
                    f"前置 {dependency} 尚未完成或在本动作之后。",
                )
        path = set()
        pending = list(action.depends_on)
        while pending:
            dependency = pending.pop()
            if dependency == action.action_id:
                error("dependency_cycle", index, "depends_on", "前置行动形成环。")
                break
            if dependency in path:
                continue
            path.add(dependency)
            previous = actions.get(dependency)
            if previous:
                pending.extend(previous.depends_on)
        condition = action.condition
        allowed = {"fact": facts, "rule": rules, "action": actions, "gap": gaps}
        if condition.operator not in OPERATORS or condition.kind not in {"always", *allowed}:
            error("condition", index, "condition", "条件类型或运算符无效。")
        elif condition.kind != "always" and condition.reference_id not in allowed[condition.kind]:
            error("condition", index, "condition.reference_id", "条件引用未知对象。")
        elif condition.kind == "always" and any(
            (condition.reference_id, condition.operator, condition.value)
        ):
            error("condition", index, "condition", "无条件动作不能带隐含判断。")
    return tuple(issues)
