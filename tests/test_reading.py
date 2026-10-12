import unittest
from dataclasses import replace

from erp_agent.application.contracts import decode_reading, dumps, editable_reading, loads
from erp_agent.application.reader import StructuredReader
from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.adapters.model_settings import RuntimeLLMClient
from erp_agent.application.ports import LLMResponse
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Evidence, Fact, Issue, KnowledgeState, ProblemSpec, ReadingResult, RuleSet, SourceDocument,
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

    def test_text_layout_whitespace_is_supported_without_changing_meaning(self):
        for value, source, supported in [
            ("每季付息，到期还本", "每季付息，到期还 本", True),
            ("本息同还", "每季付息，到期还 本", False),
            ("AB", "A B", False),
        ]:
            with self.subTest(value=value, source=source):
                checked = validate_reading(self.result(value, kind="text"),
                                           (Evidence("p1", "doc", "格", source),))
                self.assertEqual(checked.problem.facts[0].state == KnowledgeState.KNOWN,
                                 supported)
                if not supported:
                    message = next(i.message for i in checked.issues
                                   if i.code == "validation.unsupported_value")
                    self.assertIn("文字", message)
                    self.assertNotIn("单位", message)

    def test_unspecified_unit_is_empty_and_does_not_change_value_or_conversion(self):
        for unit in ("", "无", "不清楚", "unknown"):
            reading = self.result("50")
            reading = replace(reading, problem=replace(
                reading.problem, facts=(replace(reading.problem.facts[0], unit=unit),)))
            checked = validate_reading(reading, (Evidence("p1", "doc", "格", "50"),))
            fact = checked.problem.facts[0]
            self.assertEqual((fact.value, fact.unit, fact.state), ("50", "", KnowledgeState.KNOWN))
        checked = validate_reading(self.result("0.5", "percent_to_ratio"),
                                   (Evidence("p1", "doc", "格", "50"),))
        self.assertEqual(checked.problem.facts[0].state, KnowledgeState.CONFLICTED)

    def test_only_missing_unit_warning_uses_default_and_keeps_real_conflict(self):
        reading = replace(self.result("50"), issues=(
            Issue("ambiguous_unit", "warning", "cash", "数值单位未注明，需补充单位。"),
            Issue("ambiguous_unit", "error", "cash", "单位未明确，元与万元口径冲突。"),
        ))
        checked = validate_reading(reading, (Evidence("p1", "doc", "格", "50"),))
        fact = checked.problem.facts[0]
        self.assertEqual((fact.value, fact.unit, fact.state), ("50", "", KnowledgeState.KNOWN))
        self.assertTrue(any(i.code == "review.resolved.ambiguous_unit" and i.severity == "info"
                            for i in checked.issues))
        self.assertTrue(any(i.code == "ambiguous_unit" and i.severity == "error"
                            for i in checked.issues))

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

    def test_horizon_defaults_fill_absent_and_missing_fields_once(self):
        reading = self.result()
        missing = Fact("years", "initial", "company", "公司", "years", None, "integer",
                       "", KnowledgeState.MISSING, ())
        for facts in (reading.problem.facts, reading.problem.facts + (missing,)):
            with self.subTest(facts=facts):
                checked = validate_reading(replace(reading, problem=replace(
                    reading.problem, facts=facts)), (Evidence("p1", "doc", "格", "100000"),))
                self.assertEqual((checked.problem.years, checked.problem.periods_per_year), (4, 4))
                periods = [f for f in checked.problem.facts if f.key in {"years", "periods_per_year"}]
                self.assertTrue(all(f.state == KnowledgeState.ASSUMED and not f.evidence_ids
                                    and "系统默认" in f.explanation for f in periods))
                self.assertEqual(validate_reading(checked, (Evidence("p1", "doc", "格", "100000"),)),
                                 checked)

    def test_horizon_defaults_preserve_explicit_values_and_conflicts(self):
        reading = self.result()
        year = Fact("years", "initial", "company", "公司", "years", "3", "integer",
                    "年", KnowledgeState.KNOWN, ("period",))
        evidence = (Evidence("p1", "doc", "格", "100000"), Evidence("period", "doc", "段落", "经营3年"))
        for state, value, expected in ((KnowledgeState.KNOWN, "3", 3),
                                       (KnowledgeState.ASSUMED, "2", 2),
                                       (KnowledgeState.CONFLICTED, "3", None),
                                       (KnowledgeState.CONFLICTED, None, None),
                                       (KnowledgeState.KNOWN, "4", None)):
            with self.subTest(state=state, value=value):
                facts = reading.problem.facts + (replace(year, state=state, value=value),)
                checked = validate_reading(replace(reading, problem=replace(reading.problem, facts=facts),
                    issues=(Issue("horizon_conflict", "error", "years", "经营周期未提供一致值，存在冲突。"),)), evidence)
                self.assertEqual(checked.problem.years, expected)
                self.assertEqual(checked.problem.periods_per_year, 4)
                self.assertTrue(any(i.code == "horizon_conflict" and i.severity == "error"
                                    for i in checked.issues))
                rechecked = validate_reading(checked, evidence)
                self.assertEqual(rechecked.problem.years, expected)

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
        self.assertEqual(result.prompt_version, "reader-7")
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

    def test_langchain_schema_error_repairs_once_without_retrying_transport_errors(self):
        document = OOXMLDocumentParser().parse(
            SourceDocument("d", "topic.docx", "hash", 0, "docx"), docx_bytes(),
        )
        valid = editable_reading(self.result(refs=(document.evidence[0].id,)))
        calls = []

        class Client:
            configured = True

            def generate(self, request):
                calls.append(request)
                if len(calls) == 1:
                    raise ERPError("llm_structured_response", "结构不符合 schema", 502)
                return LLMResponse(dumps(valid), "original-model")

        result = StructuredReader(Client()).read((document,), Control())
        self.assertEqual(result.model, "original-model")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(loads(calls[1].user)["previous_output"])
        for code in ("llm_incomplete", "llm_http_error", "llm_timeout"):
            calls.clear()

            class FailingClient:
                configured = True

                def generate(self, request):
                    calls.append(request)
                    raise ERPError(code, "调用失败", 502)

            with self.subTest(code=code), self.assertRaises(ERPError):
                StructuredReader(FailingClient()).read((document,), Control())
            self.assertEqual(len(calls), 1)

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

    def test_assumed_horizon_is_usable_and_preserves_status(self):
        reading = self.result()
        facts = reading.problem.facts + tuple(
            Fact(key, 'initial', 'company', '公司', key, value, 'integer', '',
                 KnowledgeState.ASSUMED, (), explanation='暂定经营周期')
            for key, value in [('years', '4'), ('periods_per_year', '4')])
        result = validate_reading(replace(reading, problem=replace(reading.problem, facts=facts)),
                                  (Evidence('p1', 'doc', '格', '100000'),))
        self.assertEqual((result.problem.years, result.problem.periods_per_year), (4, 4))
        self.assertEqual(result.problem.facts[1].state, KnowledgeState.ASSUMED)
        self.assertFalse(any(i.code == 'validation.horizon_missing' for i in result.issues))
        self.assertTrue(any(i.code == 'validation.uncertain' and i.severity == 'info'
                            for i in result.issues))

    def test_reader_unwraps_whole_json_fence_without_extra_model_call(self):
        document = OOXMLDocumentParser().parse(
            SourceDocument('d', 'topic.docx', 'hash', 0, 'docx'), docx_bytes(),
        )
        content = '```json\n' + dumps(editable_reading(
            self.result(refs=(document.evidence[0].id,)))) + '\n```'
        llm = FakeLLM(content)
        result = StructuredReader(llm).read((document,), Control())
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result.raw_response, content)
        self.assertEqual(result.problem.facts[0].state, KnowledgeState.KNOWN)
