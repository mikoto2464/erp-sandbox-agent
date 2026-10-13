import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from openpyxl import Workbook

from erp_agent.adapters.budget_templates import SpreadsheetBudgetParser
from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.api.server import create_workflow
from erp_agent.application.contracts import dumps, loads, obj, to_json
from erp_agent.application.ports import LLMResponse
from erp_agent.application.template_reader import TemplateReader, template_material
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Issue, SourceDocument, TemplateReading
from tests.helpers import Control, FakeLLM, snapshot


def workbook_bytes(rows=24):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "预算"
    sheet.append(["项目", "第一季"])
    for index in range(2, rows + 1):
        sheet.cell(index, 1, "原料支出")
        sheet.cell(index, 2).number_format = "0.00"
    sheet.cell(rows + 1, 2, f"=SUM(B2:B{rows})")
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def output(target="B2"):
    return dumps(
        {
            "purpose": "现金预算",
            "periods": "第一季",
            "input_groups": [
                {"label": "原料支出", "range": target, "unit": "元", "description": "现金支出输入"}
            ],
            "issues": [],
        }
    )


class TemplateReaderTests(unittest.TestCase):
    def test_library_reviews_each_issue_and_preserves_answers_on_import(self):
        from erp_agent.application.codec import template_reading
        from erp_agent.application.template_library import LIBRARY_SCOPE

        with tempfile.TemporaryDirectory() as directory:
            workflow = create_workflow(Settings(data_dir=Path(directory)), FakeLLM(output()))
            try:
                library = workflow.template_library
                job = library.upload("budget.xlsx", workbook_bytes())
                workflow.jobs.futures[job["id"]].result(3)
                tid = job["metadata"]["template_id"]
                first = Issue("template.period", "warning", tid, "预算：期间是否为第一季？")
                second = Issue("template.unit", "warning", tid, "预算：金额是否以元填写？")
                reading = TemplateReading(tid, "hash", (), (first, second),
                                          explanation="旧版整体说明")
                workflow.repository.put("template_reading", LIBRARY_SCOPE, tid, reading)
                with self.assertRaisesRegex(ERPError, "2 条疑点"):
                    library.review(tid, {"reviewed": True, "explanation": "整体回答不能代替逐条处理"})
                with self.assertRaisesRegex(ERPError, "这条疑点"):
                    library.review(tid, {"issue": to_json(first), "answer": " "})
                with self.assertRaisesRegex(ERPError, "疑点已变更"):
                    library.review(tid, {"issue": to_json(replace(first, message="改动后的问题")),
                                         "answer": "第一季"})
                saved = template_reading(library.review(tid, {"issue": to_json(first),
                                                             "answer": "第一季"}))
                self.assertFalse(saved.reviewed)
                self.assertEqual(saved.explanation, "旧版整体说明")
                self.assertEqual(len(saved.issue_reviews), 1)
                with self.assertRaisesRegex(ERPError, "1 条疑点"):
                    library.review(tid, {"reviewed": True})
                library.review(tid, {"issue": to_json(second), "answer": "以元填写"})
                library.review(tid, {"reviewed": True})
                stored = template_reading(obj(library.detail(tid)["reading"]))
                self.assertTrue(stored.reviewed)
                self.assertEqual([(r.issue, r.answer) for r in stored.issue_reviews],
                                 [(first, "第一季"), (second, "以元填写")])
                revised = template_reading(library.review(tid, {"issue": to_json(first),
                                                               "answer": "第一年四季"}))
                self.assertFalse(revised.reviewed)
                self.assertEqual(len(revised.issue_reviews), 2)
                self.assertEqual(next(r.answer for r in revised.issue_reviews if r.issue == second),
                                 "以元填写")
                library.review(tid, {"reviewed": True})
                project = workflow.create_project("项目")["id"]
                actual = library.detail(tid)["template"]["source"]["sha256"]
                stored = template_reading(obj(library.detail(tid)["reading"]))
                workflow.repository.put("template_reading", LIBRARY_SCOPE, tid,
                                        replace(stored, sha256=actual))
                imported = workflow.import_library_template(project, {"template_id": tid})
                copy = template_reading(workflow.repository.get(
                    "imported_template_reading", project, imported["template_id"]))
                self.assertEqual([r.answer for r in copy.issue_reviews],
                                 [r.answer for r in stored.issue_reviews])
                self.assertEqual({r.issue for r in copy.issue_reviews}, set(copy.issues))
                self.assertTrue(copy.reviewed)
            finally:
                workflow.jobs.close()
                workflow.memory.close()

    def test_gateway_failure_splits_then_restart_reuses_completed_segments(self):
        content = workbook_bytes()
        template = SpreadsheetBudgetParser().parse(
            SourceDocument("template", "budget.xlsx", "hash", len(content), "xlsx"), content
        )
        calls, plans = [], []

        class Client:
            configured = True
            failing = True

            def plan_reading(self, requests, control, concurrency):
                plans.append([r.task.id for r in requests])

            def generate(self, request):
                data = loads(request.user)
                calls.append(data)
                if "row_window" not in data or (data["row_window"][0] > 1 and self.failing):
                    raise ERPError("llm_gateway_timeout", "HTTP 504", 504)
                first = data["row_window"][0]
                return LLMResponse(output(f"B{max(2, first)}"), "test")

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteRepository(Path(directory) / "erp.sqlite3")
            reader, client = TemplateReader(repository), Client()
            with self.assertRaises(ERPError):
                reader.read(template, client, Control())
            first_segment_calls = sum(d.get("row_window") == [1, 13] for d in calls)
            client.failing = False
            reading = reader.read(template, client, Control())
            self.assertEqual(
                sum(d.get("row_window") == [1, 13] for d in calls), first_segment_calls
            )
            before = len(calls)
            again = reader.read(template, client, Control())
            self.assertEqual(len(calls), before)
            self.assertEqual(reading, again)
            self.assertEqual(len(plans), 2)
            material = template_material(template, template.sheets[0])
            self.assertNotIn("blank_ranges", material["sheet"])
            self.assertFalse(any("SUM" in c[1] for c in material["sheet"]["cells"]))
            self.assertNotIn("current_problem_context", calls[0])
            self.assertEqual(sum("row_window" not in d for d in calls), 1)
            self.assertNotIn("formula_references", material["sheet"])
            self.assertEqual(material["sheet"]["input_ranges"], [["B2:B24", "0.00"]])

    def test_library_import_is_model_free_and_survives_project_and_library_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeLLM(output())
            workflow = create_workflow(Settings(data_dir=Path(directory)), client)
            try:
                library = workflow.template_library
                job = library.upload("budget.xlsx", workbook_bytes())
                workflow.jobs.futures[job["id"]].result(3)
                tid = job["metadata"]["template_id"]
                job = library.read(tid)
                workflow.jobs.futures[job["id"]].result(3)
                library.review(tid, {"reviewed": True, "explanation": ""})
                calls = len(client.calls)
                p1 = workflow.create_project("一")["id"]
                p2 = workflow.create_project("二")["id"]
                problem = replace(snapshot(), project_id=p1, document_ids=())
                workflow.repository.save_snapshot(p1, obj(to_json(problem)), 0)
                imported1 = workflow.import_library_template(p1, {"template_id": tid})
                imported2 = workflow.import_library_template(p2, {"template_id": tid})
                self.assertEqual(len(client.calls), calls)
                self.assertEqual(imported1["version"], 2)
                self.assertIsNone(imported2["version"])
                self.assertTrue(
                    workflow.repository.snapshot(p1)["template_readings"][0]["reviewed"]
                )
                self.assertNotEqual(imported1["template_id"], imported2["template_id"])
                workflow.delete_project(p1)
                self.assertEqual(len(library.items()), 1)
                library.delete(tid)
                self.assertEqual(library.items(), [])
                preserved = workflow.repository.get("template", p2, imported2["template_id"])
                self.assertTrue(any(c["formula"] for s in preserved["sheets"] for c in s["cells"]))
                self.assertTrue(
                    workflow.repository.get(
                        "imported_template_reading", p2, imported2["template_id"]
                    )["reviewed"]
                )
            finally:
                workflow.jobs.close()
                workflow.memory.close()

