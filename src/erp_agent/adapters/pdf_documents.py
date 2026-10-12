import io

import pdfplumber
from pdfminer.pdfexceptions import PDFException

from erp_agent.domain.errors import ERPError
from erp_agent.domain.evidence import contextualize_document
from erp_agent.domain.models import Cell, DocumentBlock, Evidence, ParsedDocument, SourceDocument


class PDFDocumentParser:
    def parse(self, source: SourceDocument, content: bytes) -> ParsedDocument:
        if not content.startswith(b"%PDF-"):
            raise ERPError("invalid_signature", "文件不是有效的 PDF。")
        blocks: list[DocumentBlock] = []
        evidence: list[Evidence] = []
        warnings: list[str] = []
        readable = False
        text_size = 0
        try:
            with pdfplumber.open(io.BytesIO(content)) as document:
                if len(document.pages) > 300:
                    raise ERPError("document_limit", "PDF 超过 300 页，请拆分上传。")
                for page_number, page in enumerate(document.pages, 1):
                    tables = page.find_tables()
                    areas = [table.bbox for table in tables]
                    text_page = page.filter(lambda item: not any(
                        left <= item.get("x0", -1) and item.get("x1", -1) <= right
                        and top <= item.get("top", -1) and item.get("bottom", -1) <= bottom
                        for left, top, right, bottom in areas))
                    lines = text_page.extract_text_lines(return_chars=False)
                    items = sorted([(line["top"], "text", line["text"]) for line in lines]
                                   + [(table.bbox[1], "table", table) for table in tables],
                                   key=lambda item: item[0])
                    index = len(blocks)
                    eid = f"{source.id}:p{index}"
                    blocks.append(DocumentBlock(index, "paragraph", f"第 {page_number} 页", eid))
                    evidence.append(Evidence(eid, source.id, f"PDF 第 {page_number} 页", f"第 {page_number} 页"))
                    page_readable = False
                    for _, kind, item in items:
                        index = len(blocks)
                        if kind == "text":
                            text = str(item).strip()
                            if not text:
                                continue
                            eid = f"{source.id}:p{index}"
                            blocks.append(DocumentBlock(index, "paragraph", text, eid))
                            evidence.append(Evidence(eid, source.id, f"PDF 第 {page_number} 页 / 段落", text))
                            text_size += len(text)
                            page_readable = True
                        else:
                            cells: list[Cell] = []
                            for row, values in enumerate(item.extract()):
                                for column, value in enumerate(values):
                                    eid = f"{source.id}:t{index}:r{row}:c{column}"
                                    text = value or ""
                                    cells.append(Cell(row, column, text, eid))
                                    text_size += len(text)
                                    evidence.append(Evidence(eid, source.id,
                                        f"PDF 第 {page_number} 页 / 表格行 {row + 1} 列 {column + 1}", text))
                                    page_readable |= bool(text.strip())
                            blocks.append(DocumentBlock(index, "table", cells=tuple(cells)))
                        if len(evidence) > 100000 or text_size > 2000000:
                            raise ERPError("document_limit", "PDF 提取内容过多，请拆分上传。")
                    if not page_readable:
                        warnings.append(f"PDF 第 {page_number} 页没有可提取文字，可能是图片；请手动补充，当前未启用 OCR。")
                    elif page.images:
                        warnings.append(f"PDF 第 {page_number} 页包含图片，图片内数据需手动补充，当前未启用 OCR。")
                    readable |= page_readable
                    page.close()
        except (PDFException, ValueError, TypeError, EOFError) as exc:
            raise ERPError("invalid_pdf", "PDF 无法解析，请检查文件是否损坏或加密。") from exc
        if not readable:
            raise ERPError("pdf_needs_ocr", "PDF 没有可提取文字，可能是扫描件；当前未启用 OCR，请手动输入题目或上传文字版。")
        if sum(len(e.text) for e in evidence) > 2000000:
            raise ERPError("document_limit", "PDF 提取内容过多，请拆分上传。")
        return contextualize_document(ParsedDocument(source, tuple(blocks), tuple(evidence),
                                                     tuple(warnings), "pdfplumber-1"))
