from erp_agent.application.contracts import JSONObject, JSONValue, to_json
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.models import BudgetTemplate
from erp_agent.domain.workbook import writable_target


def budget_material(templates: tuple[BudgetTemplate, ...], *,
                    include_formula_references: bool = False) -> tuple[JSONObject, ...]:
    result: list[JSONObject] = []
    for template in templates:
        sheets: list[JSONValue] = []
        input_positions = {
            sheet.name: tuple(cell_position(c.coordinate) for c in sheet.cells
                              if not c.text and c.writable and not sheet.hidden)
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
                    cells.append([cell.coordinate, cell.text, cell.number_format,
                                  cell.formula, cell.writable])
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
                if not reference_first or not reference_last or not any(
                    p and reference_first[0] <= p[0] <= reference_last[0]
                    and reference_first[1] <= p[1] <= reference_last[1]
                    for p in input_positions.get(reference.sheet, ())
                ):
                    continue
                references.setdefault((reference.formula_cell, reference.sheet), []).append(reference.range)
            sheets.append({"name": sheet.name, "rows": sheet.rows, "columns": sheet.columns,
                           "cells": cells, "blank_ranges": blanks,
                           "merged_ranges": to_json(sheet.merged_ranges),
                           "input_ranges": inputs,
                           "formula_references": [[cell, target, ranges] for (cell, target), ranges in references.items()],
                           "validations": to_json(sheet.validations),
                           "hidden": sheet.hidden, "protected": sheet.protected})
        result.append({"source": to_json(template.source), "sheets": sheets,
                       "warnings": to_json(template.warnings)})
    return tuple(result)
