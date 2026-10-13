import unittest
from collections import Counter

from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.application.contracts import decode_reading, dumps, editable_reading, loads
from erp_agent.application.input_sources import text_source
from erp_agent.application.ports import LLMResponse
from erp_agent.application.reader import StructuredReader
from erp_agent.application.reading.batches import DocumentBatcher
from erp_agent.application.reading.coverage import missing_table_rows
from erp_agent.application.reading.roles import ReaderLimits
from erp_agent.application.reading.review import LinkReviewRole
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Evidence, Fact, KnowledgeState, ProblemSpec, ReadingResult, RuleSet, SourceDocument
from tests.helpers import Control, docx_bytes


class RowClient:
    configured = True

    def __init__(self):
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        payload = loads(request.user)
        facts, rules = [], []
        if payload['role'] == 'facts':
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
            if payload['role'] != 'facts':
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
                result = ReadingResult(ProblemSpec('题目', (fact,) if payload['role'] == 'facts' else ()),
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
        result = StructuredReader(client).read((document,), Control())
        self.assertEqual(sum(loads(r.user)['role'] == 'facts' for r in client.calls), 1)
        self.assertGreater(sum(loads(r.user)['role'] == 'rules' for r in client.calls), 1)
        self.assertEqual(len([f for f in result.problem.facts if f.category == 'order_pool']), 8)

    def test_cancellation_after_call_prevents_next_subrole(self):
        class CancelControl(Control):
            def checkpoint(self, stage, completed=0):
                if len(self.stages) == 2:
                    raise Cancelled()
                super().checkpoint(stage, completed)

        client = RowClient()
        with self.assertRaises(Cancelled):
            StructuredReader(client).read((self.table(),), CancelControl())
        self.assertEqual(len(client.calls), 1)

    def test_long_paragraph_is_split_without_silently_dropping_text(self):
        document = text_source('长段落', '经营规则' * 10000, 'manual_')
        batches = DocumentBatcher().batches((document,), 10000, 3)
        self.assertGreater(len(batches), 1)
        self.assertEqual(''.join(unit.text for batch in batches for unit in batch.units),
                         document.blocks[0].text)
        self.assertTrue(all(len(dumps(batch.payload())) <= 10000 for batch in batches))

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
