import threading
import unittest
from dataclasses import replace
from decimal import Decimal

from erp_agent.application.budget_codec import decode_budget_result
from erp_agent.application.budget_execution import CheckedInput, expand_mappings
from erp_agent.application.contracts import dumps, loads, to_json
from erp_agent.application.ports import LLMResponse
from erp_agent.application.solution_stages import plan_hash
from erp_agent.application.solver_budget import PlanBudgetPlanner
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Action,
    BudgetCell,
    BudgetSheet,
    BudgetTemplate,
    OutputSource,
    Plan,
    PlanningContext,
    SourceDocument,
    TemplateInputGroup,
    TemplateReading,
    TemplateSheetReading,
)
from tests.helpers import Control, FakeLLM, snapshot


def fixture(two_sheets=False, dependency=False):
    action = Action(
        1,
        "borrow",
        "company",
        Decimal("100.25"),
        None,
        "安排借款",
        action_id="borrowing",
        amount_unit="元",
        amount_meaning="financing_principal",
        payment_period=1,
        schema_version="2",
    )
    plan = Plan("方案", "草稿", (action,), (), ())
    first = BudgetSheet(
        "融资",
        3,
        3,
        (BudgetCell("A1", "借款"), BudgetCell("B2", ""), BudgetCell("C2", "=B2", formula=True)),
    )
    sheets = (first, replace(first, name="现金")) if two_sheets else (first,)
    template = BudgetTemplate(SourceDocument("template", "空表.xlsx", "hash", 0, "xlsx"), sheets)
    read_sheets = tuple(
        TemplateSheetReading(
            s.name,
            "融资输入",
            "第一期",
            (
                TemplateInputGroup(
                    "借款", "B2", "元", "本金", ("融资",) if dependency and s.name == "现金" else ()
                ),
            ),
        )
        for s in sheets
    )
    reading = TemplateReading("template", "hash", read_sheets, reviewed=True)
    snap = replace(snapshot(), template_readings=(reading,))
    return snap, plan, template


def mapping(range="B2", refs=("action:borrowing:amount",), calculation="direct", gap_id=None):
    return {
        "group_id": "group:0",
        "target_range": range,
        "source_ids": list(refs),
        "period_axis": "single",
        "periods": [1],
        "conversion": "identity",
        "calculation": calculation,
        "gap_id": gap_id,
    }


