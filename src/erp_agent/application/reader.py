from dataclasses import replace
from typing import Callable

from erp_agent.application.ports import LLMClient, RunControl
from erp_agent.application.reading.batches import DocumentBatcher, ReadingBatch
from erp_agent.application.reading.coverage import missing_table_rows
from erp_agent.application.reading.merge import ReadingMerger
from erp_agent.application.reading.review import LinkReviewRole
from erp_agent.application.reading.roles import ExtractionRole, ReaderLimits, ReadingPart
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Issue, ParsedDocument, ReadingResult
from erp_agent.domain.validation import validate_reading


SPLIT_ERRORS = {"reading_batch_limit", "llm_input_limit", "llm_incomplete"}


class StructuredReader:
    def __init__(self, llm: LLMClient,
                 client_factory: Callable[[], LLMClient] | None = None,
                 limits: ReaderLimits = ReaderLimits(),
                 batcher: DocumentBatcher | None = None,
                 facts_role: ExtractionRole | None = None,
                 rules_role: ExtractionRole | None = None,
                 merger: ReadingMerger | None = None,
                 reviewer: LinkReviewRole | None = None) -> None:
        self.llm = llm
        self.client_factory = client_factory
        self.limits = limits
        self.batcher = batcher or DocumentBatcher()
        self.facts_role = facts_role or ExtractionRole("facts")
        self.rules_role = rules_role or ExtractionRole("rules")
        self.merger = merger or ReadingMerger()
        self.reviewer = reviewer or LinkReviewRole()

    def read(self, documents: tuple[ParsedDocument, ...], control: RunControl,
             client: LLMClient | None = None) -> ReadingResult:
        client = client or (self.client_factory() if self.client_factory else self.llm)
        control.checkpoint("reading_structure")
        max_chars = min(self.limits.max_request_chars,
                        getattr(client, "max_input_chars", self.limits.max_request_chars))
        overhead = max(len(self.facts_role.system), len(self.rules_role.system)) + 2000
        if max_chars <= overhead:
            raise ERPError("reading_input_limit", "当前输入上限不足以容纳读题提示词和来源结构，请提高模型预设输入上限。")
        batches = self.batcher.batches(documents, max_chars - overhead,
                                       self.limits.max_batch_units)
        if not batches:
            raise ERPError("reading_empty", "未发现可读取的题目文字或表格。")
        parts: list[ReadingPart] = []

        def run_role(role: ExtractionRole, batch: ReadingBatch,
                     parameters: ReadingPart | None = None) -> tuple[ReadingPart, ...]:
            try:
                result = role.extract(batch, client, control, max_chars, len(parts),
                                      parameters.result.problem.facts if parameters else ())
                parts.append(result)
                return (result,)
            except Cancelled:
                raise
            except ERPError as error:
                if error.code in SPLIT_ERRORS and len(batch.units) > 1:
                    return tuple(part for smaller in batch.split()
                                 for part in run_role(role, smaller))
                name = "参数提取" if role.name == "facts" else "规则整理"
                raise ERPError(error.code,
                               f"{batch.document.source.filename} · {name}：{error.message}", error.status) from error

        for batch in batches:
            parameter_parts = run_role(self.facts_role, batch)
            for parameters in parameter_parts:
                run_role(self.rules_role, parameters.batch, parameters)
        control.checkpoint("reading_linking", len(parts))
        reading = self.merger.merge(tuple(parts))
        evidence = tuple(e for doc in documents for e in doc.evidence)
        if len({(part.batch.document.source.id, part.batch.focus_ids) for part in parts}) > 1:
            findings = self.reviewer.review(reading, evidence, client, control, max_chars, len(parts))
            reading = replace(reading, issues=tuple(dict.fromkeys(reading.issues + findings)))
        source_issues = tuple(Issue("reading.source_warning", "warning", doc.source.id, warning)
                              for doc in documents for warning in doc.warnings)
        reading = replace(reading, issues=reading.issues + source_issues
                          + missing_table_rows(reading, documents))
        control.checkpoint("validating", len(parts))
        return validate_reading(reading, evidence)
