import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMResponse
from erp_agent.application.workbook_mapper import WorkbookMapper
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetItem,
    TemplateReading,
    TemplateSheetReading,
)
from tests.helpers import Control, FakeLLM
from tests.test_artifacts import report, template_fixture


class Memory:
    def __init__(self):
        self.entries = {}

    def get(self, key):
        return self.entries.get(key)

    def put(self, key, value):
        self.entries[key] = value


class TaskControl(Control):
    def __init__(self):
        super().__init__()
        self.tasks = []

    def report_task(self, task_id, metadata):
        self.tasks.append((task_id, metadata))


class MappingLLM(FakeLLM):
    def __init__(self, rendezvous=False):
        super().__init__("")
        self.plans = []
        self.barrier = threading.Barrier(2) if rendezvous else None

    def plan_solver(self, requests, control, concurrency):
        self.plans.append((requests, concurrency))

    def generate(self, request):
        self.calls.append(request)
        if self.barrier:
            self.barrier.wait(timeout=3)
        entries = []
        for pending in loads(request.user)["planned_inputs"]:
            item = pending["item"]
            entries.append(
                {
                    "sheet": item["sheet"],
                    "cell": item["cell"],
                    "label": item["label"],
                    "source_ids": [f"action:{item['action_indices'][0]}:amount"],
                    "conversion": "identity",
                    "explanation": "按对应期间操作金额填写。",
                }
            )
        return LLMResponse(dumps({"entries": entries}), "fake-mapper")


def setup_data(two_sheets=False):
    data = report()
    _, template = template_fixture()
    first = template.sheets[0]
    sheets = (first, replace(first, name="融资二")) if two_sheets else (first,)
    template = replace(template, sheets=sheets)
    items = tuple(
        BudgetItem(
            template.source.id,
            sheet.name,
            "B2",
            "借款",
            1,
            "元",
            None,
            (0,),
            "依据对应第一期操作填写。",
        )
        for sheet in sheets
    )
    reading = TemplateReading(
        template.source.id,
        template.source.sha256,
        tuple(TemplateSheetReading(sheet.name, "融资金额输入", "第一期") for sheet in sheets),
        reviewed=True,
    )
    return replace(
        data,
        snapshot=replace(data.snapshot, template_readings=(reading,)),
        solution=replace(data.solution, plan=replace(data.solution.plan, budget_items=items)),
    ), template


