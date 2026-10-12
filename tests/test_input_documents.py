import unittest
from pathlib import Path

from erp_agent.adapters.document_dispatch import MultiFormatDocumentParser
from erp_agent.adapters.budget_templates import SpreadsheetBudgetParser
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import SourceDocument


class InputDocumentTests(unittest.TestCase):
    def parse(self, name):
        content = Path(__file__).with_name('fixtures').joinpath(name).read_bytes()
        fmt = name.rsplit('.', 1)[-1]
        return MultiFormatDocumentParser().parse(SourceDocument('doc', name, '', len(content), fmt), content)

    def test_pdf_text_table_and_page_sources(self):
        document = self.parse('orders.pdf')
        self.assertTrue(any('Orders limit 10800' in e.text for e in document.evidence))
        self.assertTrue(any(b.kind == 'table' for b in document.blocks))
        value = next(e for e in document.evidence if e.text == '10800')
        self.assertIn('第 1 页', value.location)
        self.assertTrue(value.table_context.column_header_ids)

    def test_xls_blank_leading_rows_merges_numbers_and_percent_format(self):
        document = self.parse('orders.xls')
        cells = document.blocks[1].cells
        self.assertTrue(any(c.column_span == 3 for c in cells))
        self.assertTrue(any(c.vertical_merge == 'continue' for c in cells))
        self.assertTrue(any(c.text == '50%' and c.number_format == '0%' for c in cells))
        value = next(e for e in document.evidence if e.text == '10800')
        self.assertIn('B4', value.location)
        self.assertIn('doc:t1:r2:c1', value.table_context.column_header_ids)
        content = Path(__file__).with_name('fixtures').joinpath('orders.xls').read_bytes()
        template = SpreadsheetBudgetParser().parse(document.source, content)
        self.assertTrue(template.sheets[0].merged_ranges)
        self.assertTrue(any(c.text == '50%' for c in template.sheets[0].cells))
        self.assertTrue(any('公式及保护' in warning for warning in template.warnings))

    def test_invalid_new_format_signatures_are_rejected(self):
        for fmt in ('pdf', 'xls'):
            with self.subTest(fmt=fmt), self.assertRaises(ERPError):
                MultiFormatDocumentParser().parse(SourceDocument('doc', 'x.'+fmt, '', 4, fmt), b'fake')

    def test_pdf_without_readable_text_reports_ocr_requirement(self):
        content = Path(__file__).with_name('fixtures').joinpath('orders.pdf').read_bytes()
        for text in (b'Orders limit 10800', b'Product', b'Quantity', b'P1', b'10800'):
            content = content.replace(text, b' ' * len(text))
        with self.assertRaises(ERPError) as caught:
            MultiFormatDocumentParser().parse(SourceDocument('doc', 'scan.pdf', '', len(content), 'pdf'), content)
        self.assertEqual(caught.exception.code, 'pdf_needs_ocr')
