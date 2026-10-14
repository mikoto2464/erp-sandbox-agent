"""模型映射列语义，原始行值由 Python 展开，不重复誊写订单明细。"""

from decimal import Decimal, InvalidOperation

from erp_agent.application.contracts import (
    TEXT,
    TEXTS,
    JSONObject,
    JSONValue,
    array,
    integer,
    obj,
    object_schema,
    string,
    strings,
)
from erp_agent.application.reading.batches import ReadingBatch
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Cell

ROW_CATEGORIES = ("bom", "demand_forecast", "order_pool", "order_contract")
TABLE_MAPPING_SCHEMA: JSONObject = {
    "type": "array",
    "items": object_schema(
        {
            "block_index": {"type": "integer", "minimum": 0},
            "category": {"type": "string", "enum": list(ROW_CATEGORIES)},
            "row_start": {"type": "integer", "minimum": 0},
            "row_end": {"type": "integer", "minimum": 0},
            "columns": {
                "type": "array",
                "items": object_schema(
                    {
                        "column": {"type": "integer", "minimum": 0},
                        "key": TEXT,
                        "value_type": {"type": "string", "enum": ["text", "number", "integer"]},
                        "unit": TEXT,
                        "conversion": {
                            "type": "string",
                            "enum": ["identity", "percent_to_ratio", "wan_to_yuan"],
                        },
                        "header_ids": TEXTS,
                    }
                ),
            },
        }
    ),
}


def expand_table_mappings(value: JSONValue, batch: ReadingBatch) -> list[JSONObject]:
    sources = {source.id: source for source in batch.document.evidence}
    focused = set(batch.focus_ids)
    facts: list[JSONObject] = []
    assigned: set[str] = set()
    for item in array(value):
        mapping = obj(item, {"block_index", "category", "row_start", "row_end", "columns"})
        block_index = integer(mapping["block_index"], 0, 1_000_000)
        category = string(mapping["category"])
        start, end = (integer(mapping[key], 0, 1_000_000) for key in ("row_start", "row_end"))
        block = next(
            (b for b in batch.document.blocks if b.kind == "table" and b.index == block_index), None
        )
        if block is None or category not in ROW_CATEGORIES or start > end:
            raise ERPError("invalid_contract", "逐行表映射的表格、类别或行范围无效。")
        rows = sorted({c.row for c in block.cells if start <= c.row <= end})
        selected = [c for c in block.cells if start <= c.row <= end]
        row_cells: dict[int, list[Cell]] = {}
        for cell in selected:
            row_cells.setdefault(cell.row, []).append(cell)
        if (
            not rows
            or rows[0] != start
            or rows[-1] != end
            or any(c.evidence_id not in focused or c.column_span != 1 for c in selected)
        ):
            raise ERPError("reading_batch_source", "逐行表映射超出当前完整行范围或涉及横向合并格。")
        columns: dict[int, JSONObject] = {}
        keys: set[str] = set()
        for raw in array(mapping["columns"]):
            column = obj(raw, {"column", "key", "value_type", "unit", "conversion", "header_ids"})
            index = integer(column["column"], 0, 1_000_000)
            key = string(column["key"])
            refs = tuple(
                batch.source_aliases.get(ref, ref) for ref in strings(column["header_ids"])
            )
            headers = [sources.get(ref) for ref in refs]
            applicable_headers = {
                ref
                for cell in selected
                if cell.column == index and sources[cell.evidence_id].table_context
                for ref in sources[cell.evidence_id].table_context.column_header_ids
            }
            if (
                index in columns
                or not key
                or key in keys
                or not refs
                or any(
                    header is None
                    or not header.table_context
                    or header.id not in applicable_headers
                    or header.table_context.block_index != block_index
                    or header.table_context.row >= start
                    or not any(
                        c.evidence_id == header.id and c.column <= index < c.column + c.column_span
                        for c in block.cells
                    )
                    for header in headers
                )
            ):
                raise ERPError("reading_batch_source", "逐行表映射列重复或表头不属于当前数据列。")
            columns[index] = {**column, "header_ids": list(refs)}
            keys.add(key)
        if any(c.text.strip() and c.column not in columns for c in selected):
            raise ERPError(
                "invalid_contract", "逐行表映射遗漏非空数据列；特殊列应单独提取，不得省略。"
            )
        for row in rows:
            cells = row_cells[row]
            if not any(c.text.strip() or c.vertical_merge == "continue" for c in cells):
                continue
            for cell in cells:
                column = columns.get(cell.column)
                if column is None:
                    continue
                source = cell
                if cell.vertical_merge == "continue":
                    parents = [
                        c
                        for c in block.cells
                        if c.column == cell.column and c.row < row and c.vertical_merge == "restart"
                    ]
                    if parents:
                        source = max(parents, key=lambda c: c.row)
                if cell.evidence_id in assigned:
                    raise ERPError("invalid_contract", "逐行表映射范围重复。")
                assigned.add(cell.evidence_id)
                text = source.text.strip()
                conversion = string(column["conversion"])
                kind = string(column["value_type"])
                if (
                    kind not in {"text", "number", "integer"}
                    or conversion not in {"identity", "percent_to_ratio", "wan_to_yuan"}
                    or (kind == "text" and conversion != "identity")
                ):
                    raise ERPError("invalid_contract", "逐行表映射数据类型或转换无效。")
                # 显式百分号由 recover_numbers 转一次；纯数值按已声明单位转换。
                if (
                    text
                    and kind != "text"
                    and conversion != "identity"
                    and not text.endswith(("%", "％"))
                ):
                    try:
                        number = Decimal(text.replace(",", ""))
                        text = format(
                            number
                            * (
                                Decimal("0.01")
                                if conversion == "percent_to_ratio"
                                else Decimal("10000")
                            ),
                            "f",
                        )
                    except InvalidOperation:
                        pass  # 保留原值，由统一数值解码生成定位错误。
                refs = list(
                    dict.fromkeys(
                        [cell.evidence_id, source.evidence_id] + array(column["header_ids"])
                    )
                )
                facts.append(
                    {
                        "id": f"table_{block_index}_{row}_{cell.column}",
                        "category": category,
                        "entity_id": f"table_{block_index}_row_{row}",
                        "entity_name": f"{batch.document.source.filename} · 行 {row + 1}",
                        "key": column["key"],
                        "value": text or None,
                        "value_type": kind,
                        "unit": column["unit"],
                        "state": "known" if text else "missing",
                        "evidence_ids": refs,
                        "conversion": conversion,
                        "explanation": "",
                    }
                )
    return facts