class BudgetExecutionTests(unittest.TestCase):
    def test_python_decimal_expansion_protects_formula_and_deduplicates_gaps(self):
        snap, plan, template = fixture()
        group = CheckedInput("group:0", "借款", "B2:C2", "元", "本金")
        sources = (OutputSource("action:borrowing:amount", "借款", "100.25", True, 1, "元"),)
        data = {"status": "applicable", "mappings": [mapping("B2:C2")], "gaps": []}
        items, gaps = expand_mappings(
            data, template, template.sheets[0], (group,), sources, plan, 16
        )
        self.assertEqual([(i.cell, i.value) for i in items], [("B2", "100.25")])
        self.assertEqual(items[0].source_ids, ("action:borrowing:amount",))
        self.assertFalse(gaps)
        self.assertEqual(items[0].action_ids, ("borrowing",))

    def test_decimal_sum_and_conversion_uses_sources_and_not_model_value(self):
        from erp_agent.domain.models import WorkbookEntry
        from erp_agent.domain.workbook import mapped_value

        sources = (
            OutputSource("a", "金额", "10000.10", True, 1, "元"),
            OutputSource("b", "金额", "20000.20", True, 2, "元"),
        )
        value = mapped_value(
            WorkbookEntry("预算", "B2", "合计", ("a", "b"), "sum_yuan_to_wan"), sources
        )
        self.assertEqual(value, Decimal("3.000030"))

    def test_unknown_named_rule_is_blank_and_one_gap_for_range(self):
        _, plan, template = fixture()
        sheet = replace(template.sheets[0], cells=(BudgetCell("B2", ""), BudgetCell("C2", "")))
        group = CheckedInput("group:0", "余额", "B2:C2", "元", "未知核算")
        data = {
            "status": "applicable",
            "mappings": [mapping("B2:C2", (), "cash_balance")],
            "gaps": [],
        }
        items, gaps = expand_mappings(data, template, sheet, (group,), (), plan, 16)
        self.assertTrue(all(i.value is None for i in items))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(len(gaps[0].targets), 2)
        self.assertEqual({i.gap_id for i in items}, {gaps[0].id})

    def test_unbound_values_empty_success_and_wrong_units_are_rejected(self):
        _, plan, template = fixture()
        group = CheckedInput("group:0", "借款", "B2", "元", "本金")
        for data in (
            {"status": "applicable", "mappings": [], "gaps": []},
            {"status": "applicable", "mappings": [mapping(refs=())], "gaps": []},
        ):
            with self.assertRaises(ERPError):
                expand_mappings(data, template, template.sheets[0], (group,), (), plan, 16)
        sources = (OutputSource("action:borrowing:amount", "借款", "100.25", True, 1, "万元"),)
        with self.assertRaises(ERPError):
            expand_mappings(
                {"status": "applicable", "mappings": [mapping()], "gaps": []},
                template,
                template.sheets[0],
                (group,),
                sources,
                plan,
                16,
            )

    def test_stage_deterministic_merge_parallel_dependency_and_no_plan_mutation(self):
        for dependency in (False, True):
            with self.subTest(dependency=dependency):
                snap, plan, template = fixture(two_sheets=True, dependency=dependency)
                barrier = None if dependency else threading.Barrier(2)
                order = []

                class Model(FakeLLM):
                    def generate(self, request, barrier=barrier, order=order):
                        payload = loads(request.user)
                        if barrier:
                            barrier.wait(3)
                        order.append(payload["sheet"])
                        return LLMResponse(
                            dumps({"status": "applicable", "mappings": [mapping()], "gaps": []}),
                            "fake",
                        )

                seen = []
                result = PlanBudgetPlanner(Model(""), Control(), None).generate(
                    snap,
                    PlanningContext(templates=(template,)),
                    plan,
                    solution_id="solution",
                    on_sheet=seen.append,
                )
                self.assertEqual([i.sheet for i in result.items], ["融资", "现金"])
                self.assertEqual(plan.budget_items, ())
                self.assertEqual(result.plan_hash, plan_hash(plan))
                self.assertEqual(len(seen), 2)
                if dependency:
                    self.assertEqual(order, ["融资", "现金"])
                self.assertEqual(decode_budget_result(to_json(result), plan), result)

    def test_same_version_cache_isolated_from_changed_plan_and_cancellation_retains_success(self):
        from erp_agent.domain.errors import Cancelled
        from tests.test_workbook_mapper import Memory

        snap, plan, template = fixture(two_sheets=True, dependency=True)

        class Model(FakeLLM):
            def generate(self, request):
                self.calls.append(request)
                return LLMResponse(
                    dumps({"status": "applicable", "mappings": [mapping()], "gaps": []}), "fake"
                )

        model, memory = Model(""), Memory()
        context = PlanningContext(templates=(template,))
        PlanBudgetPlanner(model, Control(), memory).generate(
            snap, context, plan, solution_id="solution"
        )
        PlanBudgetPlanner(model, Control(), memory).generate(
            snap, context, plan, solution_id="solution"
        )
        self.assertEqual(len(model.calls), 2)
        changed = replace(plan, actions=(replace(plan.actions[0], amount=Decimal("101")),))
        PlanBudgetPlanner(model, Control(), memory).generate(
            snap, context, changed, solution_id="new"
        )
        self.assertEqual(len(model.calls), 4)
        seen = []

        class CancelControl(Control):
            should_cancel = False

            def checkpoint(self, phase):
                if self.should_cancel:
                    raise Cancelled()

        control = CancelControl()

        def cancel_after_sheet(partial):
            seen.append(partial)
            control.should_cancel = True

        with self.assertRaises(Cancelled):
            PlanBudgetPlanner(Model(""), control, None).generate(
                snap, context, plan, on_sheet=cancel_after_sheet
            )
        self.assertEqual([i.sheet for i in seen[-1].items], ["融资"])
        self.assertEqual(plan.budget_items, ())

    def test_failed_sheet_keeps_completed_sheet_and_local_patch_does_not_regenerate_plan(self):
        snap, plan, template = fixture(two_sheets=True)

        class Model(FakeLLM):
            def generate(self, request):
                self.calls.append(request)
                payload = loads(request.user)
                if payload.get("sheet") == "现金":
                    raise ERPError("llm_bad_request", "测试失败")
                return LLMResponse(
                    dumps({"status": "applicable", "mappings": [mapping()], "gaps": []}), "fake"
                )

        seen = []
        with self.assertRaises(ERPError):
            PlanBudgetPlanner(Model(""), Control(), None).generate(
                snap,
                PlanningContext(templates=(template,)),
                plan,
                solution_id="solution",
                on_sheet=seen.append,
            )
        self.assertEqual(seen[-1].status, "failed")
        self.assertEqual([i.sheet for i in seen[-1].items], ["融资"])
        self.assertEqual(plan.budget_items, ())

        class Repair(FakeLLM):
            def generate(self, request):
                self.calls.append(request)
                if request.schema_name == "erp_budget_patch":
                    self.assert_patch = loads(request.user)
                    return LLMResponse(
                        dumps({"patches": [{"index": 0, "mapping": mapping()}], "gaps": []}), "fake"
                    )
                return LLMResponse(
                    dumps(
                        {"status": "applicable", "mappings": [mapping(refs=("bad",))], "gaps": []}
                    ),
                    "fake",
                )

        model = Repair("")
        PlanBudgetPlanner(model, Control(), None).generate(
            snap,
            PlanningContext(templates=(replace(template, sheets=(template.sheets[0],)),)),
            plan,
        )
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn("plan", model.assert_patch)
        self.assertEqual(model.assert_patch["error"]["code"], "budget.source")
