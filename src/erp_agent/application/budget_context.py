from erp_agent.application.contracts import JSONObject, JSONValue, to_json
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.models import BudgetTemplate


def budget_material(templates: tuple[BudgetTemplate, ...]) -> tuple[JSONObject, ...]:
    result: list[JSONObject] = []
    for template in templates:
        sheets: list[JSONValue] = []
        for sheet in template.sheets:
            cells: list[JSONValue] = []
            blanks: list[JSONValue] = []
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
            sheets.append({"name": sheet.name, "rows": sheet.rows, "columns": sheet.columns,
                           "cells": cells, "blank_ranges": blanks,
                           "merged_ranges": to_json(sheet.merged_ranges),
                           "hidden": sheet.hidden, "protected": sheet.protected})
        result.append({"source": to_json(template.source), "sheets": sheets,
                       "warnings": to_json(template.warnings)})
    return tuple(result)
