from collections import defaultdict
from dataclasses import fields, is_dataclass, replace

from erp_agent.application.contracts import JSONObject, to_json
from erp_agent.domain.models import (
    Action,
    BudgetTemplate,
    Fact,
    PlanningContext,
    PlanTone,
    ProblemSnapshot,
)


def entity_catalog(snapshot: ProblemSnapshot) -> list[JSONObject]:
    grouped: dict[str, tuple[set[str], set[str]]] = {}
    for fact in snapshot.reading.problem.facts:
        names, categories = grouped.setdefault(fact.entity_id, (set(), set()))
        names.add(fact.entity_name)
        categories.add(fact.category)
    return [
        {"id": identifier, "names": sorted(names), "categories": sorted(categories)}
        for identifier, (names, categories) in sorted(grouped.items())
    ] + ([] if "company" in grouped else [{"id": "company", "names": ["本企业"], "categories": []}])


def repair_material(
    snapshot: ProblemSnapshot, actions: tuple[Action, ...], objects: list[JSONObject]
) -> JSONObject:
    """只传补丁对象引用的事实及前置行动；目录区分实体和模板。"""
    identifiers: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            identifiers.add(value)

    collect(objects)
    index = {a.action_id: a for a in actions}
    selected = set(index).intersection(identifiers)
    pending = list(selected)
    while pending:
        action = index[pending.pop()]
        for identifier in action.depends_on:
            if identifier in index and identifier not in selected:
                selected.add(identifier)
                pending.append(identifier)
    related = tuple(a for a in actions if a.action_id in selected)
    scoped = role_snapshot(
        snapshot,
        "repair",
        related,
        fact_ids=tuple(f.id for f in snapshot.reading.problem.facts if f.id in identifiers),
        rule_ids=tuple(r.id for r in snapshot.reading.rules.rules if r.id in identifiers),
    )
    return {
        "problem": problem_material(scoped),
        "rules": to_json(scoped.reading.rules),
        "related_actions": action_material(related),
        "dependency_catalog": [
            [a.action_id, a.period, a.completion_period, list(a.depends_on)] for a in actions
        ],
        "entity_catalog": entity_catalog(snapshot),
        "reference_catalog": {
            "fact_ids": [f.id for f in snapshot.reading.problem.facts],
            "rule_ids": [r.id for r in snapshot.reading.rules.rules],
            "action_ids": list(index),
            "gap_ids": [f"{i.code}:{i.target}" for i in snapshot.reading.issues],
        },
    }


def planning_policy(context: PlanningContext) -> JSONObject:
    tones = {
        PlanTone.CONSERVATIVE: "保守：优先现金安全与履约，控制借款、扩产和前期投入，说明收益取舍。",
        PlanTone.BALANCED: "平稳：兼顾现金周转与增长，分阶段投入，给出适度的风险缓冲。",
        PlanTone.AGGRESSIVE: "激进：在题目硬约束内争取增长与得分，说明融资和投入压力及退路。",
        PlanTone.CUSTOM: "自定义：采用 custom_prompt 中的偏好，仍遵守题目与已确认规则。",
    }
    return {
        "purpose": "经营行动草稿；制定候选策略，不要求唯一解或已验证经营可行性。",
        "tone_instruction": tones[context.config.tone],
        "decision_scope": "竞单目标、产品路线和投入安排由产销角色根据题目与用户偏好提出；"
        "用户未指定目标或订单池没有已签合同，不构成规划输入缺失。",
        "fact_boundary": "拟定数量、投入和期间是候选决策，须引用容量、价格、需求或提前期依据，"
        "不能作为已发生经营事实。合同获得、市场准入、初始存量未知时不得假定已满足或为零。",
        "missing_data": "先用已知事实形成有依据的候选与关联建议；未知值保持 null 和明确原因，"
        "受影响动作说明执行前核对条件。仍有可用的有引用结论时返回 partial 并定位剩余缺口；"
        "只有无法形成任何有依据的行动或建议时返回 missing_input。",
    }