class WorkbookMapperTests(unittest.TestCase):
    def test_pending_sheets_share_dispatch_and_use_only_relevant_sources(self):
        data, template = setup_data(two_sheets=True)
        llm, control = MappingLLM(rendezvous=True), TaskControl()
        mapping = WorkbookMapper().analyze(data.solution, template, llm, control, data.snapshot)
        self.assertEqual(len(llm.plans), 1)
        self.assertEqual(len(llm.plans[0][0]), 2)
        self.assertEqual(llm.plans[0][1], 2)
        self.assertEqual(len(llm.calls), 2)
        for request in llm.calls:
            payload = loads(request.user)
            self.assertNotIn("template", payload)
            self.assertNotIn("plan", payload)
            self.assertNotIn("problem", payload)
            self.assertNotIn("rules", payload)
            self.assertEqual(
                {source["id"] for source in payload["sources"]},
                {"action:0:amount"},
            )
            self.assertTrue(payload["template_readings"][0]["reviewed"])
            self.assertEqual(len(payload["template_readings"][0]["sheets"]), 1)
            self.assertTrue(request.stream)
            self.assertEqual(request.reasoning_reserve_tokens, 8192)
        self.assertEqual({entry.source_ids for entry in mapping.entries}, {("action:0:amount",)})
        self.assertFalse(any(issue.severity == "error" for issue in mapping.issues))
        self.assertTrue(any(task_id == "mapper:validation" for task_id, _ in control.tasks))

    def test_completed_units_are_reused_and_changed_solution_is_not(self):
        data, template = setup_data()
        llm, memory = MappingLLM(), Memory()
        mapper = WorkbookMapper(memory)
        first = mapper.analyze(data.solution, template, llm, Control(), data.snapshot)
        second = mapper.analyze(data.solution, template, llm, Control(), data.snapshot)
        self.assertEqual(first.entries, second.entries)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(llm.plans), 1)
        mapper.analyze(
            replace(data.solution, id="changed"), template, llm, Control(), data.snapshot
        )
        self.assertEqual(len(llm.calls), 2)

    def test_pending_without_related_values_remains_blank_without_model_call(self):
        data, template = setup_data()
        items = tuple(
            replace(
                item,
                action_indices=(),
                period=2,
                label="期末现金",
                explanation="缺少第二期期末现金核算。",
            )
            for item in data.solution.plan.budget_items
        )
        solution = replace(data.solution, plan=replace(data.solution.plan, budget_items=items))
        llm = MappingLLM()
        mapping = WorkbookMapper().analyze(solution, template, llm, Control(), data.snapshot)
        self.assertFalse(llm.calls)
        self.assertFalse(mapping.entries[0].source_ids)
        self.assertIn("第二期", mapping.entries[0].explanation)

    def test_unplanned_target_and_unrelated_source_are_rejected_after_one_repair(self):
        data, template = setup_data()
        for bad in ({"cell": "C2"}, {"source_ids": ["fact:cash"]}):
            with self.subTest(bad=bad):
                entry = {
                    "sheet": template.sheets[0].name,
                    "cell": "B2",
                    "label": "借款",
                    "source_ids": ["action:0:amount"],
                    "conversion": "identity",
                    "explanation": "对应方案",
                    **bad,
                }
                llm = FakeLLM(dumps({"entries": [entry]}))
                with self.assertRaises(ERPError):
                    WorkbookMapper().analyze(data.solution, template, llm, Control(), data.snapshot)
                self.assertEqual(len(llm.calls), 2)

    def test_large_pending_inputs_split_by_complete_rows_and_keep_anchors(self):
        data, template = setup_data()
        items = data.solution.plan.budget_items + (
            replace(data.solution.plan.budget_items[0], cell="C2"),
            replace(data.solution.plan.budget_items[0], cell="C3"),
        )
        solution = replace(data.solution, plan=replace(data.solution.plan, budget_items=items))
        llm = MappingLLM()
        with patch("erp_agent.application.workbook_mapper.MAPPING_BATCH_CHARS", 1):
            mapping = WorkbookMapper().analyze(solution, template, llm, Control(), data.snapshot)
        self.assertEqual(len(llm.calls), 2)
        targets = [
            {pending["item"]["cell"] for pending in loads(request.user)["planned_inputs"]}
            for request in llm.calls
        ]
        self.assertEqual(targets, [{"B2", "C2"}, {"C3"}])
        self.assertEqual(len(mapping.entries), 3)

    def test_reviewed_template_without_budget_cannot_trigger_legacy_replanning(self):
        data, template = setup_data()
        solution = replace(data.solution, plan=replace(data.solution.plan, budget_items=()))
        llm = MappingLLM()
        with self.assertRaises(ERPError) as raised:
            WorkbookMapper().analyze(solution, template, llm, Control(), data.snapshot)
        self.assertEqual(raised.exception.code, "template_plan_missing")
        self.assertFalse(llm.calls)

    def test_derived_balance_does_not_use_linked_action_amount(self):
        data, template = setup_data()
        item = replace(
            data.solution.plan.budget_items[0],
            label="第一期期末现金",
            explanation="缺少第一期期末现金核算。",
        )
        solution = replace(data.solution, plan=replace(data.solution.plan, budget_items=(item,)))
        llm = MappingLLM()
        result = WorkbookMapper().analyze(solution, template, llm, Control(), data.snapshot)
        self.assertFalse(llm.calls)
        self.assertEqual(result.entries[0].source_ids, ())

    def test_unknown_currency_and_quantity_unit_do_not_guess_source_units(self):
        data, template = setup_data()
        for quantity in (False, True):
            with self.subTest(quantity=quantity):
                problem = replace(data.snapshot.reading.problem, facts=())
                problem_snapshot = replace(
                    data.snapshot, reading=replace(data.snapshot.reading, problem=problem)
                )
                item = data.solution.plan.budget_items[0]
                action = data.solution.plan.actions[0]
                if quantity:
                    item = replace(item, label="采购数量", unit="件")
                    action = replace(action, quantity=5)
                solution = replace(
                    data.solution,
                    plan=replace(data.solution.plan, budget_items=(item,), actions=(action,)),
                )
                llm = MappingLLM()
                result = WorkbookMapper().analyze(
                    solution, template, llm, Control(), problem_snapshot
                )
                self.assertFalse(llm.calls)
                self.assertEqual(result.entries[0].source_ids, ())

    def test_unsupported_unit_conversion_is_rejected_after_one_repair(self):
        data, template = setup_data()
        entry = {
            "sheet": template.sheets[0].name,
            "cell": "B2",
            "label": "借款",
            "source_ids": ["action:0:amount"],
            "conversion": "ratio_to_percent",
            "explanation": "错误转换",
        }
        llm = FakeLLM(dumps({"entries": [entry]}))
        with self.assertRaises(ERPError) as raised:
            WorkbookMapper().analyze(data.solution, template, llm, Control(), data.snapshot)
        self.assertEqual(raised.exception.code, "mapping_unit")
        self.assertEqual(len(llm.calls), 2)
