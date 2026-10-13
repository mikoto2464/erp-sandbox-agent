from decimal import Decimal

import xlrd

from erp_agent.adapters.documents import DOC_SIGNATURE
from erp_agent.domain.errors import ERPError
from erp_agent.domain.evidence import contextualize_document
from erp_agent.domain.models import Cell, DocumentBlock, Evidence, ParsedDocument, SourceDocument


class XLSDocumentParser:
    def parse(self, source: SourceDocument, content: bytes) -> ParsedDocument:
        if not content.startswith(DOC_SIGNATURE):
            raise ERPError("invalid_signature", "文件不是有效的二进制 XLS，请检查格式。")
        try:
            workbook = xlrd.open_workbook(file_contents=content, formatting_info=True,
                                          on_demand=True)
        except (xlrd.XLRDError, ValueError, IndexError, EOFError) as exc:
            raise ERPError("invalid_xls", "XLS 无法解析，请检查文件是否损坏或加密。") from exc
        blocks: list[DocumentBlock] = []
        evidence: list[Evidence] = []
        warnings: list[str] = []
        size = 0
        text_size = 0
        normalized_numbers: list[str] = []
        try:
            for sheet in workbook.sheets():
                size += sheet.nrows * sheet.ncols
                if size > 100000 or workbook.nsheets > 100:
                    raise ERPError("document_limit", "XLS 表格范围过大，请缩小或拆分文件。")
                if any(r1 > sheet.nrows or c1 > sheet.ncols or r1 <= r0 or c1 <= c0
                       for r0, r1, c0, c1 in sheet.merged_cells):
                    raise ERPError("invalid_xls", "XLS 合并区域超出表格范围。")
                index = len(blocks)
                eid = f"{source.id}:p{index}"
                title = f"工作表：{sheet.name}" + ("（隐藏）" if sheet.visibility else "")
                blocks.append(DocumentBlock(index, "paragraph", title, eid))
                evidence.append(Evidence(eid, source.id, f"XLS 工作表 {sheet.name}", title))
                index = len(blocks)
                merges = {(r0, c0): (r1, c1) for r0, r1, c0, c1 in sheet.merged_cells}
                covered = {(r, c) for r0, r1, c0, c1 in sheet.merged_cells
                           for r in range(r0, r1) for c in range(c0, c1)
                           if (r, c) != (r0, c0)}
                cells: list[Cell] = []
                for row in range(sheet.nrows):
                    for column in range(sheet.ncols):
                        if (row, column) in covered:
                            continue
                        raw = sheet.cell(row, column)
                        fmt = workbook.format_map[workbook.xf_list[raw.xf_index].format_key].format_str
                        text = self._text(raw, fmt, workbook.datemode)
                        if raw.ctype == xlrd.XL_CELL_NUMBER and Decimal(str(raw.value)) != Decimal(format(raw.value, ".15g")):
                            normalized_numbers.append(f"{sheet.name}!{xlrd.colname(column)}{row + 1}：{raw.value} → {text}")
                        text_size += len(text)
                        if text_size > 2000000:
                            raise ERPError("document_limit", "XLS 提取内容过多，请拆分上传。")
                        eid = f"{source.id}:t{index}:r{row}:c{column}"
                        r1, c1 = merges.get((row, column), (row + 1, column + 1))
                        cells.append(Cell(row, column, text, eid, c1 - column,
                                          "restart" if r1 > row + 1 else "", fmt))
                        for next_row in range(row + 1, r1):
                            merge_id = f"{source.id}:t{index}:r{next_row}:c{column}"
                            cells.append(Cell(next_row, column, "", merge_id, c1 - column, "continue", fmt))
                            evidence.append(Evidence(merge_id, source.id,
                                f"XLS 工作表 {sheet.name} / {xlrd.colname(column)}{next_row + 1}（合并续行）", ""))
                        evidence.append(Evidence(eid, source.id,
                            f"XLS 工作表 {sheet.name} / {xlrd.colname(column)}{row + 1}", text))
                        if raw.ctype == xlrd.XL_CELL_ERROR:
                            warnings.append(f"工作表 {sheet.name} 的 {xlrd.colname(column)}{row + 1} 存在 Excel 错误值 {text}。")
                cells.sort(key=lambda cell: (cell.row, cell.column))
                blocks.append(DocumentBlock(index, "table", cells=tuple(cells)))
        finally:
            workbook.release_resources()
        if not any(c.text.strip() for b in blocks for c in b.cells):
            raise ERPError("empty_document", "XLS 没有可读取数据。")
        if normalized_numbers:
            warnings.append("XLS 二进制数值按 Excel 的 15 位有效数字规范化，消除存储浮点尾差；"
                            f"共 {len(normalized_numbers)} 格。" + "；".join(normalized_numbers[:10]))
        return contextualize_document(ParsedDocument(source, tuple(blocks), tuple(evidence),
                                                     tuple(warnings), "xlrd-2"))

    @staticmethod
    def _text(cell: xlrd.sheet.Cell, number_format: str, datemode: int) -> str:
        if cell.ctype == xlrd.XL_CELL_NUMBER:
            value = Decimal(format(cell.value, ".15g"))
            if "%" in number_format:
                return format((value * 100).normalize(), "f") + "%"
            return str(int(value)) if value == value.to_integral_value() else format(value, "f")
        if cell.ctype == xlrd.XL_CELL_DATE:
            return xlrd.xldate_as_datetime(cell.value, datemode).isoformat(sep=" ")
        if cell.ctype == xlrd.XL_CELL_BOOLEAN:
            return "TRUE" if cell.value else "FALSE"
        if cell.ctype == xlrd.XL_CELL_ERROR:
            return xlrd.error_text_from_code.get(cell.value, "#ERROR")
        return str(cell.value) if cell.ctype not in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK} else ""
