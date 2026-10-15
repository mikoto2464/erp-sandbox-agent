from erp_agent.application.contracts import JSONObject, JSONValue, to_json
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.models import BudgetSheet, BudgetTemplate, Plan, ProblemSnapshot
from erp_agent.domain.workbook import writable_target


def budget_material(
    templates: tuple[BudgetTemplate, ...], *, include_formula_references: bool = False
) -> tuple[JSONObject, ...]:
    result: list[JSONObject] = []
    for template in templates:
        sheets: list[JSONValue] = []
        input_positions = {
            sheet.name: tuple(
                cell_position(c.coordinate)
                for c in sheet.cells
                if not c.text and c.writable and not sheet.hidden
            )
            for sheet in template.sheets
        }
        for sheet in template.sheets:
            cells: list[JSONValue] = []
            blanks: list[JSONValue] = []
            inputs: list[JSONValue] = []
            previous: tuple[int, int, str, bool] | None = None
            first = ""
            for cell in sheet.cells:
                if cell.text:
                    cells.append(
                        [
                            cell.coordinate,
                            cell.text,
                            cell.number_format,
                            cell.formula,
                            cell.writable,
                        ]
                    )
                    previous = None
                    continue
                position = cell_position(cell.coordinate)
                if position is None:
                    continue
                row, column = position
                current = (row, column, cell.number_format, cell.writable)
                if previous == (row, column - 1, cell.number_format, cell.writable):
                    blanks[-1] = [f"{first}:{cell.coordinate}", cell.number_format, cell.writable]
                else:
                    first = cell.coordinate
                    blanks.append([cell.coordinate, cell.number_format, cell.writable])
                previous = current
            previous_input: tuple[int, int, str] | None = None
            input_first = ""
            cell_index = {c.coordinate: c for c in sheet.cells}
            for cell in sheet.cells:
                if not writable_target(sheet, cell.coordinate, cell_index):
                    continue
                position = cell_position(cell.coordinate)
                assert position is not None
                row, column = position
                if inputs and previous_input == (row, column - 1, cell.number_format):
                    inputs[-1] = [f"{input_first}:{cell.coordinate}", cell.number_format]
                else:
                    input_first = cell.coordinate
                    inputs.append([cell.coordinate, cell.number_format])
                previous_input = (row, column, cell.number_format)
            references: dict[tuple[str, str], list[str]] = {}
            for reference in sheet.formula_references if include_formula_references else ():
                start, _, end = reference.range.partition(":")
                reference_first, reference_last = cell_position(start), cell_position(end or start)
                if (
                    not reference_first
                    or not reference_last
                    or not any(
                        p
                        and reference_first[0] <= p[0] <= reference_last[0]
                        and reference_first[1] <= p[1] <= reference_last[1]
                        for p in input_positions.get(reference.sheet, ())
                    )
                ):
                    continue
                references.setdefault((reference.formula_cell, reference.sheet), []).append(
                    reference.range
                )
            sheets.append(
                {
                    "name": sheet.name,
                    "rows": sheet.rows,
                    "columns": sheet.columns,
                    "cells": cells,
                    "blank_ranges": blanks,
                    "merged_ranges": to_json(sheet.merged_ranges),
                    "input_ranges": inputs,
                    "formula_references": [
                        [cell, target, ranges] for (cell, target), ranges in references.items()
                    ],
                    "validations": to_json(sheet.validations),
                    "hidden": sheet.hidden,
                    "protected": sheet.protected,
                }
            )
        result.append(
            {
                "source": to_json(template.source),
                "sheets": sheets,
                "warnings": to_json(template.warnings),
            }
        )
    return tuple(result)


def budget_task_material(
    snapshot: ProblemSnapshot, plan: Plan, template: BudgetTemplate, sheet: BudgetSheet, groups
) -> JSONObject:
    """Collect action-linked facts and checked groups for one sheet."""
    facts = snapshot.reading.problem.facts
    entity_names = {f.entity_name: f.entity_id for f in facts if f.entity_name}
    matched = {
        entity
        for name, entity in entity_names.items()
        if any(name == group.label or name in group.description for group in groups)
    }
    selected_actions = (
        tuple(
            a
            for a in plan.actions
            if a.entity_id in matched or matched.intersection(a.related_entity_ids)
        )
        if matched
        else plan.actions
    )
    action_index = {a.action_id: a for a in plan.actions}
    dependency_ids = {dependency for a in selected_actions for dependency in a.depends_on}
    while dependency_ids - {a.action_id for a in selected_actions}:
        added = tuple(
            action_index[identifier]
            for identifier in dependency_ids
            if identifier in action_index
            and identifier not in {a.action_id for a in selected_actions}
        )
        if not added:
            break
        selected_actions += added
        dependency_ids.update(identifier for a in added for identifier in a.depends_on)
    selected_ids = {a.action_id for a in selected_actions}
    selected_actions = tuple(a for a in plan.actions if a.action_id in selected_ids)
    entities = (
        {a.entity_id for a in selected_actions}
        | {entity for a in selected_actions for entity in a.related_entity_ids}
        | matched
    )
    requested = {fid for a in selected_actions for fid in a.fact_ids}
    known_entities = {f.entity_id for f in facts}
    # Entity values are normalized references; only exact references add neighbors.
    changed = True
    while changed:
        changed = False
        for fact in facts:
            if fact.id in requested or fact.entity_id in entities:
                requested.add(fact.id)
                if fact.value in known_entities and fact.value not in entities:
                    entities.add(fact.value)
                    changed = True
    selected = tuple(f for f in facts if f.id in requested)
    rule_ids = {rid for a in selected_actions for rid in a.rule_ids}
    evidence_ids = {e for f in selected for e in f.evidence_ids}
    rules = tuple(
        r
        for r in snapshot.reading.rules.rules
        if r.id in rule_ids or evidence_ids.intersection(r.evidence_ids)
    )
    return {
        "problem_version": snapshot.version,
        "periods": {
            "years": snapshot.reading.problem.years,
            "periods_per_year": snapshot.reading.problem.periods_per_year,
        },
        "actions": to_json(selected_actions),
        "facts": to_json(selected),
        "rules": to_json(rules),
        "template_id": template.source.id,
        "template_sha256": template.source.sha256,
        "sheet": sheet.name,
        "input_groups": to_json(groups),
        "calculations": [
            {
                "id": r.id,
                "formula": r.formula,
                "unit": r.unit,
                "period": r.period,
                "fact_ids": list(r.fact_ids),
                "rule_id": r.rule_id,
                "assumption": r.assumption,
            }
            for r in (plan.preparation.calculations if plan.preparation else ())
            if not r.fact_ids or requested.intersection(r.fact_ids)
        ],
        "context_scope": "Current-sheet linked facts only; omitted facts are not missing.",
    }
