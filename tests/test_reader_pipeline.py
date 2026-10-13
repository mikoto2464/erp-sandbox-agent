import unittest
from collections import Counter

from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.application.contracts import decode_reading, dumps, editable_reading, loads
from erp_agent.application.input_sources import text_source
from erp_agent.application.ports import LLMResponse
from erp_agent.application.reader import StructuredReader
from erp_agent.application.reading.batches import DocumentBatcher
from erp_agent.application.reading.coverage import missing_table_rows
from erp_agent.application.reading.review import LinkReviewRole
from erp_agent.application.reading.roles import ExtractionRole, ReaderLimits
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import (
    Evidence,
    Fact,
    KnowledgeState,
    ProblemSpec,
    ReadingResult,
    Rule,
    RuleSet,
    SourceDocument,
    TableEvidenceContext,
)
from tests.helpers import Control, docx_bytes


class RowClient:
    configured = True

    def __init__(self):
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        payload = loads(request.user)
        facts, rules = [], []
        if payload['role'] in {'facts', 'reading'}:
            for document in payload['documents']:
                for block in document['blocks']:
                    if 'table' not in block:
                        continue
                    rows = {}
                    for cell in block['table']:
                        rows.setdefault(cell[0], []).append(cell)
                    for row, cells in rows.items():
                        if row == 0:
                            continue
                        fact = Fact('quantity', 'order_pool', 'order_1', '订单', 'quantity',
                                    cells[-1][2], 'integer', '', KnowledgeState.KNOWN,
                                    tuple(c[3] for c in cells))
                        facts.append(editable_reading(ReadingResult(
                            ProblemSpec('详单', (fact,)), RuleSet(()), (), 'test'))['facts'][0])
        return LLMResponse(dumps({'title': '详单', 'facts': facts, 'rules': rules, 'issues': []}), 'test')


