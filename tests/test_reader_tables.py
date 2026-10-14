import copy
import threading
import unittest
from dataclasses import replace

from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.application.contracts import dumps, editable_reading, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.reader import StructuredReader
from erp_agent.application.reading.batches import DocumentBatcher
from erp_agent.application.reading.review import LinkReviewRole
from erp_agent.application.reading.roles import ExtractionRole, ReaderLimits
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Fact, Issue, KnowledgeState, ProblemSpec, ReadingResult, RuleSet, SourceDocument
from erp_agent.domain.validation import validate_reading
from tests.helpers import Control, docx_bytes


def document(rows, name='orders'):
    xml = '<w:tbl>' + ''.join('<w:tr>' + ''.join(
        '<w:tc><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:tc>'
        for text in row) + '</w:tr>' for row in rows) + '</w:tbl>'
    return OOXMLDocumentParser().parse(SourceDocument(name, name + '.docx', '', 0, 'docx'),
                                      docx_bytes(xml))


def mapping(doc, definitions):
    block = doc.blocks[0]
    return {'block_index': block.index, 'category': 'order_pool', 'row_start': 1,
            'row_end': max(c.row for c in block.cells), 'columns': [
                {'column': index, 'key': key, 'value_type': kind, 'unit': unit,
                 'conversion': conversion, 'header_ids': [block.cells[index].evidence_id]}
                for index, (key, kind, unit, conversion) in enumerate(definitions)]}


def material(table):
    return {'title': '题目', 'fact_rows': [], 'rule_rows': [], 'issues': [],
            'table_mappings': [table]}


