from dataclasses import replace

from erp_agent.application.contracts import JSONObject, to_json
from erp_agent.application.template_reader import template_material
from erp_agent.domain.models import BudgetTemplate, ProblemSnapshot


def problem_material(snapshot: ProblemSnapshot) -> JSONObject:
    problem = snapshot.reading.problem
    return {
        "title": problem.title,
        "years": problem.years,
        "periods_per_year": problem.periods_per_year,
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
            for f in problem.facts
        ],
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
