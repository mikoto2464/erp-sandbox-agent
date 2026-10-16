import base64
import hashlib
import threading
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from erp_agent.application.answerer import AnswererService
from erp_agent.application.codec import (
    budget_template,
    generation_config,
    parsed_document,
    problem_snapshot,
    reading_inputs,
    solution_bundle,
    template_reading,
)
from erp_agent.application.contracts import (
    JSONObject,
    JSONValue,
    array,
    decode_plan,
    decode_reading,
    dumps,
    integer,
    obj,
    string,
    strings,
    to_json,
)
from erp_agent.application.input_sources import input_sources
from erp_agent.application.jobs import ApplicationJobs
from erp_agent.application.memory import MemoryService
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import (
    BudgetTemplateParser,
    DocumentParser,
    JobRunner,
    ModelSettings,
    Repository,
    RunControl,
    TaskContext,
)
from erp_agent.application.reader import StructuredReader
from erp_agent.application.reading.incremental import merge_incremental
from erp_agent.application.solution_stages import (
    plan_hash,
    public_solution,
    stage_record,
)
from erp_agent.application.solver import (
    LLMStrategyGenerator,
    SolverService,
)
from erp_agent.application.solver_budget import PlanBudgetPlanner
from erp_agent.application.solver_context import planning_requirements, selected_snapshot
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.application.template_library import TemplateLibrary
from erp_agent.application.template_reader import LIBRARY_SCOPE
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import (
    KnowledgeState,
    ParsedDocument,
    PlanningContext,
    ProblemSnapshot,
    ProblemSpec,
    ReadingInputs,
    ReadingResult,
    RuleSet,
    SolveRequest,
    SolverMode,
    SourceDocument,
)
from erp_agent.domain.validation import validate_reading


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class WorkflowService:
    def __init__(
        self,
        repository: Repository,
        jobs: JobRunner,
        parser: DocumentParser,
        reader: StructuredReader,
        solver: SolverService,
        default_solver: SolverMode = SolverMode.LLM_ONLY,
        model_settings: ModelSettings | None = None,
        template_parser: BudgetTemplateParser | None = None,
        answerer: AnswererService | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.repository, self.jobs, self.parser = repository, jobs, parser
        self.reader, self.solver = reader, solver
        self.default_solver = default_solver
        self.model_settings = model_settings
        self.template_parser = template_parser
        self._answerer = answerer
        self.memory = memory
        self._read_lock = threading.Lock()
        self._input_lock = threading.Lock()
        self._solve_lock = threading.Lock()
        self._budget_lock = threading.Lock()
        self._template_lock = threading.RLock()
        self._legacy_templates_migrated = False
        self.template_library = TemplateLibrary(
            repository, jobs, template_parser, reader.llm, model_settings
        )
        self.application_tasks = ApplicationJobs(repository, jobs)

    def application_jobs(self) -> list[JSONObject]:
        return self.application_tasks.list()

    def application_job(self, job_id: str) -> JSONObject:
        return self.application_tasks.get(job_id)

    def cancel_application_job(self, job_id: str) -> JSONObject:
        return self.application_tasks.cancel(job_id)

    def answer_application_job(self, job_id: str, data: JSONObject) -> JSONObject:
        with self._solve_lock:
            job = self.application_tasks.get(job_id)
            if job.get("kind") != "solve" or not job.get("intervention"):
                raise ERPError("resolution_stale", "此任务当前没有待处理的方案问题。", 409)
            project_id = string(job["project_id"])
            if data.get("option") != "cancel":
                snapshot = self.problem(project_id, integer(obj(job["metadata"])["version"]))
                if (
                    snapshot.version != self.repository.versions(project_id)[0]
                    or snapshot.inputs.revision != self.inputs(project_id).revision
                ):
                    raise ERPError(
                        "resolution_context_changed",
                        "题目已变化，请取消此任务并按当前题目重新生成。",
                        409,
                    )
            result = self.jobs.answer(project_id, job_id, data)
            if data.get("option") == "restart":
                return self.retry_application_job(job_id)
            return result

    def retry_application_job(self, job_id: str) -> JSONObject:
        job = self.application_tasks.get(job_id)
        if job["status"] not in {"failed", "cancelled"}:
            raise ERPError("job_not_retryable", "只有失败或已取消的任务可以重试。", 409)
        metadata = obj(job["metadata"])
        if metadata.get("upload_deleted"):
            raise ERPError("upload_deleted", "文件已删除，不能重试此任务。", 409)
        project_id = string(job["project_id"])
        if project_id == LIBRARY_SCOPE:
            return self.template_library.read(string(metadata["template_id"]))
        if job["kind"] == "parse":
            return self.retry_parse(project_id, string(metadata["document_id"]))
        if job["kind"] == "template":
            raise ERPError("template_library_required", "旧空表任务请在空表库继续处理。", 409)
        if job["kind"] == "read":
            return self.read(
                project_id,
                {
                    "document_ids": metadata.get("document_ids", []),
                    "append": metadata.get("append", False),
                },
            )
        if job["kind"] == "solve":
            return self.solve(
                project_id,
                {
                    "version": metadata["version"],
                    "generation": metadata.get("generation", {}),
                },
            )
        if job["kind"] == "budget":
            return self.start_budget(project_id, string(metadata["solution_id"]))
        if job["kind"] == "mapping":
            return self.answerer.analyze(
                project_id,
                {
                    "solution_id": metadata["solution_id"],
                    "template_id": metadata["template_id"],
                },
            )
        if job["kind"] == "artifact":
            return self.answerer.export(
                project_id,
                {
                    "solution_id": metadata["solution_id"],
                    "kind": metadata["kind"],
                    "mapping_id": metadata.get("mapping_id"),
                },
            )
        raise ERPError("job_not_retryable", "此类任务不支持重试。", 409)

    def import_library_template(self, project_id: str, data: JSONObject) -> JSONObject:
        with self._template_lock, self._read_lock, self._solve_lock:
            self.project(project_id)
            tid = string(data.get("template_id"), 100)
            detail = self.template_library.detail(tid)
            template = budget_template(obj(detail["template"]))
            reading = template_reading(obj(detail["reading"])) if detail["reading"] else None
            if not reading or not reading.reviewed or reading.sha256 != template.source.sha256:
                raise ERPError("template_not_reviewed", "请先在空表库完成解读与确认。", 409)
            if any(
                obj(j).get("status") in {"running", "queued"}
                and obj(j).get("kind") in {"read", "solve"}
                for j in self.repository.list("job", project_id)
            ):
                raise ERPError("project_busy", "请等待当前读题或方案任务完成后导入空表。", 409)
            matches = [
                budget_template(obj(t)) for t in self.repository.list("template", project_id)
            ]
            existing = next((t for t in matches if t.source.sha256 == template.source.sha256), None)
            source = existing.source if existing else replace(template.source, id=uuid4().hex)
            original = self.repository.get("template_original", LIBRARY_SCOPE, tid)
            self.repository.put(
                "template_original", project_id, source.id, {**original, "source": to_json(source)}
            )
            self.repository.put("template", project_id, source.id, replace(template, source=source))
            copied = replace(
                reading,
                template_id=source.id,
                issues=tuple(replace(i, target=source.id) for i in reading.issues),
                issue_reviews=tuple(
                    replace(r, issue=replace(r.issue, target=source.id))
                    for r in reading.issue_reviews
                ),
            )
            self.repository.put("imported_template_reading", project_id, source.id, copied)
            if not self.repository.versions(project_id):
                return {"template_id": source.id, "version": None}
            previous = self.problem(project_id)
            if any(t == copied for t in previous.template_readings):
                return {"template_id": source.id, "version": previous.version}
            active_ids = {t.source.id for t in matches} | {source.id}
            readings = tuple(
                t
                for t in previous.template_readings
                if t.template_id in active_ids and t.template_id != source.id
            ) + (copied,)
            snapshot = replace(
                previous,
                version=previous.version + 1,
                created_at=timestamp(),
                reading=replace(
                    previous.reading,
                    rules=replace(previous.reading.rules, version=previous.version + 1),
                ),
                template_readings=readings,
                revision_note="导入已处理空表",
            )
            saved = self.repository.save_snapshot(
                project_id, obj(to_json(snapshot)), previous.version
            )
            if self.memory:
                self.memory.remember(problem_snapshot(saved))
            return {"template_id": source.id, "version": saved["version"]}

    @property
    def answerer(self) -> AnswererService:
        if self._answerer is None:
            raise ERPError("answerer_unavailable", "导出模块未配置。", 503)
        return self._answerer

    def upload_template(self, project_id: str, filename: str, content: bytes) -> JSONObject:
        self.project(project_id)
        raise ERPError("template_library_required", "请在空表库上传、解读和核对，再导入项目。", 409)

    def retry_template(self, project_id: str, template_id: str) -> JSONObject:
        self.project(project_id)
        self.repository.get("template_original", project_id, template_id)
        self._require_upload(project_id, "template", template_id)
        raise ERPError("template_library_required", "请在空表库继续解读。", 409)

    def migrate_legacy_templates(self) -> None:
        with self._template_lock:
            if not self._legacy_templates_migrated:
                self._publish_legacy_templates()
                self._legacy_templates_migrated = True

    def _publish_legacy_templates(self) -> None:
        for project in self.repository.list_all("project"):
            project_id = string(project["id"])
            readings = {}
            if self.repository.versions(project_id):
                snapshot = problem_snapshot(self.repository.snapshot(project_id))
                readings = {t.template_id: t for t in snapshot.template_readings}
            imported = {
                obj(r)["template_id"]
                for r in self.repository.list("imported_template_reading", project_id)
            }
            for value in self.repository.list("template", project_id):
                template = budget_template(obj(value))
                tid = template.source.id
                if tid in imported:
                    continue
                self.template_library.publish(
                    template,
                    self.repository.get("template_original", project_id, tid),
                    readings.get(tid),
                )

    def create_project(self, name: str) -> JSONObject:
        name = name.strip()
        if not name or len(name) > 100:
            raise ERPError("invalid_name", "项目名称必须在 1 到 100 字之间。")
        project_id = uuid4().hex
        record = {"id": project_id, "name": name, "created_at": timestamp()}
        self.repository.put("project", project_id, project_id, record)
        return obj(to_json(record))

    def project(self, project_id: str) -> JSONObject:
        return self.repository.get("project", project_id, project_id)

    def delete_project(self, project_id: str) -> None:
        self.project(project_id)
        self.jobs.cancel_project(project_id)
        self.repository.delete_project(project_id)
        if self.memory:
            self.memory.forget(project_id)

    def upload(self, project_id: str, filename: str, content: bytes) -> JSONObject:
        self.project(project_id)
        if not filename or len(filename) > 200 or any(c in filename for c in "/\\\x00"):
            raise ERPError("invalid_filename", "文件名无效。")
        fmt = filename.rsplit(".", 1)[-1].lower()
        if fmt not in {"doc", "docx", "pdf", "xls", "xlsx"}:
            raise ERPError("unsupported_format", "请上传 DOCX、DOC、PDF、XLS 或 XLSX 文件。")
        source = SourceDocument(
            uuid4().hex, filename, hashlib.sha256(content).hexdigest(), len(content), fmt
        )

        self.repository.put(
            "original",
            project_id,
            source.id,
            {
                "source": to_json(source),
                "content": base64.b64encode(content).decode(),
            },
        )
        return self._parse_job(project_id, source, content)

    def _parse_job(self, project_id: str, source: SourceDocument, content: bytes) -> JSONObject:
        def operation(control: RunControl) -> JSONValue:
            control.checkpoint("parsing")
            parsed = self.parser.parse(source, content)
            control.checkpoint("saving_document")
            self.repository.put("document", project_id, source.id, parsed)
            return {"document_id": source.id}

        return self.jobs.submit(
            project_id, "parse", operation, {"filename": source.filename, "document_id": source.id}
        )

    def retry_parse(self, project_id: str, document_id: str) -> JSONObject:
        self.project(project_id)
        self._require_upload(project_id, "document", document_id)
        original = self.repository.get("original", project_id, document_id)
        data = obj(original["source"])
        source = SourceDocument(
            string(data["id"]),
            string(data["filename"]),
            string(data["sha256"]),
            integer(data["size"], 0, 100 * 1024 * 1024),
            string(data["format"]),
        )
        content = string(original["content"], maximum=150 * 1024 * 1024)
        return self._parse_job(project_id, source, base64.b64decode(content))

    def documents(self, project_id: str) -> list[JSONValue]:
        self.project(project_id)
        return [
            to_json(parsed_document(obj(document)))
            for document in self.repository.list("document", project_id)
        ]

    def _require_upload(self, project_id: str, kind: str, upload_id: str) -> None:
        if self.repository.upload_deleted(project_id, kind, upload_id):
            raise ERPError("upload_deleted", "文件已删除，请重新上传或选择其他文件。", 409)

    def delete_upload(self, project_id: str, kind: str, upload_id: str) -> JSONObject:
        with self._read_lock, self._solve_lock, self._input_lock:
            self.project(project_id)
            original = "original" if kind == "document" else "template_original"
            self.repository.get(original, project_id, upload_id)
            for value in self.repository.list("job", project_id):
                job = obj(value)
                metadata = obj(job.get("metadata", {}))
                references = (
                    [metadata.get("document_id")]
                    + array(metadata.get("document_ids", []))
                    + array(metadata.get("source_document_ids", []))
                    if kind == "document"
                    else [metadata.get("template_id")]
                    + array(metadata.get("template_ids", []))
                    + array(metadata.get("source_template_ids", []))
                    + array(obj(metadata.get("generation", {})).get("template_ids", []))
                )
                if (
                    kind == "document"
                    and job.get("kind") == "solve"
                    and (job.get("status") in {"queued", "running"} or job.get("intervention"))
                ):
                    snapshot = self.repository.snapshot(project_id, integer(metadata["version"]))
                    references += array(snapshot.get("document_ids", []))
                if upload_id in references and (
                    job.get("status") in {"queued", "running"} or job.get("intervention")
                ):
                    self.jobs.cancel(project_id, string(job["id"]))
            return self.repository.delete_upload(project_id, kind, upload_id)

    def inputs(self, project_id: str) -> ReadingInputs:
        self.project(project_id)
        try:
            data = self.repository.get("inputs", project_id, project_id)
        except ERPError as exc:
            if exc.code != "not_found":
                raise
            return ReadingInputs()
        return reading_inputs(data)

    def save_inputs(self, project_id: str, data: JSONObject) -> ReadingInputs:
        with self._input_lock:
            previous = self.inputs(project_id)
            current = reading_inputs(data)
            if current.revision != previous.revision:
                raise ERPError("inputs_conflict", "输入内容已更新，请重新打开项目后修改。", 409)
            available = {
                parsed_document(obj(d)).source.id
                for d in self.repository.list("document", project_id)
            }
            if any(a.document_id not in available for a in current.annotations):
                raise ERPError("invalid_annotation", "文件说明引用了未解析或不属于当前项目的文件。")
            saved = replace(current, revision=previous.revision + 1)
            self.repository.put("inputs", project_id, project_id, saved)
            return saved

    def problem(self, project_id: str, version: int | None = None) -> ProblemSnapshot:
        self.project(project_id)
        snapshot = problem_snapshot(self.repository.snapshot(project_id, version))
        reading = snapshot.reading
        evidence = tuple(
            e
            for document_id in snapshot.document_ids
            for e in parsed_document(
                self.repository.get("document", project_id, document_id)
            ).evidence
        )
        evidence += tuple(e for d in snapshot.additional_sources for e in d.evidence)
        failed_ids = {i.target for i in reading.issues if i.code == "validation.unsupported_value"}
        if failed_ids and reading.raw_response:
            try:
                original = decode_reading(normalize_model_json(reading.raw_response), reading.model)
            except ERPError:
                original = None
            if original:
                original_facts = {f.id: f for f in original.problem.facts}
                facts = []
                for fact in reading.problem.facts:
                    source_fact = original_facts.get(fact.id)
                    unchanged = (
                        source_fact and replace(fact, state=source_fact.state) == source_fact
                    )
                    disputed = any(
                        i.target == fact.id
                        and i.severity != "info"
                        and not i.code.startswith("validation.")
                        for i in reading.issues
                    )
                    if (
                        fact.id in failed_ids
                        and fact.state == KnowledgeState.CONFLICTED
                        and unchanged
                        and source_fact.state == KnowledgeState.KNOWN
                        and not disputed
                    ):
                        fact = replace(fact, state=KnowledgeState.KNOWN)
                    facts.append(fact)
                reading = replace(reading, problem=replace(reading.problem, facts=tuple(facts)))
        return replace(snapshot, reading=validate_reading(reading, evidence))

    def read(self, project_id: str, data: JSONObject | None = None) -> JSONObject:
        with self._read_lock:
            return self._submit_read(project_id, data)

    def _submit_read(self, project_id: str, data: JSONObject | None) -> JSONObject:
        self.project(project_id)
        if not self.reader.llm.configured:
            raise ERPError("llm_not_configured", "服务端未配置模型，仍可上传和查看原文。", 503)
        documents = tuple(parsed_document(obj(d)) for d in self.documents(project_id))
        if data and "document_ids" in data:
            selected = strings(data["document_ids"])
            available = {d.source.id for d in documents}
            if set(selected) - available:
                raise ERPError("document_not_ready", "所选文件尚未解析完成或不属于当前项目。")
            documents = tuple(d for d in documents if d.source.id in selected)
        inputs = self.inputs(project_id)
        if (
            data
            and "input_revision" in data
            and integer(data["input_revision"], 0) != inputs.revision
        ):
            raise ERPError("inputs_conflict", "输入内容已更新，请重新加载后读取。", 409)
        versions = self.repository.versions(project_id)
        expected_version = versions[0] if versions else 0
        append = bool(data and data.get("append") is True and expected_version)
        previous = self.problem(project_id, expected_version) if append else None
        if previous:
            old_annotations = {a.document_id: a.text for a in previous.inputs.annotations}
            new_annotations = {a.document_id: a.text for a in inputs.annotations}
            if (
                inputs.problem_text != previous.inputs.problem_text
                or inputs.supplement_text != previous.inputs.supplement_text
                or any(new_annotations.get(key) != value for key, value in old_annotations.items())
                or any(
                    a.document_id in previous.document_ids
                    and old_annotations.get(a.document_id) != a.text
                    for a in inputs.annotations
                )
            ):
                raise ERPError(
                    "append_inputs_changed", "已有文字或说明已修改，请重新读取全部题目。", 409
                )
            available = {obj(d)["source"]["id"] for d in self.documents(project_id)}
            if set(previous.document_ids) - available:
                raise ERPError("append_source_deleted", "已有来源被删除，请重新读取当前文件。", 409)
            documents = tuple(d for d in documents if d.source.id not in previous.document_ids)
        extra_sources = input_sources(inputs, documents)
        if previous:
            old_extra = {d.source.id for d in previous.additional_sources}
            extra_sources = tuple(d for d in extra_sources if d.source.id not in old_extra)
        requested_templates = strings(data.get("template_ids", [])) if data else ()
        if requested_templates:
            raise ERPError("template_library_required", "空表解读与核对请在空表库完成。", 409)
        active_templates = {
            obj(t)["source"]["id"] for t in self.repository.list("template", project_id)
        }
        existing_templates = (
            {
                t.template_id: t
                for t in previous.template_readings
                if t.template_id in active_templates and t.reviewed
            }
            if previous
            else {}
        )
        for value in self.repository.list("imported_template_reading", project_id):
            interpretation = template_reading(obj(value))
            if interpretation.template_id in active_templates and interpretation.reviewed:
                existing_templates[interpretation.template_id] = interpretation
        for value in self.repository.list("job", project_id):
            job = obj(value)
            if job.get("kind") == "read" and job.get("status") in {"running", "queued"}:
                raise ERPError("reading_in_progress", "当前项目正在读题，请等待完成。", 409)
        if not documents and not extra_sources:
            raise ERPError("no_new_sources", "没有待读取的新题目资料。")
        binding = self.model_settings.capture_pool() if self.model_settings else None
        if binding and not binding.client.configured:
            raise ERPError("llm_not_configured", "当前模型预设未配置完成。", 503)

        def operation(control: RunControl) -> JSONValue:
            refreshed: list[ParsedDocument] = []
            for document in documents:
                if document.source.format == "xls" and document.parser_version == "xlrd-1":
                    control.checkpoint("parsing")
                    original = self.repository.get("original", project_id, document.source.id)
                    refreshed.append(
                        self.parser.parse(
                            document.source,
                            base64.b64decode(string(original["content"], 150 * 1024 * 1024)),
                        )
                    )
            replacements = {d.source.id: d for d in refreshed}
            sources = tuple(replacements.get(d.source.id, d) for d in documents) + extra_sources
            fingerprint = hashlib.sha256(
                dumps(
                    {
                        "inputs": inputs,
                        "sources": [d.source for d in sources],
                        "parsers": [d.parser_version for d in sources],
                        "reader": self.reader.limits,
                        "model": binding.metadata() if binding else None,
                        "version": "reader-17",
                    }
                ).encode()
            ).hexdigest()
            memory = (
                self.memory.scope(
                    project_id,
                    previous.version if previous else 0,
                    fingerprint,
                    tuple(d.source.id for d in documents),
                )
                if self.memory
                else None
            )
            client = binding.client if binding else self.reader.llm
            result = (
                self.reader.read(sources, control, client, memory)
                if sources
                else (
                    previous.reading
                    if previous
                    else ReadingResult(ProblemSpec("题目", ()), RuleSet(()), (), "未读取题目")
                )
            )
            all_ids = tuple(
                dict.fromkeys(
                    (previous.document_ids if previous else ())
                    + tuple(d.source.id for d in documents)
                )
            )
            all_extra = tuple(
                dict.fromkeys(
                    (previous.additional_sources if previous else ())
                    + extra_sources
                    + tuple(refreshed)
                )
            )
            all_documents = (
                tuple(
                    parsed_document(self.repository.get("document", project_id, tid))
                    for tid in all_ids
                )
                + all_extra
            )
            if previous and sources:
                result = merge_incremental(previous.reading, result, all_documents)
                groups = {e.id: "existing" for d in all_documents for e in d.evidence}
                groups.update({e.id: "added" for d in sources for e in d.evidence})
                findings = self.reader.reviewer.review(
                    result,
                    tuple(e for d in all_documents for e in d.evidence),
                    client,
                    control,
                    self.reader.limits.max_request_chars,
                    0,
                    review_groups=groups,
                )
                result = validate_reading(
                    replace(result, issues=result.issues + findings),
                    tuple(e for d in all_documents for e in d.evidence),
                )
            result = replace(result, rules=replace(result.rules, version=expected_version + 1))
            template_results = dict(existing_templates)
            if not sources and not previous:
                result = validate_reading(result, ())
            control.checkpoint("saving_reading")
            snapshot = ProblemSnapshot(
                project_id,
                expected_version + 1,
                timestamp(),
                all_ids,
                result,
                inputs=inputs,
                additional_sources=all_extra,
                template_readings=tuple(template_results.values()),
            )
            saved = self.repository.save_snapshot(
                project_id,
                obj(to_json(snapshot)),
                expected_version,
            )
            if self.memory:
                self.memory.remember(problem_snapshot(saved))
            return {
                "version": saved["version"],
                "issues": len(result.issues),
                "needs_input": any(i.severity == "error" for i in result.issues)
                or any(not t.reviewed for t in snapshot.template_readings),
            }

        return self.jobs.submit(
            project_id,
            "read",
            operation,
            {
                "document_ids": to_json(tuple(d.source.id for d in documents)),
                "expected_version": expected_version,
                "input_revision": inputs.revision,
                "append": append,
                "template_ids": list(requested_templates),
                "source_document_ids": list(previous.document_ids) if previous else [],
                "source_template_ids": list(existing_templates),
                "model_preset": binding.metadata() if binding else None,
            },
        )

    def revise(self, project_id: str, data: JSONObject) -> JSONObject:
        self.project(project_id)
        expected = integer(data.get("expected_version"))
        old = problem_snapshot(self.repository.snapshot(project_id, expected))
        active_templates = {
            obj(t)["source"]["id"] for t in self.repository.list("template", project_id)
        }
        old = replace(
            old,
            template_readings=tuple(
                t for t in old.template_readings if t.template_id in active_templates
            ),
        )
        note = string(data.get("note", "")).strip() or "默认"
        payload = obj(data.get("reading"))
        reading = decode_reading(dumps(payload), old.reading.model)
        evidence = tuple(
            e
            for document_id in old.document_ids
            for e in parsed_document(
                self.repository.get("document", project_id, document_id),
            ).evidence
        )
        evidence += tuple(e for d in old.additional_sources for e in d.evidence)
        reading = validate_reading(reading, evidence)
        reading = replace(
            reading,
            rules=replace(reading.rules, version=expected + 1),
            raw_response=old.reading.raw_response,
        )
        template_reviews = old.template_readings
        if data.get("template_reviews"):
            raise ERPError(
                "template_library_required", "请在空表库核对，再重新导入已确认解读。", 409
            )
        snapshot = ProblemSnapshot(
            project_id,
            expected + 1,
            timestamp(),
            old.document_ids,
            reading,
            note,
            old.inputs,
            old.additional_sources,
            template_reviews,
        )
        saved = self.repository.save_snapshot(project_id, obj(to_json(snapshot)), expected)
        if self.memory:
            self.memory.remember(problem_snapshot(saved))
        return saved

    def solve(self, project_id: str, data: JSONObject) -> JSONObject:
        with self._solve_lock:
            return self._submit_solve(project_id, data)

    def _submit_solve(self, project_id: str, data: JSONObject) -> JSONObject:
        snapshot = self.problem(project_id, integer(data.get("version")))
        if snapshot.version != self.repository.versions(project_id)[0]:
            raise ERPError("problem_version_changed", "题目已有更新，请刷新后按当前题目生成。", 409)
        if any(
            obj(job).get("kind") == "solve"
            and (
                obj(job).get("status") in {"running", "queued"}
                or bool(obj(job).get("intervention"))
            )
            for job in self.repository.list("job", project_id)
        ):
            raise ERPError("solve_in_progress", "当前项目正在生成方案，请等待完成。", 409)
        if snapshot.inputs.revision != self.inputs(project_id).revision:
            raise ERPError(
                "reading_inputs_changed", "题目输入或文件说明已修改，请重新读取后生成方案。", 409
            )
        if any(issue.severity == "error" for issue in snapshot.reading.issues):
            raise ERPError("reading_needs_revision", "请先修正读题错误并保存新版本。", 409)
        if not snapshot.reading.problem.years or not snapshot.reading.problem.periods_per_year:
            raise ERPError(
                "reading_horizon_missing",
                "经营周期存在无效值或冲突，请在题目数据中修正并保存后生成方案。",
                409,
            )
        if data.get("mode", "llm_only") != "llm_only":
            raise ERPError("solver_mode_removed", "蒙特卡罗已移除，请使用协作生成方案。", 409)
        mode = SolverMode.LLM_ONLY
        if not self.reader.llm.configured:
            raise ERPError("llm_not_configured", "请在服务端配置模型。", 503)
        config = generation_config(obj(data.get("generation", {})))
        previous_plan = None
        requirements = ()
        if config.previous_solution_id:
            previous = self.repository.get("solution", project_id, config.previous_solution_id)
            if integer(previous["problem_version"]) != snapshot.version:
                raise ERPError(
                    "previous_solution_stale", "此方案使用旧题目，请按当前题目重新生成。", 409
                )
            previous_plan = decode_plan(previous["plan"])
            previous_config = generation_config(obj(previous.get("generation", {})))
            requirements = previous_config.requirements + (config.revision_prompt,)
            if len(requirements) > 20 or sum(len(r) for r in requirements) > 50000:
                raise ERPError("revision_limit", "此方案调整记录较长，请整理要求后重新生成。", 409)
        config = replace(config, requirements=requirements)
        templates = tuple(
            budget_template(self.repository.get("template", project_id, template_id))
            for template_id in config.template_ids
        )
        for template_id in config.template_ids:
            self._require_upload(project_id, "template", template_id)
            interpretation = next(
                (t for t in snapshot.template_readings if t.template_id == template_id), None
            )
            template = next(t for t in templates if t.source.id == template_id)
            if (
                not interpretation
                or not interpretation.reviewed
                or interpretation.sha256 != template.source.sha256
            ):
                raise ERPError(
                    "template_not_reviewed", "请先在空表库解读核对并导入空表，再生成方案。", 409
                )
        if len(dumps(planning_requirements(selected_snapshot(snapshot, templates)))) > 1500000:
            raise ERPError("template_context_limit", "所选预算表参数需求过多，请精简或减少模板。")
        request = SolveRequest(
            snapshot, mode, context=PlanningContext(config, templates, previous_plan)
        )
        binding = self.model_settings.capture_pool() if self.model_settings else None
        if binding and not binding.client.configured:
            raise ERPError("llm_not_configured", "当前模型预设未配置完成。", 503)

        planning_generator = None

        def operation(control: RunControl) -> JSONValue:
            nonlocal planning_generator
            generator = self.solver.generator
            role_memory = None
            if binding or isinstance(generator, LLMStrategyGenerator):
                task = TaskContext(
                    "solver", "Solver", "策略生成", f"经营方案 · {snapshot.reading.problem.title}"
                )
                client = TaskLLMClient(binding.client if binding else generator.llm, control, task)
                if self.memory:
                    self.memory.remember(snapshot)
                    role_memory = self.memory.scope(
                        project_id,
                        snapshot.version,
                        "solver:"
                        + hashlib.sha256(
                            dumps(
                                {
                                    "config": config,
                                    "version": "solver-19",
                                    "model": binding.metadata() if binding else None,
                                }
                            ).encode()
                        ).hexdigest(),
                        snapshot.document_ids,
                    )
                    role_memory.put("requirements", {"generation": to_json(config)})
                    # 只使用冻结快照的职责投影/只读查询，不自动注入记忆检索文本。
                from erp_agent.application.planning_pipeline import ParallelPlanningGenerator

                documents = tuple(
                    parsed_document(self.repository.get("document", project_id, document_id))
                    for document_id in snapshot.document_ids
                )
                if planning_generator is None:
                    planning_generator = ParallelPlanningGenerator(
                        client, control, role_memory, documents
                    )
                generator = planning_generator
            result = self.solver.solve(request, control, generator)
            report_task(
                control,
                TaskContext("solver", "Solver", "策略生成", snapshot.reading.problem.title),
                "succeeded" if result.evaluation.feasible else "needs_input",
            )
            control.checkpoint("saving_action_plan")
            self.repository.put("solution", project_id, result.id, result)
            if self.memory:
                self.memory.remember_solution(result)
            if templates and result.evaluation.feasible:
                checker = getattr(control, "check_cancelled", None)
                if checker:
                    checker()
                try:
                    self.start_budget(project_id, result.id, binding=binding)
                except ERPError as error:
                    stage_record(
                        self.repository,
                        project_id,
                        result,
                        "budget",
                        "failed",
                        error={"code": error.code, "message": error.message},
                        template_hashes=tuple((t.source.id, t.source.sha256) for t in templates),
                    )
            return {
                "solution_id": result.id,
                "verified": result.evaluation.verified,
                "needs_input": not result.evaluation.feasible,
            }

        return self.jobs.submit(
            project_id,
            "solve",
            operation,
            {
                "mode": mode.value,
                "version": snapshot.version,
                "generation": to_json(config),
                "prompt_version": "solver-19",
                "model_preset": binding.metadata() if binding else None,
            },
        )

    def solutions(self, project_id: str) -> list[JSONObject]:
        self.project(project_id)
        return [
            public_solution(self.repository, solution_bundle(obj(value)))
            for value in self.repository.list("solution", project_id)
        ]

    def start_budget(self, project_id: str, solution_id: str, *, binding=None) -> JSONObject:
        with self._budget_lock:
            return self._submit_budget(project_id, solution_id, binding=binding)

    def _submit_budget(self, project_id: str, solution_id: str, *, binding=None) -> JSONObject:
        solution = solution_bundle(self.repository.get("solution", project_id, solution_id))
        snapshot = self.problem(project_id, solution.problem_version)
        templates = tuple(
            budget_template(self.repository.get("template", project_id, tid))
            for tid in solution.generation.template_ids
        )
        if not templates:
            raise ERPError("budget_not_applicable", "此方案没有绑定预算空表。", 409)
        if any(
            obj(j).get("kind") == "budget"
            and obj(j).get("status") in {"queued", "running"}
            and obj(obj(j).get("metadata", {})).get("solution_id") == solution_id
            for j in self.repository.list("job", project_id)
        ):
            raise ERPError("budget_in_progress", "此方案预算正在处理。", 409)
        context = PlanningContext(solution.generation, templates)
        hashes = tuple((t.source.id, t.source.sha256) for t in templates)
        attempt_id = uuid4().hex
        if not solution.plan.preparation:
            binding = binding or (
                self.model_settings.capture_pool() if self.model_settings else None
            )
        client = binding.client if binding else self.reader.llm
        stage_record(
            self.repository,
            project_id,
            solution,
            "budget",
            "queued",
            template_hashes=hashes,
            attempt_id=attempt_id,
        )

        def operation(control: RunControl) -> JSONValue:
            try:
                stage_record(
                    self.repository,
                    project_id,
                    solution,
                    "budget",
                    "running",
                    template_hashes=hashes,
                    attempt_id=attempt_id,
                )
                memory = None
                if self.memory:
                    memory = self.memory.scope(
                        project_id,
                        snapshot.version,
                        "budget:"
                        + hashlib.sha256(
                            dumps(
                                {
                                    "solution": solution.id,
                                    "plan_hash": plan_hash(solution.plan),
                                    "templates": hashes,
                                    "version": "budget-sources-1",
                                    "models": binding.metadata() if binding else None,
                                }
                            ).encode()
                        ).hexdigest(),
                        snapshot.document_ids,
                        solution_id=solution.id,
                        object_id="budget",
                    )
                planner = PlanBudgetPlanner(client, control, memory)

                def save_partial(result):
                    control.checkpoint("saving_budget_progress")
                    stage_record(
                        self.repository,
                        project_id,
                        solution,
                        "budget",
                        "partial",
                        result=result,
                        template_hashes=hashes,
                        attempt_id=attempt_id,
                    )

                if solution.plan.preparation:
                    from erp_agent.application.prepared_budget import prepared_budget

                    result = prepared_budget(solution, snapshot, templates)
                else:
                    result = planner.generate(
                        snapshot,
                        context,
                        solution.plan,
                        solution_id=solution.id,
                        on_sheet=save_partial,
                    )
                control.checkpoint("saving_budget_result")
                stage_record(
                    self.repository,
                    project_id,
                    solution,
                    "budget",
                    result.status,
                    result=result,
                    template_hashes=hashes,
                    attempt_id=attempt_id,
                )
                if result.failed_sheets:
                    raise ERPError(
                        "budget.stage_failed",
                        "生成阶段部分空表来源匹配未完成；"
                        "有效行动已保留，请调整经营方案后重新生成参数。",
                    )
                return {"solution_id": solution.id, "needs_input": bool(result.gaps)}
            except Exception as error:
                stage_record(
                    self.repository,
                    project_id,
                    solution,
                    "budget",
                    "cancelled" if isinstance(error, Cancelled) else "failed",
                    error={
                        "code": getattr(error, "code", "internal_error"),
                        "message": getattr(error, "message", "预算阶段失败。"),
                    },
                    template_hashes=hashes,
                    attempt_id=attempt_id,
                )
                raise

        try:
            return self.jobs.submit(
                project_id,
                "budget",
                operation,
                {
                    "solution_id": solution.id,
                    "version": snapshot.version,
                    "template_ids": list(solution.generation.template_ids),
                    "attempt_id": attempt_id,
                },
            )
        except ERPError as error:
            stage_record(
                self.repository,
                project_id,
                solution,
                "budget",
                "failed",
                error={"code": error.code, "message": error.message},
                template_hashes=hashes,
                attempt_id=attempt_id,
            )
            raise
