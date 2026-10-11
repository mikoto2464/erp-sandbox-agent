import re
from dataclasses import replace

from erp_agent.domain.models import Cell, ParsedDocument, TableEvidenceContext

CELL_NUMBER = re.compile(r"[-+]?\d[\d,.]*\s*(?:%|％|万|元|万元|年|月|季|件|个)?")
LABEL_NUMBER = re.compile(r"\d+(?:\.\d+)?|[一二三四五六七八九十]+(?=年|季|期|月)")


def _row_signature(cells: list[Cell]) -> tuple[tuple[int, int, str], ...]:
    return tuple((cell.column, cell.column_span,
                  "#" if CELL_NUMBER.fullmatch(cell.text.strip()) else
                  LABEL_NUMBER.sub("#", re.sub(r"\s+", "", cell.text))) for cell in cells)


def contextualize_document(document: ParsedDocument) -> ParsedDocument:
    contexts: dict[str, TableEvidenceContext] = {}
    heading_ids: tuple[str, ...] = ()
    previous_headers: list[Cell] = []
    previous_rows: set[tuple[tuple[int, int, str], ...]] = set()
    for block in document.blocks:
        if block.kind == "paragraph":
            if block.text.strip():
                heading_ids = (block.evidence_id,)
                previous_headers = []
                previous_rows = set()
            continue
        rows: dict[int, list[Cell]] = {}
        for cell in block.cells:
            rows.setdefault(cell.row, []).append(cell)
        headers: list[Cell] = []
        for cells in rows.values():
            texts = [cell.text.strip() for cell in cells if cell.text.strip()]
            if not texts or any(CELL_NUMBER.fullmatch(text) for text in texts):
                break
            headers.extend(cells)
        inherited = (not headers and bool(previous_headers) and bool(rows)
                     and all(_row_signature(cells) in previous_rows
                             and any(text not in {"", "#"} for _, _, text in _row_signature(cells))
                             for cells in rows.values()))
        if inherited:
            headers = previous_headers
        data_rows = [cells for cells in rows.values()
                     if inherited or cells[0].row > max((h.row for h in headers), default=-1)]
        for cell in block.cells:
            column_headers = tuple(
                header.evidence_id for header in headers if (inherited or header.row < cell.row)
                and header.column < cell.column + cell.column_span
                and cell.column < header.column + header.column_span and header.text
            )
            row_ids = tuple(peer.evidence_id for peer in rows[cell.row])
            contexts[cell.evidence_id] = TableEvidenceContext(
                block.index, cell.row, cell.column, heading_ids, column_headers, row_ids,
            )
        previous_headers = headers
        previous_rows = {_row_signature(cells) for cells in data_rows}
    return replace(document, evidence=tuple(
        replace(item, table_context=contexts.get(item.id)) for item in document.evidence
    ))
