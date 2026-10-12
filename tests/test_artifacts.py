import io
import unittest
from unittest.mock import patch
from dataclasses import replace
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation
from docx import Document

from erp_agent.adapters.artifacts import DocxReportRenderer, XlsxWorkbookRenderer
from erp_agent.adapters.budget_templates import SpreadsheetBudgetParser
from erp_agent.application.budget_context import budget_material
from erp_agent.application.codec import budget_template
from erp_agent.application.contracts import decode_proposal, dumps, loads, to_json
from erp_agent.application.workbook_mapper import WorkbookMapper
from erp_agent.domain.models import BudgetItem, ReportData, SolutionBundle, SolverMode, SourceDocument, WorkbookEntry
from erp_agent.domain.workbook import validate_mapping
from tests.helpers import snapshot, proposal_json, Control, FakeLLM
from erp_agent.application.solver import DraftPlanEvaluator


def report():
    problem = snapshot()
    plan = decode_proposal(proposal_json(), 'fake').baseline
    solution = SolutionBundle('solution', problem.project_id, 1, SolverMode.LLM_ONLY,
                              plan, DraftPlanEvaluator().evaluate(problem, plan), None, 1, 1, None, 'fake', '')
    return ReportData(problem, solution)


def template_fixture(transposed=False):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '借款预算' if not transposed else '年度预算'
    if transposed:
        sheet.append(['期间', '借款'])
        sheet.append(['第1季', None])
        sheet['B2'].number_format = '0.00'
    else:
        sheet.append(['项目', '第1季', '第2季'])
        sheet.append(['借款', None, None])
        sheet['B2'].number_format = '0.00'
        sheet['B3'] = '=SUM(B2)'
        sheet.merge_cells('A4:C4')
        sheet['A4'] = '说明'
    content = io.BytesIO()
    workbook.save(content)
    workbook.close()
    original = content.getvalue()
    template = SpreadsheetBudgetParser().parse(SourceDocument('template', 'budget.xlsx', 'hash', len(original), 'xlsx'), original)
    return original, template


