from collections import defaultdict
from dataclasses import replace

from erp_agent.application.contracts import JSONObject, to_json
from erp_agent.application.template_reader import template_material
from erp_agent.domain.models import BudgetTemplate, Fact, ProblemSnapshot


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


def planning_templates(templates: tuple[BudgetTemplate, ...]) -> tuple[JSONObject, ...]:
    return tuple(
        {
            "source": to_json(template.source),
            "sheets": [template_material(template, sheet)["sheet"] for sheet in template.sheets],
        }
        for template in templates
    )


def selected_snapshot(
    snapshot: ProblemSnapshot, templates: tuple[BudgetTemplate, ...]
) -> ProblemSnapshot:
    ids = {t.source.id for t in templates}
    return replace(
        snapshot,
        template_readings=tuple(t for t in snapshot.template_readings if t.template_id in ids),
    )
