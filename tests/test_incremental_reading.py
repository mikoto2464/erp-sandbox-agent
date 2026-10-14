import unittest
from dataclasses import replace

from erp_agent.application.answerer import AnswererService
from erp_agent.application.input_sources import text_source
from erp_agent.application.reading.incremental import merge_incremental
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetTemplate,
    Fact,
    KnowledgeState,
    ProblemSpec,
    ReadingResult,
    ReportData,
    RuleSet,
    SourceDocument,
    TemplateReading,
)
from tests.helpers import snapshot


class IncrementalReadingTests(unittest.TestCase):
    def test_conflicting_append_keeps_saved_id_explanation_and_both_sources(self):
        old_doc = text_source("old", "本金100元", "old_")
        new_doc = text_source("new", "本金200元", "new_")
        old = Fact("saved", "initial", "company", "公司", "initial_cash", "100",
                   "number", "元", KnowledgeState.KNOWN, (old_doc.evidence[0].id,),
                   explanation="用户已核对本金")
        added = replace(old, id="new-id", value="200", evidence_ids=(new_doc.evidence[0].id,),
                        explanation="新增资料")
        first = ReadingResult(ProblemSpec("题目", (old,)), RuleSet(()), (), "test")
        second = replace(first, problem=replace(first.problem, facts=(added,)))
        result = merge_incremental(first, second, (old_doc, new_doc))
        fact = next(f for f in result.problem.facts if f.key == "initial_cash")
        self.assertEqual(fact.id, "saved")
        self.assertEqual(fact.value, "100")
        self.assertEqual(fact.state, KnowledgeState.CONFLICTED)
        self.assertEqual(fact.evidence_ids, old.evidence_ids + added.evidence_ids)
        self.assertIn("用户已核对本金", fact.explanation)
        conflicts = [i for i in result.issues if i.code == "reading.source_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].target, "saved")

    def test_filling_rejects_template_changed_since_reader_confirmation(self):
        problem = replace(snapshot(), template_readings=(
            TemplateReading("template", "original", (), (), reviewed=True),))
        template = BudgetTemplate(
            SourceDocument("template", "budget.xlsx", "changed", 1, "xlsx"), ())
        report = ReportData(problem, None, ())
        with self.assertRaises(ERPError) as caught:
            AnswererService._require_planned_template(None, report, template)
        self.assertEqual(caught.exception.code, "template_changed")
