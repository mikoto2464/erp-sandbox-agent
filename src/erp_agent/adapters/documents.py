import hashlib
import io
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from erp_agent.application.ports import DocumentConverter
from erp_agent.domain.errors import ERPError
from erp_agent.domain.evidence import contextualize_document
from erp_agent.domain.models import (
    Cell,
    DocumentBlock,
    Evidence,
    ParsedDocument,
    SourceDocument,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOC_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


class LibreOfficeConverter:
    def __init__(self, executable: str = "libreoffice", timeout: int = 45) -> None:
        self.executable = executable
        self.timeout = timeout

    def convert(self, content: bytes) -> bytes:
        executable = shutil.which(self.executable)
        if not executable:
            raise ERPError("converter_unavailable", "DOC 转换器未安装，请安装 LibreOffice。", 503)
        with tempfile.TemporaryDirectory(prefix="erp-doc-") as directory:
            root = Path(directory)
            source = root / "input.doc"
            source.write_bytes(content)
            try:
                result = subprocess.run(
                    [executable, f"-env:UserInstallation={(root / 'profile').as_uri()}",
                     "--headless", "--convert-to", "docx", "--outdir", str(root), str(source)],
                    capture_output=True, timeout=self.timeout, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ERPError("conversion_timeout", "DOC 转换超时。", 504) from exc
            output = root / "input.docx"
            if result.returncode or not output.is_file():
                raise ERPError("conversion_failed", "DOC 转换失败，请检查文件是否损坏。")
            if output.stat().st_size > 30 * 1024 * 1024:
                raise ERPError("conversion_too_large", "转换后的文件超过大小限制。")
            return output.read_bytes()


class OOXMLDocumentParser:
    def __init__(self, converter: DocumentConverter | None = None) -> None:
        self.converter = converter

    def parse(self, source: SourceDocument, content: bytes) -> ParsedDocument:
        converted_hash = None
        if source.format == "doc":
            if not content.startswith(DOC_SIGNATURE):
                raise ERPError("invalid_signature", "文件不是有效的二进制 DOC。")
            if self.converter is None:
                raise ERPError("converter_unavailable", "未配置 DOC 转换器。", 503)
            content = self.converter.convert(content)
            converted_hash = hashlib.sha256(content).hexdigest()
        elif source.format != "docx":
            raise ERPError("unsupported_format", "当前读题支持 DOCX 和 DOC。")
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 3000 or sum(e.file_size for e in entries) > 80 * 1024 * 1024:
                    raise ERPError("archive_limit", "文档解压体积或成员数超过限制。")
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                        raise ERPError("unsafe_archive", "文档包含不安全路径。")
                    if entry.flag_bits & 1:
                        raise ERPError("encrypted_document", "暂不支持加密文档。")
                raw = archive.read("word/document.xml")
                if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                    raise ERPError("unsafe_xml", "文档包含不支持的 XML 声明。")
                root = ET.fromstring(raw)
                warnings = []
                if any(e.filename.startswith("word/media/") for e in entries):
                    warnings.append("文档包含图片；当前未启用 OCR，图片内的数据需要补充复核。")
                if any(e.filename.startswith(("word/header", "word/footer")) for e in entries):
                    warnings.append("当前解析正文，页眉页脚中的规则需另行补充。")
        except (BadZipFile, KeyError, ET.ParseError, RuntimeError) as exc:
            raise ERPError("invalid_docx", "DOCX 结构无效或文件已损坏。") from exc
        body = root.find(f"{W}body")
        if body is None:
            raise ERPError("empty_document", "文档没有正文。")
        blocks: list[DocumentBlock] = []
        evidence: list[Evidence] = []
        for index, element in enumerate(body):
            if element.tag == f"{W}p":
                text = self._text(element)
                eid = f"{source.id}:p{index}"
                blocks.append(DocumentBlock(index, "paragraph", text, eid))
                evidence.append(Evidence(eid, source.id, f"正文块 {index + 1} / 段落", text))
            elif element.tag == f"{W}tbl":
                cells: list[Cell] = []
                for row_index, row in enumerate(element.findall(f"{W}tr")):
                    before = row.find(f"{W}trPr/{W}gridBefore")
                    column = int(before.get(f"{W}val", "0")) if before is not None else 0
                    for cell in row.findall(f"{W}tc"):
                        span = cell.find(f"{W}tcPr/{W}gridSpan")
                        width = int(span.get(f"{W}val", "1")) if span is not None else 1
                        merge = cell.find(f"{W}tcPr/{W}vMerge")
                        vertical = merge.get(f"{W}val", "continue") if merge is not None else ""
                        text = self._text(cell)
                        eid = f"{source.id}:t{index}:r{row_index}:c{column}"
                        cells.append(Cell(row_index, column, text, eid, width, vertical))
                        evidence.append(Evidence(
                            eid, source.id,
                            f"正文块 {index + 1} / 表格行 {row_index + 1} 列 {column + 1}", text,
                        ))
                        column += width
                blocks.append(DocumentBlock(index, "table", cells=tuple(cells)))
                if element.findall(f".//{W}tbl"):
                    warnings.append("文档含嵌套表格；请复核原始表格与字段映射。")
        if not any(e.text.strip() for e in evidence):
            raise ERPError("empty_document", "文档没有可读取文字，可能需要 OCR。")
        return contextualize_document(ParsedDocument(
            source, tuple(blocks), tuple(evidence), tuple(warnings), parser_version="ooxml-3",
            converted_sha256=converted_hash,
        ))

    @staticmethod
    def _text(element: ET.Element) -> str:
        parts: list[str] = []
        for node in element.iter():
            if node.tag == f"{W}t":
                parts.append(node.text or "")
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in {f"{W}br", f"{W}cr"}:
                parts.append("\n")
            elif node.tag == f"{W}p" and parts:
                parts.append("\n")
        return "".join(parts).strip()
