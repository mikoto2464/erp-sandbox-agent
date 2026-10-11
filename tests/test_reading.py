import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_reading, dumps, editable_reading, loads
from erp_agent.application.reader import StructuredReader
from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.adapters.model_settings import RuntimeLLMClient
from erp_agent.application.ports import LLMResponse
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Evidence, Fact, KnowledgeState, ProblemSpec, ReadingResult, RuleSet, SourceDocument,
)
from erp_agent.domain.validation import validate_reading
from tests.helpers import Control, FakeLLM, docx_bytes


class ReadingTests(unittest.TestCase):
    def result(self, value="100000", conversion="identity", kind="number", refs=("p1",)):
        fact = Fact("cash", "initial", "company", "公司", "initial_cash", value, kind,
                    "元", KnowledgeState.KNOWN, refs, conversion)
        return ReadingResult(ProblemSpec("题目", (fact,)), RuleSet(()), (), "fake")

    def test_supported_value_and_chinese_prefix(self):
        result = validate_reading(self.result(), (Evidence("p1", "doc", "段落", "初始资本100000元"),))
        self.assertEqual(result.problem.facts[0].state, KnowledgeState.KNOWN)

    def test_wrong_value_and_bad_reference(self):
        for result in [self.result("72000"), self.result(refs=("wrong",))]:
            checked = validate_reading(result, (Evidence("p1", "doc", "格", "60000"),))
            self.assertEqual(checked.problem.facts[0].state, KnowledgeState.CONFLICTED)
            self.assertTrue(any(i.severity == "error" for i in checked.issues))

    def test_percent_wan_and_header_conversion(self):
        cases = [("0.5", "percent_to_ratio", "50%"), ("100000", "wan_to_yuan", "10万元")]
        for value, conversion, text in cases:
            checked = validate_reading(self.result(value, conversion),
                                       (Evidence("p1", "doc", "格", text),))
            self.assertEqual(checked.problem.facts[0].state, KnowledgeState.KNOWN)
        checked = validate_reading(self.result("0.5", "percent_to_ratio", refs=("p1", "header")),
                                   (Evidence("p1", "doc", "格", "50"),
                                    Evidence("header", "doc", "表头", "效率（%）")))
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.KNOWN)

    def test_unknown_is_not_zero(self):
        checked = validate_reading(self.result(None), ())
        self.assertIsNone(checked.problem.facts[0].value)
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.MISSING)

    def test_assumption_preserved_and_duplicates_flagged(self):
        reading = self.result("72000")
        fact = replace(reading.problem.facts[0], state=KnowledgeState.ASSUMED, evidence_ids=())
        checked = validate_reading(replace(reading, problem=replace(reading.problem, facts=(fact, fact))), ())
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.ASSUMED)
        self.assertTrue(any(i.code == "validation.duplicate_field" for i in checked.issues))

    def test_contract_rejects_nan_and_extra_fields(self):
        data = editable_reading(self.result())
        data["extra"] = True
        with self.assertRaises(ERPError):
            decode_reading(dumps(data), "fake")
        data = editable_reading(self.result("NaN"))
        with self.assertRaises(ERPError):
            decode_reading(dumps(data), "fake")

    def test_periods_have_evidence(self):
        reading = self.result()
        facts = reading.problem.facts + tuple(
            Fact(key, "initial", "company", "公司", key, value, "integer", "", KnowledgeState.KNOWN, (eid,))
            for key, value, eid in [("years", "4", "years"), ("periods_per_year", "4", "periods")])
        reading = replace(reading, problem=replace(reading.problem, facts=facts))
        checked = validate_reading(reading, (Evidence("p1", "doc", "格", "100000"),
                                           Evidence("years", "doc", "格", "4年"),
                                           Evidence("periods", "doc", "格", "每年4季")))
        self.assertEqual((checked.problem.years, checked.problem.periods_per_year), (4, 4))

    def test_chinese_counts_match_whole_numbers(self):
        cases = [("经营四年", "4", True), ("经营十四年", "14", True),
                 ("经营十四年", "4", False), ("二十四个月", "24", True),
                 ("一百零四年", "4", False), ("经营四年", "0", False)]
        for text, value, supported in cases:
            with self.subTest(text=text, value=value):
                checked = validate_reading(self.result(value, kind="integer"),
                                           (Evidence("p1", "doc", "段落", text),))
                self.assertEqual(checked.problem.facts[0].state == KnowledgeState.KNOWN,
                                 supported)

    def test_reader_repairs_once_with_same_model_and_materials(self):
        document = OOXMLDocumentParser().parse(
            SourceDocument("d", "topic.docx", "hash", 0, "docx"), docx_bytes(),
        )
        valid = editable_reading(self.result(refs=(document.evidence[0].id,)))
        replacement = FakeLLM("must not be called")
        calls = []

        class RepairingClient:
            configured = True

            def generate(self, request):
                calls.append(request)
                if len(calls) == 1:
                    runtime.replace(replacement)
                    return LLMResponse("invalid json", "original-model")
                return LLMResponse(dumps(valid), "original-model")

        runtime = RuntimeLLMClient(RepairingClient())
        control = Control()
        result = StructuredReader(runtime, runtime.current).read((document,), control)
        self.assertEqual(result.model, "original-model")
        self.assertEqual(result.prompt_version, "reader-2")
        self.assertEqual(len(calls), 2)
        self.assertEqual(replacement.calls, [])
        self.assertIn("repairing_reading", control.stages)
        self.assertEqual(loads(calls[0].user)["documents"], loads(calls[1].user)["documents"])

    def test_reader_stops_after_failed_repair(self):
        document = OOXMLDocumentParser().parse(
            SourceDocument("d", "topic.docx", "hash", 0, "docx"), docx_bytes(),
        )
        llm = FakeLLM("invalid json")
        with self.assertRaises(ERPError):
            StructuredReader(llm).read((document,), Control())
        self.assertEqual(len(llm.calls), 2)

    def test_document_capability_warnings_survive_reading(self):
        document = OOXMLDocumentParser().parse(
            SourceDocument("d", "topic.docx", "hash", 0, "docx"),
            docx_bytes(extras={"word/media/image1.png": b"image"}),
        )
        llm = FakeLLM(dumps(editable_reading(self.result(refs=(document.evidence[0].id,)))))
        result = StructuredReader(llm).read((document,), Control())
        warnings = [issue for issue in result.issues if issue.code == "reading.source_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("OCR", warnings[0].message)