class TemplateInputTests(unittest.TestCase):
    def test_layout_roundtrip_and_groups_exclude_formula_and_existing_data(self):
        from erp_agent.application.codec import budget_template
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['项目', '第一季', '合计'])
        sheet['A2'] = '材料费'
        sheet['B2'].number_format = '0.00'
        sheet['C2'] = '=SUM(B2)'
        sheet['A3'] = '已有数据'
        sheet['B3'] = 20
        sheet.column_dimensions['A'].width = 28
        sheet.row_dimensions[2].height = 30
        sheet.merge_cells('A4:C4')
        sheet['A4'] = '说明'
        buffer = io.BytesIO()
        workbook.save(buffer)
        content = buffer.getvalue()
        template = SpreadsheetBudgetParser().parse(
            SourceDocument('layout', 'budget.xlsx', 'hash', len(content), 'xlsx'), content)
        restored = budget_template(obj(to_json(template)))
        self.assertEqual(restored, template)
        self.assertGreater(restored.sheets[0].column_widths[0], 180)
        self.assertEqual(restored.sheets[0].row_heights[1], 40)
        reading = TemplateReader().read(template, FakeLLM(output('B2:C3')), Control())
        self.assertEqual([g.range for g in reading.sheets[0].input_groups], ['B2'])

    def test_import_before_topic_read_is_automatic_and_project_reading_is_rejected(self):
        from tests.helpers import docx_bytes
        with tempfile.TemporaryDirectory() as directory:
            client = FakeLLM(output())
            workflow = create_workflow(Settings(data_dir=Path(directory)), client)
            try:
                project = workflow.create_project('项目')['id']
                library = workflow.template_library
                job = library.upload('budget.xlsx', workbook_bytes())
                workflow.jobs.futures[job['id']].result(3)
                tid = job['metadata']['template_id']
                job = library.read(tid)
                workflow.jobs.futures[job['id']].result(3)
                library.review(tid, {'reviewed': True})
                imported = workflow.import_library_template(project, {'template_id': tid})
                with self.assertRaises(ERPError) as rejected:
                    workflow.read(project, {'template_ids': [imported['template_id']]})
                self.assertEqual(rejected.exception.code, 'template_library_required')
                with self.assertRaises(ERPError) as rejected:
                    workflow.upload_template(project, 'budget.xlsx', workbook_bytes())
                self.assertEqual(rejected.exception.code, 'template_library_required')
                job = workflow.upload(project, 'topic.docx', docx_bytes())
                workflow.jobs.futures[job['id']].result(3)
                client.response = dumps({'title': '赛题', 'facts': [], 'rules': [], 'issues': []})
                job = workflow.read(project, {})
                workflow.jobs.futures[job['id']].result(3)
                result = workflow.repository.snapshot(project)
                self.assertEqual(result['template_readings'][0]['template_id'], imported['template_id'])
                self.assertTrue(result['template_readings'][0]['reviewed'])
            finally:
                workflow.jobs.close()
                workflow.memory.close()

    def test_timeout_segments_reuse_initial_model_assignment(self):
        from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
        from erp_agent.application.ports import ModelBinding
        calls = []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                calls.append((self.name, request.schema_name))
                data = loads(request.user)
                if request.schema_name == 'erp_reader_dispatch':
                    return LLMResponse(dumps({'tasks': [
                        {'task_id': t['task_id'], 'difficulty': 'simple',
                         'assignments': [{'preset_id': 'helper', 'reason': '空白标签归类'}],
                         'summary': '协作模型整理空白输入'} for t in data['tasks']]}), self.name)
                if request.schema_name == 'erp_model_dispatch':
                    raise AssertionError('超时分段应复用初次分配')
                if 'row_window' not in data:
                    raise ERPError('llm_gateway_timeout', 'HTTP 504', 504)
                first = max(2, data['row_window'][0])
                return LLMResponse(output(f'B{first}'), self.name)

        models = tuple(ModelBinding(name, name, 1, {
            'model': name, 'priority': priority, 'max_parallel': 2, 'max_input_chars': 100000
        }, Client(name)) for name, priority in [('main', 100), ('helper', 80)])
        scheduled = ScheduledLLMClient(models, ModelCapacity())
        content = workbook_bytes()
        template = SpreadsheetBudgetParser().parse(
            SourceDocument('scheduled', 'budget.xlsx', 'hash', len(content), 'xlsx'), content)
        reading = TemplateReader().read(template, scheduled, Control())
        self.assertEqual(len(reading.sheets), 1)
        self.assertEqual(calls.count(('main', 'erp_reader_dispatch')), 1)
        self.assertEqual(calls.count(('helper', 'erp_template_reading')), 3)
        self.assertEqual(scheduled.capacity.used, {'main': 0, 'helper': 0})

    def test_legacy_unread_template_moves_to_library_without_model_call(self):
        import base64
        with tempfile.TemporaryDirectory() as directory:
            client = FakeLLM(output())
            workflow = create_workflow(Settings(data_dir=Path(directory)), client)
            try:
                project = workflow.create_project('旧项目')['id']
                content = workbook_bytes()
                source = SourceDocument('legacy', 'budget.xlsx', 'f' * 64, len(content), 'xlsx')
                template = SpreadsheetBudgetParser().parse(source, content)
                workflow.repository.put('template', project, source.id, template)
                workflow.repository.put('template_original', project, source.id, {
                    'source': to_json(source), 'content': base64.b64encode(content).decode()})
                workflow.migrate_legacy_templates()
                workflow.migrate_legacy_templates()
                items = workflow.template_library.items()
                self.assertEqual(len(items), 1)
                self.assertIsNone(items[0]['reading'])
                self.assertTrue(items[0]['parsed'])
                self.assertEqual(client.calls, [])
                self.assertEqual(len(workflow.repository.list('template', project)), 1)
            finally:
                workflow.jobs.close()
                workflow.memory.close()