class ArtifactTests(unittest.TestCase):
    def test_formula_driven_inputs_use_agent_even_with_partial_previous_budget(self):
        data = report()
        workbook = Workbook()
        first = workbook.active
        first.title = '第一年'
        first.append(['项目', '第一季', '年度合计'])
        first.append(['借款', None, '=SUM(B2)'])
        first['B2'].number_format = '0.00'
        first['B3'] = 0
        second = workbook.create_sheet('生产')
        second.append(['期间', '操作', '期初资本', '跨表合计'])
        second.append(['第一季', None, None, "='第一年'!C2"])
        second['B2'].number_format = '@'
        second['C2'].number_format = '0'
        second['A3'] = '内部'
        second['B3'].number_format = '0'
        second.row_dimensions[3].hidden = True
        validation = DataValidation(type='whole', operator='greaterThanOrEqual', formula1=0)
        validation.add(second['C2'])
        second.add_data_validation(validation)
        buffer = io.BytesIO()
        workbook.save(buffer)
        content = buffer.getvalue()
        template = SpreadsheetBudgetParser().parse(SourceDocument('template', 'budget.xlsx', 'hash', len(content), 'xlsx'), content)
        self.assertEqual(budget_template(to_json(template)), template)
        material = budget_material((template,), include_formula_references=True)[0]
        self.assertEqual(material['sheets'][0]['formula_references'], [['C2', '第一年', ['B2']]])
        self.assertEqual(material['sheets'][1]['validations'][0]['kind'], 'whole')
        self.assertFalse(next(c for c in template.sheets[1].cells if c.coordinate == 'B3').writable)
        old = BudgetItem('template', '第一年', 'B2', '借款', 1, '元', '10', (0,), '旧预算')
        data = replace(data, solution=replace(data.solution, plan=replace(data.solution.plan, budget_items=(old,))))

        class MappingLLM(FakeLLM):
            def generate(self, request):
                current = loads(request.user)['template']['sheets'][0]['name']
                entries = [
                    {'sheet': current, 'cell': 'B2', 'label': '借款' if current == '第一年' else '操作',
                     'source_ids': ['action:0:amount' if current == '第一年' else 'action:0:description'],
                     'conversion': 'identity', 'explanation': '第一季方案'},
                    {'sheet': current, 'cell': 'C2', 'label': '汇总' if current == '第一年' else '期初资本',
                     'source_ids': [] if current == '第一年' else ['fact:cash'],
                     'conversion': 'identity', 'explanation': '保留公式或引用题目初始值'},
                ]
                self.response = dumps({'entries': entries})
                return super().generate(request)

        llm = MappingLLM('')
        with patch('erp_agent.application.workbook_mapper.MAPPING_BATCH_CHARS', 1):
            mapping = WorkbookMapper().analyze(data.solution, template, llm, Control(), data.snapshot)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(len(mapping.entries), 3)
        self.assertFalse(any(i.severity == 'error' for i in mapping.issues))
        filled = XlsxWorkbookRenderer().render(data, template, content, replace(mapping, confirmed=True))
        reopened = load_workbook(io.BytesIO(filled.content), data_only=False)
        self.assertEqual(reopened['第一年']['C2'].value, '=SUM(B2)')
        self.assertEqual(reopened['第一年']['B3'].value, 0)
        self.assertEqual(reopened['生产']['D2'].value, "='第一年'!C2")
        self.assertEqual(reopened['生产']['B2'].value, data.solution.plan.actions[0].description)
        self.assertEqual(reopened['生产']['C2'].value, 100000)
        self.assertTrue(reopened['生产'].row_dimensions[3].hidden)
        self.assertEqual(str(reopened['生产'].data_validations), str(second.data_validations))
        self.assertTrue(any(i.severity == 'error' for i in validate_mapping(
            template, (WorkbookEntry('生产', 'B3', '内部', ('action:0:amount',)),), data.solution)))
        reopened.close()
        workbook.close()

    def test_report_covers_all_sixteen_periods_and_preserves_operating_values(self):
        data = report()
        problem = replace(data.snapshot, reading=replace(data.snapshot.reading, problem=replace(data.snapshot.reading.problem, years=4, periods_per_year=4)))
        plan = replace(data.solution.plan, actions=tuple(replace(data.solution.plan.actions[0], period=p) for p in range(1, 17)))
        result = DocxReportRenderer().render(replace(data, snapshot=problem, solution=replace(data.solution, plan=plan)))
        document = Document(io.BytesIO(result.content))
        self.assertIn('第 4 年 · 第 4 季', [p.text for p in document.paragraphs])
        tables = [t for t in document.tables if t.cell(0, 0).text == '对象']
        self.assertEqual(len(tables), 16)
        self.assertTrue(all(t.cell(1, 3).text == '10' for t in tables))
        self.assertTrue(any('草稿' in p.text for p in document.paragraphs))

    def test_two_template_layouts_reference_sources_without_inventing_values(self):
        data = report()
        for transposed in (False, True):
            original, template = template_fixture(transposed)
            sheet_name = template.sheets[0].name
            entry = {'sheet': sheet_name, 'cell': 'B2', 'label': '借款', 'source_ids': ['action:0:amount'], 'conversion': 'identity', 'explanation': '第一季借款操作金额'}
            llm = FakeLLM(dumps({'entries': [entry, {**entry, 'cell': 'B3' if not transposed else 'A2', 'source_ids': [], 'explanation': '保留原值或公式'}]}))
            mapping = WorkbookMapper().analyze(data.solution, template, llm, Control())
            self.assertFalse(any(i.severity == 'error' for i in mapping.issues))
            filled = XlsxWorkbookRenderer().render(data, template, original, replace(mapping, confirmed=True))
            reopened = load_workbook(io.BytesIO(filled.content), data_only=False)
            self.assertEqual(reopened[sheet_name]['B2'].value, 10)
            self.assertEqual(reopened[sheet_name]['B2'].number_format, '0.00')
            if not transposed:
                self.assertEqual(reopened[sheet_name]['B3'].value, '=SUM(B2)')
                self.assertIn('A4:C4', [str(r) for r in reopened[sheet_name].merged_cells.ranges])
                self.assertEqual(filled.formula_status, 'pending')
            self.assertTrue(any('草稿' in str(c.value) for row in reopened['填表说明'] for c in row))
            reopened.close()
            unchanged = load_workbook(io.BytesIO(original))
            self.assertIsNone(unchanged[sheet_name]['B2'].value)
            unchanged.close()

    def test_readonly_and_unknown_sources_cannot_be_confirmed(self):
        data = report()
        _, template = template_fixture()
        sheet = template.sheets[0].name
        for entry in (WorkbookEntry(sheet, 'B3', '公式', ('action:0:amount',)),
                      WorkbookEntry(sheet, 'B4', '合并续格', ('action:0:amount',)),
                      WorkbookEntry(sheet, 'A2', '标题', ('action:0:amount',)),
                      WorkbookEntry(sheet, 'B2', '未知', ('fake-value',))):
            self.assertTrue(any(i.severity == 'error' for i in validate_mapping(template, (entry,), data.solution)))
