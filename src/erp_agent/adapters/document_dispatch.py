from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.adapters.pdf_documents import PDFDocumentParser
from erp_agent.adapters.spreadsheet_documents import XLSDocumentParser
from erp_agent.adapters.xlsx_documents import XLSXDocumentParser
from erp_agent.application.ports import DocumentConverter, DocumentParser
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import ParsedDocument, SourceDocument


class MultiFormatDocumentParser:
    def __init__(self, converter: DocumentConverter | None = None) -> None:
        word = OOXMLDocumentParser(converter)
        self.parsers: dict[str, DocumentParser] = {
            "doc": word, "docx": word, "pdf": PDFDocumentParser(), "xls": XLSDocumentParser(),
            "xlsx": XLSXDocumentParser(),
        }

    def parse(self, source: SourceDocument, content: bytes) -> ParsedDocument:
        parser = self.parsers.get(source.format)
        if parser is None:
            raise ERPError("unsupported_format", "请上传 DOCX、DOC、PDF、XLS 或 XLSX 文件。")
        return parser.parse(source, content)
