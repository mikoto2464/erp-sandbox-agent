import unittest
from dataclasses import replace

from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.application.codec import parsed_document
from erp_agent.application.contracts import dumps, editable_reading, obj, to_json
from erp_agent.application.reader import StructuredReader
from erp_agent.domain.models import Fact, KnowledgeState, ProblemSpec, ReadingResult, RuleSet, SourceDocument
from erp_agent.domain.validation import validate_reading
from tests.helpers import Control, FakeLLM, docx_bytes


def cell(text: str, properties: str = '') -> str:
    return f'<w:tc><w:tcPr>{properties}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'


def table(rows: list[list[str]]) -> str:
    return '<w:tbl>' + ''.join('<w:tr>' + ''.join(cell(text) for text in row) + '</w:tr>'
                              for row in rows) + '</w:tbl>'


class TableEvidenceTests(unittest.TestCase):
    def parse(self, xml: str):
        content = docx_bytes(xml)
        return OOXMLDocumentParser().parse(SourceDocument('doc', '规则.docx', '', len(content),
                                                           'docx'), content)

    def reading(self, eid: str, value: str = '0.5', conversion: str = 'percent_to_ratio',
                refs: tuple[str, ...] = ()):
        fact = Fact('efficiency', 'worker', 'junior', '初级工人', 'efficiency', value,
                    'number', '比例', KnowledgeState.KNOWN, (eid,) + refs, conversion)
        return ReadingResult(ProblemSpec('测试题目', (fact,)), RuleSet(()), (), 'controlled')

    def test_unit_header_and_row_are_locatable_without_changing_cell_text(self):
        document = self.parse('<w:p><w:r><w:t>工人招聘参数</w:t></w:r></w:p>' + table([
            ['名称', '月薪（元/月）', '效率（%）'], ['初级工人', '800', '50'],
            ['高级工人', '1800', '50'],
        ]))
        source = next(e for e in document.evidence if e.id == 'doc:t1:r1:c2')
        self.assertEqual(source.text, '50')
        self.assertEqual(source.table_context.heading_ids, ('doc:p0',))
        self.assertEqual(source.table_context.column_header_ids, ('doc:t1:r0:c2',))
        self.assertEqual(source.table_context.row_ids,
                         ('doc:t1:r1:c0', 'doc:t1:r1:c1', 'doc:t1:r1:c2'))
        for row in (1, 2):
            result = validate_reading(self.reading(f'doc:t1:r{row}:c2'), document.evidence)
            self.assertEqual(result.problem.facts[0].state, KnowledgeState.KNOWN)
            self.assertEqual(result.problem.facts[0].value, '0.5')
            self.assertIn('doc:t1:r0:c2', result.problem.facts[0].evidence_ids)
        wrong = validate_reading(self.reading('doc:t1:r1:c2', value='0.6'), document.evidence)
        self.assertEqual(wrong.problem.facts[0].state, KnowledgeState.CONFLICTED)

    def test_percentage_from_other_column_table_or_document_cannot_support_value(self):
        document = self.parse(table([['名称', '数量', '效率（%）'], ['初级工人', '50', '80']])
                              + table([['名称', '工资'], ['高级工人', '50']]))
        cases = [('doc:t0:r1:c1', 'doc:t0:r0:c2'), ('doc:t1:r1:c1', 'doc:t0:r0:c2')]
        for eid, wrong_header in cases:
            with self.subTest(eid=eid):
                checked = validate_reading(self.reading(eid, refs=(wrong_header,)), document.evidence)
                self.assertEqual(checked.problem.facts[0].state, KnowledgeState.CONFLICTED)
        other = replace(document.evidence[2], document_id='other')
        checked = validate_reading(self.reading('doc:t0:r1:c1', refs=(other.id,)),
                                   tuple(e for e in document.evidence if e.id != other.id) + (other,))
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.CONFLICTED)

    def test_merged_multilevel_unit_header_supports_only_columns_it_covers(self):
        xml = ('<w:tbl><w:tr>' + cell('名称', '<w:vMerge w:val="restart"/>')
               + cell('效率（％）', '<w:gridSpan w:val="2"/>') + cell('数量') + '</w:tr>'
               '<w:tr>' + cell('', '<w:vMerge/>') + cell('基础') + cell('培训后')
               + cell('上限') + '</w:tr><w:tr>' + cell('工人') + cell('50') + cell('80')
               + cell('50') + '</w:tr></w:tbl>')
        document = self.parse(xml)
        checked = validate_reading(self.reading('doc:t0:r2:c1'), document.evidence)
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.KNOWN)
        self.assertIn('doc:t0:r0:c1', checked.problem.facts[0].evidence_ids)
        unrelated = validate_reading(self.reading('doc:t0:r2:c3'), document.evidence)
        self.assertEqual(unrelated.problem.facts[0].state, KnowledgeState.CONFLICTED)

    def test_wan_unit_in_column_header_and_no_double_conversion(self):
        document = self.parse(table([['对象', '投入（万元）'], ['产线', '10']]))
        checked = validate_reading(self.reading('doc:t0:r1:c1', '100000', 'wan_to_yuan'),
                                   document.evidence)
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.KNOWN)
        bad = validate_reading(self.reading('doc:t0:r1:c1', '1000000000', 'wan_to_yuan'),
                               document.evidence)
        self.assertEqual(bad.problem.facts[0].state, KnowledgeState.CONFLICTED)

    def test_legacy_document_gets_context_on_load_and_reader_receives_header_links(self):
        document = self.parse(table([['名称', '效率（%）'], ['初级工人', '50']]))
        legacy = obj(to_json(document))
        for evidence in legacy['evidence']:
            evidence.pop('table_context')
        legacy['parser_version'] = 'ooxml-1'
        restored = parsed_document(legacy)
        source = next(e for e in restored.evidence if e.id == 'doc:t0:r1:c1')
        self.assertEqual(source.table_context.column_header_ids, ('doc:t0:r0:c1',))
        llm = FakeLLM(dumps(editable_reading(self.reading(source.id))))
        result = StructuredReader(llm).read((restored,), Control())
        self.assertEqual(result.problem.facts[0].state, KnowledgeState.KNOWN)
        self.assertIn('doc:t0:r0:c1', result.problem.facts[0].evidence_ids)
        self.assertIn('table_contexts', llm.calls[0].user)
