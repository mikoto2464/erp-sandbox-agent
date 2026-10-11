import hashlib
import unittest

from erp_agent.adapters.documents import DOC_SIGNATURE, OOXMLDocumentParser
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import SourceDocument
from tests.helpers import docx_bytes


class DocumentTests(unittest.TestCase):
    def parse(self, content: bytes, fmt: str = "docx"):
        return OOXMLDocumentParser().parse(SourceDocument("a" * 32, f"topic.{fmt}",
            hashlib.sha256(content).hexdigest(), len(content), fmt), content)

    def test_order_spans_and_empty_merge_preserved(self):
        content = docx_bytes('''<w:p><w:r><w:t>市场调研</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr>
        <w:p><w:r><w:t>第一年</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>南京</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc>
        <w:tc><w:p><w:r><w:t>深圳</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:r><w:t>补充说明</w:t></w:r></w:p>''')
        doc = self.parse(content)
        self.assertEqual([b.kind for b in doc.blocks], ["paragraph", "table", "paragraph"])
        self.assertEqual(doc.blocks[1].cells[0].column_span, 2)
        self.assertEqual(doc.blocks[1].cells[1].column, 2)
        self.assertEqual(doc.blocks[1].cells[2].text, "")
        self.assertEqual(doc.blocks[1].cells[2].vertical_merge, "continue")
        self.assertEqual(len({e.id for e in doc.evidence}), len(doc.evidence))

    def test_split_tables_and_names(self):
        doc = self.parse(docx_bytes('''<w:tbl><w:tr><w:tc><w:p><w:r><w:t>P3</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p/><w:tbl><w:tr><w:tc><w:p><w:r><w:t>灯饰产品</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'''))
        self.assertEqual([e.text for e in doc.evidence], ["P3", "", "灯饰产品"])

    def test_invalid_zip_rejected(self):
        with self.assertRaises(ERPError):
            self.parse(b"not a docx")

    def test_path_traversal_and_xml_entity_rejected(self):
        for content in [docx_bytes(extras={"../bad": b"x"}),
                        docx_bytes('<!DOCTYPE bad><w:p/>')]:
            with self.assertRaises(ERPError):
                self.parse(content)

    def test_image_warning(self):
        self.assertTrue(self.parse(docx_bytes(extras={"word/media/image.png": b"x"})).warnings)

    def test_doc_requires_converter_and_signature(self):
        with self.assertRaises(ERPError) as caught:
            self.parse(DOC_SIGNATURE + b"x", "doc")
        self.assertEqual(caught.exception.code, "converter_unavailable")
        with self.assertRaises(ERPError):
            self.parse(b"invalid", "doc")

    def test_converter_replaceable(self):
        class Converter:
            def convert(self, content: bytes) -> bytes:
                return docx_bytes()
        doc = OOXMLDocumentParser(Converter()).parse(
            SourceDocument("a" * 32, "题目.doc", "hash", 9, "doc"), DOC_SIGNATURE + b"x")
        self.assertIsNotNone(doc.converted_sha256)
        self.assertEqual(doc.source.format, "doc")