class ReaderTableTests(unittest.TestCase):
    def test_large_order_table_expands_original_values_once_without_model_transcription(self):
        doc = document([['编号', '产品', '数量', '价格']] + [
            [f'{i:03d}', 'A', str(i * 10), '12.5'] for i in range(1, 71)])
        table = mapping(doc, [('order_id', 'text', '', 'identity'),
                              ('product', 'text', '', 'identity'),
                              ('quantity', 'integer', '件', 'identity'),
                              ('reference_price', 'number', '元', 'identity')])
        class Client:
            configured = True
            calls = []
            def generate(self, request):
                self.calls.append(request)
                return LLMResponse(dumps(material(table)), 'controlled')
        client = Client()
        result = StructuredReader(client).read((doc,), Control())
        facts = [f for f in result.problem.facts if f.category == 'order_pool']
        self.assertEqual(len(client.calls), 1)
        self.assertLess(len(dumps(material(table))), 1000)
        self.assertEqual(len(facts), 280)
        self.assertEqual(len({f.entity_id for f in facts}), 70)
        self.assertEqual([f.value for f in facts if f.key == 'order_id'],
                         [f'{i:03d}' for i in range(1, 71)])
        self.assertEqual(sum(int(f.value) for f in facts if f.key == 'quantity'), 24850)
        self.assertFalse(any(i.severity == 'error' for i in result.issues))
        self.assertFalse(any(f.key == 'detail' for f in facts))

    def test_mapping_converts_supported_units_once_and_keeps_blanks_unknown(self):
        doc = document([['数量', '损失（%）', '费用（万元）'], ['2', '5', '10'], ['3', '5%', '']])
        table = mapping(doc, [('quantity', 'integer', '件', 'identity'),
                              ('loss', 'number', '比例', 'percent_to_ratio'),
                              ('fee', 'number', '元', 'wan_to_yuan')])
        batch = DocumentBatcher().tasks((doc,))[0]
        result = ExtractionRole('reading')._decode(dumps(material(table)), 'test', batch)
        checked = validate_reading(result, doc.evidence)
        self.assertEqual([f.value for f in checked.problem.facts if f.key == 'loss'], ['0.05', '0.05'])
        self.assertEqual([f.value for f in checked.problem.facts if f.key == 'fee'], ['100000', None])
        self.assertFalse(any(i.severity == 'error' for i in checked.issues))
        self.assertEqual(next(f.state for f in checked.problem.facts if f.value is None),
                         KnowledgeState.MISSING)

    def test_mapping_rejects_missing_columns_wrong_headers_scope_and_overlap(self):
        doc = document([['编号', '数量'], ['001', '10'], ['002', '20']])
        table = mapping(doc, [('order_id', 'text', '', 'identity'), ('quantity', 'integer', '', 'identity')])
        batch = DocumentBatcher().tasks((doc,))[0]
        role = ExtractionRole('reading')
        variants = []
        bad = copy.deepcopy(table); bad['columns'].pop(); variants.append(material(bad))
        bad = copy.deepcopy(table); bad['columns'][1]['header_ids'] = table['columns'][0]['header_ids']; variants.append(material(bad))
        bad = copy.deepcopy(table); bad['row_end'] = 3; variants.append(material(bad))
        bad = material(table); bad['table_mappings'].append(table); variants.append(bad)
        bad = material(table)
        bad['fact_rows'] = [['copy', 'order_pool', 'order', '订单', 'quantity', '10',
                             'integer', '', 'known', doc.blocks[0].cells[3].evidence_id,
                             'identity', '']]
        variants.append(bad)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ERPError):
                role._decode(dumps(value), 'test', batch)
        partial = replace(batch, units=tuple(replace(b, cells=tuple(c for c in b.cells if c.row < 2))
                                            for b in batch.units))
        with self.assertRaises(ERPError):
            role._decode(dumps(material(table)), 'test', partial)

    def test_joined_rows_require_exact_cell_values_and_column_order(self):
        doc = document([['年份', '产品', '数量（件）'], ['1', 'P1', '10'], ['2', 'P2', '20']])
        refs = tuple(c.evidence_id for c in doc.blocks[0].cells if c.row == 1)
        fact = Fact('row', 'order_pool', 'o', '订单', 'detail', '1|P1|10', 'text', '',
                    KnowledgeState.KNOWN, refs)
        base = ReadingResult(ProblemSpec('题目', (fact,)), RuleSet(()), (), 'test')
        for text, evidence_ids, supported in [
            ('1|P1|10', refs, True), ('1|P1|100', refs, False), ('1|10|P1', refs, False),
            ('1|P1', refs, False), ('1|P1|10', (refs[0], refs[1], doc.evidence[-1].id), False),
        ]:
            with self.subTest(text=text, evidence_ids=evidence_ids):
                checked = validate_reading(replace(base, problem=replace(base.problem, facts=(
                    replace(fact, value=text, evidence_ids=evidence_ids),))), doc.evidence)
                self.assertEqual(checked.problem.facts[0].state == KnowledgeState.KNOWN, supported)
        headers = tuple(c.evidence_id for c in doc.blocks[0].cells if c.row == 0)
        checked = validate_reading(replace(base, problem=replace(base.problem, facts=(
            replace(fact, category='other', key='order_pool_field_order', value='年份|产品|数量',
                    evidence_ids=headers),))), doc.evidence)
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.KNOWN)
        changed = replace(checked, problem=replace(checked.problem, facts=(
            replace(checked.problem.facts[0], value='年份|产品|参考价'),)))
        self.assertTrue(any(i.code == 'validation.unsupported_value'
                            for i in validate_reading(changed, doc.evidence).issues))

    def test_explicit_vertical_merge_inherits_only_its_anchor_and_keeps_row_entities(self):
        xml = '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>年份</w:t></w:r></w:p></w:tc>'
        xml += '<w:tc><w:p><w:r><w:t>数量</w:t></w:r></w:p></w:tc></w:tr>'
        for merge, year, quantity in [('restart', '1', '10'), ('continue', '', '20')]:
            xml += f'<w:tr><w:tc><w:tcPr><w:vMerge w:val="{merge}"/></w:tcPr>'
            xml += f'<w:p><w:r><w:t>{year}</w:t></w:r></w:p></w:tc>'
            xml += f'<w:tc><w:p><w:r><w:t>{quantity}</w:t></w:r></w:p></w:tc></w:tr>'
        xml += '</w:tbl>'
        doc = OOXMLDocumentParser().parse(SourceDocument('doc', 'orders.docx', '', 0, 'docx'),
                                          docx_bytes(xml))
        table = mapping(doc, [('year', 'integer', '年', 'identity'),
                              ('quantity', 'integer', '件', 'identity')])
        class Client:
            configured = True
            def generate(self, request):
                return LLMResponse(dumps(material(table)), 'test')
        result = StructuredReader(Client()).read((doc,), Control())
        facts = [f for f in result.problem.facts if f.category == 'order_pool']
        self.assertEqual(len({f.entity_id for f in facts}), 2)
        self.assertEqual([f.value for f in facts if f.key == 'year'], ['1', '1'])
        self.assertFalse(any(i.severity == 'error' for i in result.issues))

    def test_larger_tasks_start_first_but_fact_merge_keeps_original_document_order(self):
        from tests.test_reader_pipeline import RowClient
        docs = (document([['编号', '数量'], ['1', '10']], 'small'),
                document([['编号', '数量']] + [[str(i), str(i * 10)] for i in range(1, 30)], 'large'))
        client = RowClient()
        result = StructuredReader(client, limits=ReaderLimits(concurrency=1)).read(docs, Control())
        self.assertEqual(loads(client.calls[0].user)['documents'][0]['filename'], 'large.docx')
        self.assertTrue(result.problem.facts[0].evidence_ids[0].startswith('small:'))

    def test_review_deduplicates_entity_scope_and_runs_independent_batches_concurrently(self):
        doc = document([['编号', '数量']] + [[str(i), '10'] for i in range(1, 61)])
        refs = [c.evidence_id for c in doc.blocks[0].cells if c.row > 0 and c.column == 1]
        facts = tuple(Fact(f'f{i}', 'worker', f'worker{i // 10}', f'工人{i // 10}', f'key{i}',
                           '10', 'integer', '', KnowledgeState.CONFLICTED, (ref,))
                      for i, ref in enumerate(refs))
        reading = ReadingResult(ProblemSpec('题目', facts), RuleSet(()), (), 'test')
        barrier = threading.Barrier(2)
        class Client:
            configured = True
            def __init__(self):
                self.calls = []
            def generate(self, request):
                self.calls.append(request)
                barrier.wait(timeout=3)
                data = loads(request.user)
                return LLMResponse(dumps({'issues': [{'code': 'review.test', 'severity': 'warning',
                    'target': data['facts'][0]['id'], 'message': '待核对', 'evidence_ids': []}]}), 'test')
        client = Client()
        issues = LinkReviewRole().review(reading, doc.evidence, client, Control(), 30000, 0,
                                         {ref: '0' for ref in refs}, concurrency=2)
        seen = [f['id'] for request in client.calls for f in loads(request.user)['facts']]
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {f.id for f in facts})
        self.assertEqual([i.target for i in issues], ['f0', 'f30'])


if __name__ == '__main__':
    unittest.main()