def problem_material(snapshot: ProblemSnapshot, *, order_details: bool = True) -> JSONObject:
    problem = snapshot.reading.problem
    included = tuple(f for f in problem.facts if order_details or f.category != "order_pool")
    grouped: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for fact in included:
        grouped[(fact.category, fact.entity_id)].append(fact)
    layouts: dict[tuple[str, tuple[str, ...]], list[list[Fact]]] = defaultdict(list)
    for (category, _), facts in grouped.items():
        keys = tuple(sorted(f.key for f in facts))
        if (
            len(keys) >= 4
            and len(set(keys)) == len(keys)
            and len({f.entity_name for f in facts}) == 1
        ):
            layouts[(category, keys)].append(sorted(facts, key=lambda f: f.key))
    tables = []
    tabulated = set()
    evidence_index: dict[str, str] = {}
    for (category, keys), groups in layouts.items():
        if len(groups) < 4:
            continue
        attributes = ("value_type", "unit", "state", "conversion")
        defaults = [
            {
                name: to_json(getattr(groups[0][index], name))
                for name in attributes
                if all(
                    getattr(row[index], name) == getattr(groups[0][index], name) for row in groups
                )
            }
            for index in range(len(keys))
        ]
        rows = []
        for group in groups:
            references = []
            overrides = {}
            for index, fact in enumerate(group):
                tabulated.add(fact.id)
                refs = []
                for evidence_id in fact.evidence_ids:
                    evidence_index.setdefault(evidence_id, f"e{len(evidence_index)}")
                    refs.append(evidence_index[evidence_id])
                references.append(refs)
                metadata = {
                    name: to_json(getattr(fact, name))
                    for name in attributes
                    if name not in defaults[index]
                }
                if fact.explanation:
                    metadata["explanation"] = fact.explanation
                if metadata:
                    overrides[str(index)] = metadata
            rows.append(
                [
                    group[0].entity_id,
                    group[0].entity_name,
                    [f.value for f in group],
                    [f.id for f in group],
                    references,
                    overrides,
                ]
            )
        tables.append(
            {
                "category": category,
                "columns": list(keys),
                "column_defaults": defaults,
                "row_columns": [
                    "entity_id",
                    "entity_name",
                    "values",
                    "fact_ids",
                    "evidence_refs",
                    "cell_overrides",
                ],
                "rows": rows,
            }
        )
    return {
        "title": problem.title,
        "years": problem.years,
        "periods_per_year": problem.periods_per_year,
        "calendar": {
            "total_periods": problem.years * problem.periods_per_year,
            "final_period": problem.years * problem.periods_per_year,
            "year_ranges": [
                [year, (year - 1) * problem.periods_per_year + 1, year * problem.periods_per_year]
                for year in range(1, problem.years + 1)
            ],
        }
        if problem.years and problem.periods_per_year
        else None,
        "fact_columns": [
            "id",
            "category",
            "entity_id",
            "entity_name",
            "key",
            "value",
            "value_type",
            "unit",
            "state",
            "explanation",
            "conversion",
            "evidence_ids",
        ],
        "facts": [
            [
                f.id,
                f.category,
                f.entity_id,
                f.entity_name,
                f.key,
                f.value,
                f.value_type,
                f.unit,
                f.state.value,
                f.explanation,
                f.conversion,
                list(f.evidence_ids),
            ]
            for f in included
            if f.id not in tabulated
        ],
        "fact_tables": tables,
        "table_evidence_index": {short: original for original, short in evidence_index.items()},
        "deferred_order_pool": None
        if order_details
        else {
            "entities": len({f.entity_id for f in problem.facts if f.category == "order_pool"}),
            "instruction": "完整订单池由产销及总协调角色处理，本任务不重复明细；"
            "订单未缺失、不等于已获合同，勿在本任务重新竞单规划。",
        },
    }


def planning_requirements(snapshot: ProblemSnapshot) -> list[JSONObject]:
    """规划只消费 Reader 已核对的业务输入需求，不传原格、公式或写入坐标。"""
    result = []
    for reading in snapshot.template_readings:
        sheets = []
        for sheet in reading.sheets:
            parameters = tuple(
                dict.fromkeys(
                    (group.label, group.unit, group.description, group.depends_on_sheets)
                    for group in sheet.input_groups
                )
            )
            sheets.append(
                {
                    "name": sheet.name,
                    "purpose": sheet.purpose,
                    "periods": sheet.periods,
                    "required_parameters": [
                        {
                            "label": label,
                            "unit": unit,
                            "description": description,
                            "depends_on_sheets": list(dependencies),
                        }
                        for label, unit, description, dependencies in parameters
                    ],
                }
            )
        result.append(
            {
                "template_id": reading.template_id,
                "sha256": reading.sha256,
                "sheets": sheets,
                "explanation": reading.explanation,
                "issue_reviews": to_json(reading.issue_reviews),
                "scope": "仅为输出所需业务参数，不是题目事实或已实现的计算规则；"
                "未知派生值保留缺口。",
            }
        )
    return result


def selected_snapshot(
    snapshot: ProblemSnapshot, templates: tuple[BudgetTemplate, ...]
) -> ProblemSnapshot:
    ids = {t.source.id for t in templates}
    return replace(
        snapshot,
        template_readings=tuple(t for t in snapshot.template_readings if t.template_id in ids),
    )


ROLE_CATEGORIES = {
    "operations": {
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
        "demand_forecast",
        "order_pool",
        "order_contract",
    },
    "finance": {"initial", "finance", "discount", "carbon", "digital", "budget"},
    "budget": {"initial", "budget"},
    "coordinator": {"initial", "finance", "discount"},
    "report": {"initial", "finance", "discount"},
}


