import hashlib
import re
import threading
from collections.abc import Callable
from dataclasses import replace

from langchain_core.runnables import RunnableLambda

from erp_agent.application.contracts import decode_reading, dumps, editable_reading
from erp_agent.application.memory import RoleMemory
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.reading.batches import DocumentBatcher, ReadingBatch
from erp_agent.application.reading.coverage import missing_table_rows
from erp_agent.application.reading.merge import ReadingMerger
from erp_agent.application.reading.review import LinkReviewRole
from erp_agent.application.reading.roles import ExtractionRole, ReaderLimits, ReadingPart
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Issue, ParsedDocument, ProblemSpec, ReadingResult, RuleSet
from erp_agent.domain.validation import source_notice, validate_reading

SPLIT_ERRORS = {
    "reading_batch_limit",
    "llm_input_limit",
    "llm_incomplete",
    "llm_gateway_timeout",
    "llm_unavailable",
    "llm_timeout",
    "invalid_contract",
    "invalid_json",
    "reading_batch_source",
    "llm_structured_response",
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
        self.combined_role = (
            ExtractionRole("reading") if facts_role is None and rules_role is None else None
        )
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
        ) - getattr(client, "request_overhead_chars", 0)
        roles = (self.combined_role,) if self.combined_role else (self.facts_role, self.rules_role)
        overhead = max(len(role.system) + len(role.schema) for role in roles) + 2200
        if max_chars <= overhead:
            raise ERPError(
                "reading_input_limit",
                "当前输入上限不足以容纳读题提示词和来源结构，请提高模型预设输入上限。",
            )
        if self.combined_role:

            def fit(batch: ReadingBatch) -> tuple[ReadingBatch, ...]:
                try:
                    self.combined_role.request(batch, max_chars, memory=memory)
                    return (batch,)
                except ERPError as error:
                    if error.code != "reading_batch_limit":
                        raise
                    if len(batch.units) > 1:
                        return tuple(part for smaller in batch.split() for part in fit(smaller))
                    if batch.units[0].kind != "paragraph":
                        raise
                    smaller = self.batcher.batches(
                        (replace(batch.document, blocks=batch.units),),
                        max_chars - overhead,
                        self.limits.max_batch_units,
                    )
                    if len(smaller) <= 1:
                        raise
                    return tuple(
                        part
                        for item in smaller
                        for part in fit(
                            replace(
                                item,
                                document=batch.document,
                                notes=batch.notes,
                                label=batch.label,
                            )
                        )
                    )

            batches = tuple(part for batch in self.batcher.tasks(documents) for part in fit(batch))
        else:
            batches = self.batcher.batches(
                documents, max_chars - overhead, self.limits.max_batch_units
            )
        if not batches:
            raise ERPError("reading_empty", "未发现可读取的题目文字或表格。")
        parts: list[ReadingPart] = []
        lock = threading.Lock()
        completed = 0
        stopped = threading.Event()

        class BatchControl:
            def check_cancelled(self) -> None:
                if stopped.is_set():
                    raise Cancelled()
                check = getattr(control, "check_cancelled", None)
                if check:
                    check()
                else:
                    self.checkpoint("waiting_model")

            def report_task(self, task_id, changes) -> None:
                report = getattr(control, "report_task", None)
                if report:
                    report(task_id, changes)

            def checkpoint(self, stage: str, completed: int = 0) -> None:
                with lock:
                    if stopped.is_set():
                        raise Cancelled()
                    control.checkpoint(stage, progress())

        def progress() -> int:
            return completed

        batch_control = BatchControl()

        def task_for(role: ExtractionRole, batch: ReadingBatch) -> TaskContext:
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
            blocks = ", ".join(str(i + 1) for i in sorted({u.index for u in batch.units}))
            rows = [c.row + 1 for u in batch.units for c in u.cells]
            object_name = f"{batch.document.source.filename} · {batch.label or '正文块 ' + blocks}"
            if rows:
                object_name += f" · 行 {min(rows)}–{max(rows)}"
            return TaskContext(
                key,
                "Reader",
                "整理参数与规则"
                if role.name == "reading"
                else "参数提取"
                if role.name == "facts"
                else "规则整理",
                object_name,
                "reader",
            )

        def run_role(
            role: ExtractionRole, batch: ReadingBatch, parameters: ReadingPart | None = None
        ) -> tuple[ReadingPart, ...]:
            nonlocal completed
            task = task_for(role, batch)
            key = task.id
            object_name = task.object

            class RoleControl:
                def checkpoint(self, stage, completed=0):
                    batch_control.checkpoint(stage, completed)
                    report_task(control, task, "running", stage=stage)

                def check_cancelled(self):
                    batch_control.check_cancelled()

                def report_task(self, task_id, changes):
                    batch_control.report_task(task_id, changes)

            role_control = RoleControl()
            role_client = TaskLLMClient(client, role_control, task)
            report_task(control, task, "queued", evidence_count=len(batch.focus_ids),
                        task_kind="extraction")
            batch_control.checkpoint("reading_" + role.name)
            cached = memory.get(key) if memory else None
            if cached and cached.get("split") is True and len(batch.units) > 1:
                report_task(control, task, "split")
                return tuple(part for smaller in batch.split() for part in run_role(role, smaller))
            if cached:
                result = ReadingPart(
                    role.name, batch, decode_reading(dumps(cached["reading"]), str(cached["model"]))
                )
            else:
                try:
                    result = role.extract(
                        batch,
                        role_client,
                        role_control,
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
                        report_task(control, task, "split", error=error.message)
                        if memory:
                            memory.put(key, {"split": True})
                        return tuple(
                            part for smaller in batch.split() for part in run_role(role, smaller)
                        )
                    if error.code in {"llm_gateway_timeout", "llm_unavailable", "llm_timeout"}:
                        batch_control.checkpoint("reading_retry")
                        try:
                            result = role.extract(
                                batch,
                                role_client,
                                role_control,
                                max_chars,
                                completed,
                                memory=memory,
                            )
                        except Cancelled:
                            raise
                        except ERPError as failure:
                            name = (
                                "整理参数与规则"
                                if role.name == "reading"
                                else "参数提取"
                                if role.name == "facts"
                                else "规则整理"
                            )
                            raise ERPError(
                                failure.code,
                                f"{batch.document.source.filename} · {name}：{failure.message}",
                                failure.status,
                            ) from failure
                    elif error.code in {
                        "invalid_contract",
                        "invalid_json",
                        "reading_batch_source",
                        "llm_structured_response",
                    }:
                        issue = Issue(
                            "reading.extraction_unresolved",
                            "error",
                            "problem",
                            f"{object_name}：该片段经重试仍无法可靠整理（{error.message}）。"
                            "请查看原文并说明相关参数、规则及适用对象；其余已读内容已保留。",
                            batch.focus_ids,
                        )
                        result = ReadingPart(
                            role.name,
                            batch,
                            ReadingResult(
                                ProblemSpec(batch.document.source.filename, ()),
                                RuleSet(()),
                                (issue,),
                                "未完成",
                            ),
                        )
                    else:
                        report_task(control, task, "failed", error=error.message)
                        name = (
                            "整理参数与规则"
                            if role.name == "reading"
                            else "参数提取"
                            if role.name == "facts"
                            else "规则整理"
                        )
                        raise ERPError(
                            error.code,
                            f"{batch.document.source.filename} · {name}：{error.message}",
                            error.status,
                        ) from error
                batch_control.checkpoint("reading_" + role.name)
                if memory and not any(
                    i.code in {"reading.invalid_number", "reading.extraction_unresolved"}
                    for i in result.result.issues
                ):
                    memory.put(
                        key,
                        {"reading": editable_reading(result.result), "model": result.result.model},
                    )
            report_task(
                control,
                task,
                "needs_input"
                if any(i.severity == "error" for i in result.result.issues)
                else "cached"
                if cached
                else "succeeded",
                model=result.result.model,
                fact_count=len(result.result.problem.facts),
                rule_count=len(result.result.rules.rules),
            )
            with lock:
                completed += 1
            return (result,)

        def read_batch(batch: ReadingBatch) -> tuple[ReadingPart, ...]:
            try:
                if self.combined_role:
                    return run_role(self.combined_role, batch)
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

        parent = TaskContext(
            "reader",
            "Reader",
            "读题统筹",
            f"{len(documents)} 份资料 · {len(batches)} 个章节/表格任务",
        )
        report_task(control, parent, "running", batch_count=len(batches))
        for batch in batches:
            role = self.combined_role or self.facts_role
            report_task(control, task_for(role, batch), "queued",
                        evidence_count=len(batch.focus_ids), task_kind="extraction")
        plan_reading = getattr(client, "plan_reading", None)
        if self.combined_role and plan_reading:
            requests: list[LLMRequest] = []

            def prepare(batch: ReadingBatch) -> None:
                task = task_for(self.combined_role, batch)
                cached = memory.get(task.id) if memory else None
                if cached and cached.get("split") is True and len(batch.units) > 1:
                    for smaller in batch.split():
                        prepare(smaller)
                elif not cached:
                    requests.append(
                        replace(
                            self.combined_role.request(batch, max_chars, memory=memory),
                            task=task,
                        )
                    )

            for batch in batches:
                prepare(batch)
            plan_reading(tuple(requests), batch_control, self.limits.concurrency)
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
            review_groups = {
                eid: str(index) for index, part in enumerate(parts) for eid in part.batch.focus_ids
            }
            findings = self.reviewer.review(
                reading,
                evidence,
                client,
                control,
                max_chars,
                len(parts),
                review_groups=review_groups if self.combined_role else None,
            )
            reading = replace(reading, issues=tuple(dict.fromkeys(reading.issues + findings)))
        source_issues = tuple(
            source_notice(doc.source.id, warning)
            for doc in documents
            for warning in doc.warnings
        )
        reading = replace(
            reading, issues=reading.issues + source_issues + missing_table_rows(reading, documents)
        )
        control.checkpoint("validating", len(parts))
        result = validate_reading(reading, evidence)
        report_task(
            control,
            parent,
            "needs_input" if any(i.severity == "error" for i in result.issues) else "succeeded",
            fact_count=len(result.problem.facts),
            rule_count=len(result.rules.rules),
        )
        return result