class ReaderPipelineTests(unittest.TestCase):
    def test_default_reader_formats_small_table_once_and_keeps_rules(self):
        document = self.table(8)
        class Client(RowClient):
            def generate(self, request):
                response = super().generate(request)
                data = loads(response.content)
                data['rules'] = [{'id': 'limit', 'name': '订单用途', 'expression': '详单为待竞订单池',
                                  'state': 'known', 'evidence_ids': [document.evidence[0].id],
                                  'explanation': ''}]
                # 实际生产使用紧凑行输出，解码后仍是完整规范结构。
                return LLMResponse(dumps({'title': data['title'],
                    'fact_rows': [[f[k] if k != 'evidence_ids' else '|'.join(f[k])
                                  for k in ('id', 'category', 'entity_id', 'entity_name', 'key', 'value',
                                            'value_type', 'unit', 'state', 'evidence_ids', 'conversion', 'explanation')]
                                  for f in data['facts']],
                    'rule_rows': [[r['id'], r['name'], r['expression'], r['state'], '|'.join(r['evidence_ids']), r['explanation']]
                                  for r in data['rules']], 'issues': []}), 'test')
        client = Client()
        result = StructuredReader(client).read((document,), Control())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len([f for f in result.problem.facts if f.category == 'order_pool']), 8)
        self.assertEqual(len(result.rules.rules), 1)
        self.assertFalse(any(i.code == 'validation.rule_source' for i in result.issues))

    def table(self, size=14):
        rows = [['订单', '数量']] + [[str(i), str(i * 10)] for i in range(1, size + 1)]
        xml = '<w:p><w:r><w:t>待竞订单池</w:t></w:r></w:p><w:tbl>'
        for row in rows:
            xml += '<w:tr>' + ''.join('<w:tc><w:p><w:r><w:t>' + value +
                                      '</w:t></w:r></w:p></w:tc>' for value in row) + '</w:tr>'
        xml += '</w:tbl>'
        return OOXMLDocumentParser().parse(SourceDocument('doc', 'orders.docx', 'hash', 0, 'docx'),
                                            docx_bytes(xml))

    def test_batches_preserve_every_row_headers_and_bound_every_role_request(self):
        document = self.table()
        client = RowClient()
        control = Control()
        result = StructuredReader(client, limits=ReaderLimits(15000, 3)).read((document,), control)
        self.assertEqual(len([f for f in result.problem.facts if f.category == 'order_pool']), 14)
        self.assertEqual(len({f.entity_id for f in result.problem.facts if f.category == 'order_pool'}), 14)
        covered = Counter()
        for request in client.calls:
            self.assertLessEqual(len(request.system) + len(request.user), 15000)
            payload = loads(request.user)
            self.assertEqual(len(payload['documents']), 1)
            if payload['role'] not in {'facts', 'reading'}:
                continue
            covered.update(payload['focus_evidence_ids'])
            doc = payload['documents'][0]
            ids = set(payload['focus_evidence_ids']) | {e[0] for e in doc['context_evidence']}
            if any(':r' in eid and ':r0:' not in eid for eid in payload['focus_evidence_ids']):
                self.assertIn('doc:t1:r0:c1', ids)
        self.assertEqual(set(covered), {e.id for e in document.evidence})
        self.assertTrue(all(count == 1 for count in covered.values()))
        self.assertIn('reading_linking', control.stages)
        self.assertEqual((result.problem.years, result.problem.periods_per_year), (4, 4))
        self.assertEqual(decode_reading(result.raw_response, result.model).problem.facts,
                         tuple(f for f in result.problem.facts if not f.id.startswith('default_')))

    def test_invalid_model_number_keeps_other_fields_and_a_located_manual_question(self):
        document = text_source('题目', '工资50元，订单数量待明确', 'numbers')
        batch = DocumentBatcher().batches((document,), 15000, 4)[0]
        refs = (document.evidence[0].id,)
        facts = tuple(Fact('f' + str(i), 'other', 'item' + str(i), '条目' + str(i), 'quantity',
                          value, 'number', '', KnowledgeState.KNOWN, refs)
                      for i, value in enumerate(('50.000000000000000', '1,000', '1e20', '0.050000000000000003', '编号001-A')))
        payload = editable_reading(ReadingResult(ProblemSpec('题目', facts), RuleSet(()), (), 'test'))
        result = ExtractionRole('facts')._decode(dumps(payload), 'test', batch)
        self.assertEqual([f.value for f in result.problem.facts], ['50', '1000', None, None, None])
        self.assertEqual(len(result.issues), 3)
        self.assertTrue(all(i.evidence_ids == refs and '请查看标出的原文' in i.message for i in result.issues))
        self.assertTrue(all(f.state == KnowledgeState.MISSING for f in result.problem.facts[2:]))
        self.assertIn('1e20', result.problem.facts[2].explanation)

    def test_cross_file_equal_values_merge_and_conflicts_keep_both_sources(self):
        documents = tuple(text_source(name, '初始资金' + value + '元', 'doc_' + name)
                          for name, value in [('赛题', '100'), ('补充', '100'), ('另表', '200')])

        class Client:
            configured = True

            def generate(self, request):
                payload = loads(request.user)
                if payload['role'] == 'review':
                    return LLMResponse(dumps({'issues': []}), 'test')
                document = payload['documents'][0]
                eid, text = document['blocks'][0]['paragraph']
                fact = Fact('cash', 'initial', 'company', '公司', 'initial_cash',
                            '200' if '200' in text else '100', 'number', '元', KnowledgeState.KNOWN, (eid,))
                result = ReadingResult(ProblemSpec('题目', (fact,) if payload['role'] in {'facts', 'reading'} else ()),
                                       RuleSet(()), (), 'test')
                return LLMResponse(dumps(editable_reading(result)), 'test')

        reader = StructuredReader(Client())
        equal = reader.read(documents[:2], Control())
        cash = next(f for f in equal.problem.facts if f.key == 'initial_cash')
        self.assertEqual(cash.state, KnowledgeState.KNOWN)
        self.assertEqual(len(cash.evidence_ids), 2)
        conflict = reader.read(documents, Control())
        cash = next(f for f in conflict.problem.facts if f.key == 'initial_cash')
        self.assertEqual(cash.state, KnowledgeState.CONFLICTED)
        self.assertEqual(len(cash.evidence_ids), 3)
        self.assertTrue(any(i.code == 'reading.source_conflict' and i.target == cash.id
                            for i in conflict.issues))

    def test_rule_truncation_splits_only_rules_and_keeps_completed_parameters(self):
        document = self.table(8)

        class Client(RowClient):
            def generate(self, request):
                payload = loads(request.user)
                if payload['role'] == 'rules' and len(payload['focus_evidence_ids']) > 6:
                    self.calls.append(request)
                    raise ERPError('llm_incomplete', '输出截断', 502)
                return super().generate(request)

        client = Client()
        result = StructuredReader(client, limits=ReaderLimits(30000, 12, 1), facts_role=ExtractionRole('facts'), rules_role=ExtractionRole('rules')).read((document,), Control())
        self.assertEqual(sum(loads(r.user)['role'] == 'facts' for r in client.calls), 1)
        self.assertGreater(sum(loads(r.user)['role'] == 'rules' for r in client.calls), 1)
        self.assertEqual(len([f for f in result.problem.facts if f.category == 'order_pool']), 8)

    def test_gateway_timeout_splits_failed_batch_and_preserves_all_rows(self):
        document = self.table(6)

        class Client(RowClient):
            def generate(self, request):
                payload = loads(request.user)
                if payload['role'] in {'facts', 'reading'} and len(payload['focus_evidence_ids']) > 7:
                    self.calls.append(request)
                    raise ERPError('llm_gateway_timeout', 'HTTP 504', 504)
                return super().generate(request)

        client = Client()
        result = StructuredReader(client, limits=ReaderLimits(30000, 12, 1)).read((document,), Control())
        self.assertEqual(len([f for f in result.problem.facts if f.category == 'order_pool']), 6)

    def test_complete_order_rows_avoid_redundant_rule_calls(self):
        document = self.table(8)
        client = RowClient()
        result = StructuredReader(client, limits=ReaderLimits(15000, 3, 1)).read((document,), Control())
        requests = [loads(r.user) for r in client.calls]
        self.assertEqual(sum(r['role'] == 'rules' for r in requests), 0)
        self.assertEqual(len([f for f in result.problem.facts if f.category == 'order_pool']), 8)

    def test_cancellation_after_call_prevents_next_subrole(self):
        class CancelControl(Control):
            def checkpoint(self, stage, completed=0):
                if len(self.stages) == 3:
                    raise Cancelled()
                super().checkpoint(stage, completed)

        client = RowClient()
        with self.assertRaises(Cancelled):
            StructuredReader(client, limits=ReaderLimits(30000, 12, 1)).read((self.table(),), CancelControl())
        self.assertEqual(len(client.calls), 1)

    def test_long_paragraph_is_split_without_silently_dropping_text(self):
        document = text_source('长段落', '经营规则' * 10000, 'manual_')
        batches = DocumentBatcher().batches((document,), 10000, 3)
        self.assertGreater(len(batches), 1)
        self.assertEqual(''.join(unit.text for batch in batches for unit in batch.units),
                         document.blocks[0].text)
        self.assertTrue(all(len(dumps(batch.payload())) <= 10000 for batch in batches))

    def test_numbered_rule_sentences_stay_together(self):
        document = text_source('规则', '1. 招聘后开始计薪。\n2. 每季支付工资。\n3. 离职需要赔偿。', 'manual_')
        batches = DocumentBatcher().batches((document,), 12000, 16)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0].units), 3)

    def test_review_skips_repeated_headers_but_checks_cross_batch_rules(self):
        fact = Fact('salary', 'worker', 'worker', '工人', 'salary', '100', 'number', '元',
                    KnowledgeState.KNOWN, ('header', 'value'))
        rule = Rule('payment', '工人工资支付', '工人每季付薪', KnowledgeState.KNOWN, ('rule',))
        evidence = (Evidence('header', 'doc', '表头', '月薪'),
                    Evidence('value', 'doc', '数据格', '100',
                             TableEvidenceContext(1, 1, 0, column_header_ids=('header',))),
                    Evidence('rule', 'doc', '另一章节', '工人每季付薪'))
        client = RowClient()
        groups = {'header': '0', 'value': '1', 'rule': '2'}
        reviewer = LinkReviewRole()
        ordinary = ReadingResult(ProblemSpec('题目', (fact,)), RuleSet(()), (), 'test')
        self.assertEqual(reviewer.review(ordinary, evidence, client, Control(), 12000, 0, groups), ())
        self.assertEqual(client.calls, [])

        class Client:
            configured = True
            calls = []

            def generate(self, request):
                self.calls.append(request)
                return LLMResponse(dumps({'issues': []}), 'test')

        client = Client()
        linked = ReadingResult(ordinary.problem, RuleSet((rule,)), (), 'test')
        reviewer.review(linked, evidence, client, Control(), 12000, 0, groups)
        self.assertEqual(len(client.calls), 1)
        material = loads(client.calls[0].user)
        self.assertEqual([f['id'] for f in material['facts']], ['salary'])
        self.assertEqual([r['id'] for r in material['rules']], ['payment'])

        worker_count = Fact('count', 'worker', 'worker', '工人', 'count', '2', 'integer', '',
                            KnowledgeState.KNOWN, ('rule',))
        client.calls.clear()
        attributes = ReadingResult(ProblemSpec('题目', (fact, worker_count)), RuleSet(()), (), 'test')
        reviewer.review(attributes, evidence, client, Control(), 12000, 0, groups)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual({f['id'] for f in loads(client.calls[0].user)['facts']}, {'salary', 'count'})

    def test_file_annotation_is_attached_to_all_relevant_table_batches(self):
        document = self.table()
        note = text_source('文件说明', '关联文件：orders.docx（doc）\n按已签合同处理', 'annotation_')
        for batch in DocumentBatcher().batches((document, note), 8000, 3):
            if batch.document.source.id == document.source.id:
                self.assertIn('按已签合同处理', dumps(batch.payload()['file_notes']))

    def test_missing_numeric_rows_are_reported_without_flagging_headers(self):
        document = self.table(3)
        complete = StructuredReader(RowClient()).read((document,), Control())
        self.assertEqual(missing_table_rows(complete, (document,)), ())
        fact = complete.problem.facts[0]
        partial = ReadingResult(ProblemSpec('题目', (fact,)), RuleSet(()), (), 'test')
        issues = missing_table_rows(partial, (document,))
        self.assertEqual(len(issues), 1)
        self.assertEqual({ref for ref in issues[0].evidence_ids},
                         {e.id for e in document.evidence if ':r2:' in e.id or ':r3:' in e.id})

    def test_link_review_is_bounded_and_repairs_only_invalid_issue_references(self):
        facts = tuple(Fact('f' + str(i), 'worker', 'worker' + str(i), '工人' + str(i),
                           'salary', '100', 'number', '元', KnowledgeState.KNOWN, ('e' + str(i),))
                      for i in range(40))
        evidence = tuple(Evidence('e' + str(i), 'doc', '工资表', '月薪100元') for i in range(40))
        reading = ReadingResult(ProblemSpec('题目', facts), RuleSet(()), (), 'test')

        class Client:
            configured = True
            calls = []

            def generate(self, request):
                self.calls.append(request)
                material = loads(request.user)
                target = 'invalid' if len(self.calls) == 1 else material['facts'][0]['id']
                return LLMResponse(dumps({'issues': [{'code': 'review.conflict', 'severity': 'warning',
                    'target': target, 'message': '请确认工资时点。', 'evidence_ids': []}]}), 'test')

        client = Client()
        issues = LinkReviewRole().review(reading, evidence, client, Control(), 13000, 0)
        self.assertGreater(len(client.calls), 2)
        self.assertTrue(all(len(r.system) + len(r.user) <= 13000 for r in client.calls))
        self.assertTrue(all(i.target in {f.id for f in facts} for i in issues))
        self.assertEqual(reading.problem.facts, facts)


if __name__ == '__main__':
    unittest.main()
