import re
from dataclasses import replace

from erp_agent.domain.models import Cell, ParsedDocument, TableEvidenceContext

CELL_NUMBER = re.compile(r"[-+]?\d[\d,.]*\s*(?:%|％|万|元|万元|年|月|季|件|个)?")


def contextualize_document(document: ParsedDocument) -> ParsedDocument:
    contexts: dict[str, TableEvidenceContext] = {}
    heading_ids: tuple[str, ...] = ()
    for block in document.blocks:
        if block.kind == "paragraph":
            if block.text:
                heading_ids = (block.evidence_id,)
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
        for cell in block.cells:
            column_headers = tuple(
                header.evidence_id for header in headers if header.row < cell.row
                and header.column < cell.column + cell.column_span
                and cell.column < header.column + header.column_span and header.text
            )
            row_ids = tuple(peer.evidence_id for peer in rows[cell.row])
            contexts[cell.evidence_id] = TableEvidenceContext(
                block.index, cell.row, cell.column, heading_ids, column_headers, row_ids,
            )
    return replace(document, evidence=tuple(
        replace(item, table_context=contexts.get(item.id)) for item in document.evidence
    ))
