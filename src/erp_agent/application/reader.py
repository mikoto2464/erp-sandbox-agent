import hashlib
import re
import threading
from collections.abc import Callable
from dataclasses import replace

from langchain_core.runnables import RunnableLambda

from erp_agent.application.contracts import decode_reading, dumps, editable_reading
from erp_agent.application.memory import RoleMemory
from erp_agent.application.ports import LLMClient, RunControl
from erp_agent.application.reading.batches import DocumentBatcher, ReadingBatch
from erp_agent.application.reading.coverage import missing_table_rows
from erp_agent.application.reading.merge import ReadingMerger
from erp_agent.application.reading.review import LinkReviewRole
from erp_agent.application.reading.roles import ExtractionRole, ReaderLimits, ReadingPart
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Issue, ParsedDocument, ReadingResult
from erp_agent.domain.validation import validate_reading

SPLIT_ERRORS = {
    "reading_batch_limit",
    "llm_input_limit",
    "llm_incomplete",
    "llm_gateway_timeout",
    "llm_unavailable",
    "llm_timeout",
}


class StructuredReader:
    def __init__(
        self,
        llm: LLMClient,
        client_factory: Callable[[], LLMClient] | None = None,
        limits: ReaderLimits | None = None,
        batcher: DocumentBatcher | None = None,
        facts_role: ExtractionRole | None = None,
        rules_role: ExtractionRole | None = None,
        merger: ReadingMerger | None = None,
        reviewer: LinkReviewRole | None = None,
    ) -> None:
        self.llm = llm
        self.client_factory = client_factory
        self.limits = limits or ReaderLimits()
        self.batcher = batcher or DocumentBatcher()
        self.facts_role = facts_role or ExtractionRole("facts")
        self.rules_role = rules_role or ExtractionRole("rules")
        self.merger = merger or ReadingMerger()
        self.reviewer = reviewer or LinkReviewRole()

    def read(
        self,
        documents: tuple[ParsedDocument, ...],
        control: RunControl,
        client: LLMClient | None = None,
        memory: RoleMemory | None = None,
    ) -> ReadingResult:
        client = client or (self.client_factory() if self.client_factory else self.llm)
        control.checkpoint("reading_structure")
        max_chars = min(
            self.limits.max_request_chars,
            getattr(client, "max_input_chars", self.limits.max_request_chars),
        )
        overhead = (
            max(len(self.facts_role.system), len(self.rules_role.system))
            + max(len(self.facts_role.schema), len(self.rules_role.schema))
            + 2200
        )
        if max_chars <= overhead:
            raise ERPError(
                "reading_input_limit",
                "当前输入上限不足以容纳读题提示词和来源结构，请提高模型预设输入上限。",
            )
        batches = self.batcher.batches(documents, max_chars - overhead, self.limits.max_batch_units)
        if not batches:
            raise ERPError("reading_empty", "未发现可读取的题目文字或表格。")
        parts: list[ReadingPart] = []
        lock = threading.Lock()
        completed = 0
        stopped = threading.Event()

        class BatchControl:
            def checkpoint(self, stage: str, completed: int = 0) -> None:
                with lock:
                    if stopped.is_set():
                        raise Cancelled()
                    control.checkpoint(stage, progress())

        def progress() -> int:
            return completed

        batch_control = BatchControl()

        def run_role(
            role: ExtractionRole, batch: ReadingBatch, parameters: ReadingPart | None = None
        ) -> tuple[ReadingPart, ...]:
            nonlocal completed
            key = hashlib.sha256(
                dumps(
                    {
                        "role": role.name,
                        "schema": role.schema,
                        "prompt": role.system,
                        "batch": batch.payload(),
                        "focus": batch.focus_ids,
                    }
                ).encode()
            ).hexdigest()
            batch_control.checkpoint("reading_" + role.name)
            cached = memory.get(key) if memory else None
            if cached and cached.get("split") is True and len(batch.units) > 1:
                return tuple(part for smaller in batch.split() for part in run_role(role, smaller))
            if cached:
                result = ReadingPart(
                    role.name, batch, decode_reading(dumps(cached["reading"]), str(cached["model"]))
                )
            else:
                try:
                    result = role.extract(
                        batch,
                        client,
                        batch_control,
                        max_chars,
                        completed,
                        parameters.result.problem.facts if parameters else (),
                        memory,
                    )
                except Cancelled:
                    raise
                except ERPError as error:
                    if error.code in SPLIT_ERRORS and len(batch.units) > 1:
                        batch_control.checkpoint("reading_smaller_batch")
                        if memory:
                            memory.put(key, {"split": True})
                        return tuple(
                            part for smaller in batch.split() for part in run_role(role, smaller)
                        )
                    if error.code in {"llm_gateway_timeout", "llm_unavailable", "llm_timeout"}:
                        batch_control.checkpoint("reading_retry")
                        try:
                            result = role.extract(
                                batch, client, batch_control, max_chars, completed, memory=memory
                            )
                        except Cancelled:
                            raise
                        except ERPError as failure:
                            name = "参数提取" if role.name == "facts" else "规则整理"
                            raise ERPError(
                                failure.code,
                                f"{batch.document.source.filename} · {name}：{failure.message}",
                                failure.status,
                            ) from failure
                    else:
                        name = "参数提取" if role.name == "facts" else "规则整理"
                        raise ERPError(
                            error.code,
                            f"{batch.document.source.filename} · {name}：{error.message}",
                            error.status,
                        ) from error
                batch_control.checkpoint("reading_" + role.name)
                if memory:
                    memory.put(
                        key,
                        {"reading": editable_reading(result.result), "model": result.result.model},
                    )
            with lock:
                completed += 1
            return (result,)

        def read_batch(batch: ReadingBatch) -> tuple[ReadingPart, ...]:
            try:
                parameters = run_role(self.facts_role, batch)
                rules = tuple(
                    part
                    for parameter in parameters
                    if not pure_order_rows(parameter)
                    for part in run_role(self.rules_role, parameter.batch, parameter)
                )
                return parameters + rules
            except ERPError:
                stopped.set()
                raise

        def pure_order_rows(parameter: ReadingPart) -> bool:
            facts = parameter.result.problem.facts
            if not facts or any(f.category != "order_pool" for f in facts):
                return False
            if any(unit.kind != "table" for unit in parameter.batch.units):
                return False
            pattern = r"必须|不得|不能|不超过|至少|最多|公式|规则|按比例|扣分|罚|违约|优先|限制"
            if any(
                re.search(pattern, cell.text)
                for unit in parameter.batch.units
                for cell in unit.cells
            ):
                return False
            cited = {eid for fact in facts for eid in fact.evidence_ids}
            return all(
                cell.evidence_id in cited
                for unit in parameter.batch.units
                for cell in unit.cells
                if cell.text.strip()
            )

        runner = RunnableLambda(read_batch)
        results = runner.batch(
            list(batches),
            config={"max_concurrency": self.limits.concurrency},
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise next(
                (error for error in failures if not isinstance(error, Cancelled)), failures[0]
            )
        for batch_parts in results:
            if not isinstance(batch_parts, Exception):
                parts.extend(batch_parts)
        control.checkpoint("reading_linking", len(parts))
        reading = self.merger.merge(tuple(parts))
        evidence = tuple(e for doc in documents for e in doc.evidence)
        if len({(part.batch.document.source.id, part.batch.focus_ids) for part in parts}) > 1:
            findings = self.reviewer.review(
                reading, evidence, client, control, max_chars, len(parts)
            )
            reading = replace(reading, issues=tuple(dict.fromkeys(reading.issues + findings)))
        source_issues = tuple(
            Issue("reading.source_warning", "warning", doc.source.id, warning)
            for doc in documents
            for warning in doc.warnings
        )
        reading = replace(
            reading, issues=reading.issues + source_issues + missing_table_rows(reading, documents)
        )
        control.checkpoint("validating", len(parts))
        return validate_reading(reading, evidence)