def action_material(actions: tuple[Action, ...]) -> JSONObject:
    return {
        "action_columns": [
            "action_id",
            "period",
            "kind",
            "entity_id",
            "quantity",
            "quantity_unit",
            "amount",
            "amount_unit",
            "amount_meaning",
            "related_entity_ids",
            "depends_on",
            "completion_period",
            "payment_period",
            "condition",
            "fact_ids",
            "rule_ids",
            "gap_ids",
            "missing_reason",
            "description",
            "details",
        ],
        "actions": [
            [
                getattr(action, key)
                for key in (
                    "action_id",
                    "period",
                    "kind",
                    "entity_id",
                    "quantity",
                    "quantity_unit",
                    "amount",
                    "amount_unit",
                    "amount_meaning",
                    "related_entity_ids",
                    "depends_on",
                    "completion_period",
                    "payment_period",
                    "condition",
                    "fact_ids",
                    "rule_ids",
                    "gap_ids",
                    "missing_reason",
                    "description",
                    "details",
                )
            ]
            for action in actions
        ],
    }


def role_snapshot(
    snapshot: ProblemSnapshot,
    role: str,
    actions: tuple[Action, ...] = (),
    *,
    fact_ids: tuple[str, ...] = (),
    rule_ids: tuple[str, ...] = (),
) -> ProblemSnapshot:
    """用实体关系和显式引用闭包补齐角色事实，裁剪不产生缺值。"""
    facts = snapshot.reading.problem.facts
    categories = ROLE_CATEGORIES.get(role, set())
    selected = set(fact_ids) | {identifier for action in actions for identifier in action.fact_ids}
    entities = {action.entity_id for action in actions} | {
        e for a in actions for e in a.related_entity_ids
    }
    for action in actions:
        if is_dataclass(action.details):
            for field in fields(action.details):
                if "entity_id" in field.name:
                    value = getattr(action.details, field.name)
                    entities.update(
                        value if isinstance(value, tuple) else (value,) if value else ()
                    )
    entities |= {f.entity_id for f in facts if f.id in selected}
    for fact in facts:
        if fact.category in categories:
            selected.add(fact.id)
    # 未结构化分类的事实保留，避免遗漏税费、结算或新机制约束。
    if role in {"operations", "finance", "coordinator"}:
        selected.update(f.id for f in facts if f.category == "other")
    # 事实中实体 ID 关系（BOM、订单、资格、提前期）可遍历，不按名称关键词猜。
    changed = True
    known_entities = {f.entity_id for f in facts}
    while changed:
        changed = False
        for fact in facts:
            if fact.id in selected or fact.entity_id in entities:
                if (
                    role != "operations"
                    and fact.category == "order_pool"
                    and fact.id not in selected
                ):
                    continue
                if fact.id not in selected:
                    selected.add(fact.id)
                    changed = True
                if fact.value in known_entities and fact.value not in entities:
                    entities.add(fact.value)
                    changed = True
    rules = set(rule_ids) | {r for a in actions for r in a.rule_ids}
    evidence = {e for f in facts if f.id in selected for e in f.evidence_ids}
    included_rules = tuple(
        r
        for r in snapshot.reading.rules.rules
        if role in {"operations", "finance", "coordinator"}
        or r.id in rules
        or (evidence & set(r.evidence_ids))
    )
    return replace(
        snapshot,
        reading=replace(
            snapshot.reading,
            problem=replace(
                snapshot.reading.problem, facts=tuple(f for f in facts if f.id in selected)
            ),
            rules=replace(snapshot.reading.rules, rules=included_rules),
        ),
    )


def role_material(
    snapshot: ProblemSnapshot,
    role: str,
    actions: tuple[Action, ...] = (),
    *,
    fact_ids: tuple[str, ...] = (),
    rule_ids: tuple[str, ...] = (),
) -> JSONObject:
    focused = role_snapshot(snapshot, role, actions, fact_ids=fact_ids, rule_ids=rule_ids)
    material = problem_material(focused)
    if role != "operations":
        material["deferred_order_pool"] = {
            "instruction": "只传当前行动或显式引用的订单事实，其他明细由产销持有；裁剪不代表缺失。",
        }
    return {
        "project_id": snapshot.project_id,
        "problem_version": snapshot.version,
        "rule_version": snapshot.reading.rules.version,
        "problem": material,
        "rules": to_json(focused.reading.rules),
        "gaps": [
            {"gap_id": f"{i.code}:{i.target}", **to_json(i)}
            for i in snapshot.reading.issues
            if i.severity != "info"
        ],
        "scope_instruction": "这是当前职责的相关事实；"
        "被裁剪资料仍保存在权威快照中，不能视为题目缺失。"
        "证据可通过 evidence_ids 还原。数值与期间未知时保留 null 并关联现有缺口。",
    }
