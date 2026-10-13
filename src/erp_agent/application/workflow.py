import base64
import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from erp_agent.application.answerer import AnswererService
from erp_agent.application.budget_context import budget_material
from erp_agent.application.codec import (
    budget_template,
    generation_config,
    parsed_document,
    problem_snapshot,
    reading_inputs,
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
from erp_agent.application.memory import MemoryService
from erp_agent.application.memory_client import MemoryLLMClient
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
from erp_agent.application.solver import LLMStrategyGenerator, SolverService
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    KnowledgeState,
    ParsedDocument,
    PlanningContext,
    ProblemSnapshot,
    ReadingInputs,
    SearchConfig,
    SolveRequest,
    SolverMode,
    SourceDocument,
)
from erp_agent.domain.validation import validate_reading


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowService:
    def __init__(self, repository: Repository, jobs: JobRunner, parser: DocumentParser,
                 reader: StructuredReader, solver: SolverService,
                 default_solver: SolverMode = SolverMode.LLM_ONLY,
                 model_settings: ModelSettings | None = None,
                 template_parser: BudgetTemplateParser | None = None,
                 answerer: AnswererService | None = None,
                 memory: MemoryService | None = None) -> None:
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

    @property
    def answerer(self) -> AnswererService:
        if self._answerer is None:
            raise ERPError('answerer_unavailable', '导出模块未配置。', 503)
        return self._answerer

    def upload_template(self, project_id: str, filename: str, content: bytes) -> JSONObject:
        self.project(project_id)
        if not filename or len(filename) > 200 or any(c in filename for c in "/\\\x00"):
            raise ERPError("invalid_filename", "文件名无效。")
        fmt = filename.rsplit(".", 1)[-1].lower()
        if fmt not in {"xlsx", "xls"}:
            raise ERPError("unsupported_template", "预算空表支持 XLSX 或 XLS。")
        if self.template_parser is None:
            raise ERPError("template_parser_unavailable", "预算表解析器未配置。", 503)
        source = SourceDocument(uuid4().hex, filename, hashlib.sha256(content).hexdigest(),
                                len(content), fmt)
        self.repository.put("template_original", project_id, source.id, {
            "source": to_json(source), "content": base64.b64encode(content).decode(),
        })
        return self._template_job(project_id, source, content)

    def _template_job(self, project_id: str, source: SourceDocument, content: bytes) -> JSONObject:
        parser = self.template_parser
        if parser is None:
            raise ERPError("template_parser_unavailable", "预算表解析器未配置。", 503)

        def operation(control: RunControl) -> JSONValue:
            control.checkpoint("parsing_template")
            template = parser.parse(source, content)
            control.checkpoint("saving_template")
            self.repository.put("template", project_id, source.id, template)
            return {"template_id": source.id}

        return self.jobs.submit(project_id, "template", operation,
                                {"filename": source.filename, "template_id": source.id})

    def retry_template(self, project_id: str, template_id: str) -> JSONObject:
        self.project(project_id)
        self._require_upload(project_id, "template", template_id)
        original = self.repository.get("template_original", project_id, template_id)
        data = obj(original["source"])
        source = SourceDocument(string(data["id"]), string(data["filename"]), string(data["sha256"]),
                                integer(data["size"], 0, 100 * 1024 * 1024), string(data["format"]))
        return self._template_job(project_id, source,
                                  base64.b64decode(string(original["content"], 150 * 1024 * 1024)))

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
        source = SourceDocument(uuid4().hex, filename, hashlib.sha256(content).hexdigest(),
                                len(content), fmt)

        self.repository.put("original", project_id, source.id, {
            "source": to_json(source), "content": base64.b64encode(content).decode(),
        })
        return self._parse_job(project_id, source, content)

    def _parse_job(self, project_id: str, source: SourceDocument, content: bytes) -> JSONObject:
        def operation(control: RunControl) -> JSONValue:
            control.checkpoint("parsing")
            parsed = self.parser.parse(source, content)
            control.checkpoint("saving_document")
            self.repository.put("document", project_id, source.id, parsed)
            return {"document_id": source.id}

        return self.jobs.submit(project_id, "parse", operation,
                                {"filename": source.filename, "document_id": source.id})

    def retry_parse(self, project_id: str, document_id: str) -> JSONObject:
        self.project(project_id)
        self._require_upload(project_id, "document", document_id)
        original = self.repository.get("original", project_id, document_id)
        data = obj(original["source"])
        source = SourceDocument(string(data["id"]), string(data["filename"]),
                                string(data["sha256"]),
                                integer(data["size"], 0, 100 * 1024 * 1024), string(data["format"]))
        content = string(original["content"], maximum=150 * 1024 * 1024)
        return self._parse_job(project_id, source, base64.b64decode(content))

    def documents(self, project_id: str) -> list[JSONValue]:
        self.project(project_id)
        return [to_json(parsed_document(obj(document)))
                for document in self.repository.list("document", project_id)]

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
                references = ([metadata.get("document_id")] + array(metadata.get("document_ids", []))
                              if kind == "document" else
                              [metadata.get("template_id")] + array(obj(metadata.get("generation", {})).get("template_ids", [])))
                if kind == "document" and job.get("kind") == "solve" and job.get("status") in {"queued", "running"}:
                    snapshot = self.repository.snapshot(project_id, integer(metadata["version"]))
                    references += array(snapshot.get("document_ids", []))
                if upload_id in references and job.get("status") in {"queued", "running"}:
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
            available = {parsed_document(obj(d)).source.id
                         for d in self.repository.list("document", project_id)}
            if any(a.document_id not in available for a in current.annotations):
                raise ERPError("invalid_annotation", "文件说明引用了未解析或不属于当前项目的文件。")
            saved = replace(current, revision=previous.revision + 1)
            self.repository.put("inputs", project_id, project_id, saved)
            return saved

    def problem(self, project_id: str, version: int | None = None) -> ProblemSnapshot:
        self.project(project_id)
        snapshot = problem_snapshot(self.repository.snapshot(project_id, version))
        reading = snapshot.reading
        evidence = tuple(e for document_id in snapshot.document_ids for e in parsed_document(
            self.repository.get("document", project_id, document_id)).evidence)
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
                    unchanged = source_fact and replace(fact, state=source_fact.state) == source_fact
                    disputed = any(i.target == fact.id and i.severity != "info"
                                   and not i.code.startswith("validation.") for i in reading.issues)
                    if (fact.id in failed_ids and fact.state == KnowledgeState.CONFLICTED
                            and unchanged and source_fact.state == KnowledgeState.KNOWN and not disputed):
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
        if data and "input_revision" in data and integer(data["input_revision"], 0) != inputs.revision:
            raise ERPError("inputs_conflict", "输入内容已更新，请重新加载后读取。", 409)
        extra_sources = input_sources(inputs, documents)
        for value in self.repository.list("job", project_id):
            job = obj(value)
            if job.get("kind") == "read" and job.get("status") in {"running", "queued"}:
                raise ERPError("reading_in_progress", "当前项目正在读题，请等待完成。", 409)
        if not documents and not inputs.problem_text:
            raise ERPError("no_documents", "请上传文件或填写手动题目后读取。")
        versions = self.repository.versions(project_id)
        expected_version = versions[0] if versions else 0
        binding = self.model_settings.capture_pool() if self.model_settings else None
        if binding and not binding.client.configured:
            raise ERPError("llm_not_configured", "当前模型预设未配置完成。", 503)

        def operation(control: RunControl) -> JSONValue:
            refreshed: list[ParsedDocument] = []
            for document in documents:
                if document.source.format == "xls" and document.parser_version == "xlrd-1":
                    control.checkpoint("parsing")
                    original = self.repository.get("original", project_id, document.source.id)
                    refreshed.append(self.parser.parse(document.source,
                        base64.b64decode(string(original["content"], 150 * 1024 * 1024))))
            replacements = {d.source.id: d for d in refreshed}
            sources = tuple(replacements.get(d.source.id, d) for d in documents) + extra_sources
            fingerprint = hashlib.sha256(dumps({"inputs": inputs, "sources": [d.source for d in sources],
                "parsers": [d.parser_version for d in sources], "reader": self.reader.limits,
                "model": binding.metadata() if binding else None, "version": "reader-16"}).encode()).hexdigest()
            memory = self.memory.scope(project_id, 0, fingerprint,
                                       tuple(d.source.id for d in documents)) if self.memory else None
            result = self.reader.read(sources, control, binding.client if binding else None, memory)
            result = replace(result, rules=replace(result.rules, version=expected_version + 1))
            control.checkpoint("saving_reading")
            snapshot = ProblemSnapshot(project_id, expected_version + 1, timestamp(),
                                       tuple(d.source.id for d in documents), result,
                                       inputs=inputs, additional_sources=extra_sources + tuple(refreshed))
            saved = self.repository.save_snapshot(
                project_id, obj(to_json(snapshot)), expected_version,
            )
            if self.memory:
                self.memory.remember(problem_snapshot(saved))
            return {"version": saved["version"], "issues": len(result.issues),
                    "needs_input": any(i.severity == "error" for i in result.issues)}

        return self.jobs.submit(project_id, "read", operation,
                                {"document_ids": to_json(tuple(d.source.id for d in documents)),
                                 "expected_version": expected_version,
                                 "input_revision": inputs.revision,
                                 "model_preset": binding.metadata() if binding else None})

    def revise(self, project_id: str, data: JSONObject) -> JSONObject:
        self.project(project_id)
        expected = integer(data.get("expected_version"))
        old = problem_snapshot(self.repository.snapshot(project_id, expected))
        note = string(data.get("note", "")).strip() or "默认"
        payload = obj(data.get("reading"))
        reading = decode_reading(dumps(payload), old.reading.model)
        evidence = tuple(
            e for document_id in old.document_ids
            for e in parsed_document(
                self.repository.get("document", project_id, document_id),
            ).evidence
        )
        evidence += tuple(e for d in old.additional_sources for e in d.evidence)
        reading = validate_reading(reading, evidence)
        reading = replace(reading, rules=replace(reading.rules, version=expected + 1),
                          raw_response=old.reading.raw_response)
        snapshot = ProblemSnapshot(project_id, expected + 1, timestamp(), old.document_ids,
                                   reading, note, old.inputs, old.additional_sources)
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
        if any(obj(job).get("kind") == "solve" and obj(job).get("status") in {"running", "queued"}
               for job in self.repository.list("job", project_id)):
            raise ERPError("solve_in_progress", "当前项目正在生成方案，请等待完成。", 409)
        if snapshot.inputs.revision != self.inputs(project_id).revision:
            raise ERPError("reading_inputs_changed", "题目输入或文件说明已修改，请重新读取后生成方案。", 409)
        if any(issue.severity == "error" for issue in snapshot.reading.issues):
            raise ERPError("reading_needs_revision", "请先修正读题错误并保存新版本。", 409)
        if not snapshot.reading.problem.years or not snapshot.reading.problem.periods_per_year:
            raise ERPError("reading_horizon_missing", "经营周期存在无效值或冲突，请在题目数据中修正并保存后生成方案。", 409)
        try:
            mode = SolverMode(string(data.get("mode", self.default_solver.value)))
        except ValueError as exc:
            raise ERPError("invalid_solver_mode", "解题策略无效。") from exc
        if mode == SolverMode.LLM_MONTE_CARLO and not self.solver.evaluator.supports_scoring:
            raise ERPError("simulation_not_ready", "蒙特卡罗模式等待经营仿真内核实现。", 409)
        if not self.reader.llm.configured:
            raise ERPError("llm_not_configured", "请在服务端配置模型。", 503)
        config = generation_config(obj(data.get("generation", {})))
        if mode == SolverMode.LLM_MONTE_CARLO and config.template_ids:
            raise ERPError("budget_search_unavailable", "预算条目规划当前仅支持单 LLM 模式。", 409)
        previous_plan = None
        requirements = ()
        if config.previous_solution_id:
            previous = self.repository.get("solution", project_id, config.previous_solution_id)
            if integer(previous["problem_version"]) != snapshot.version:
                raise ERPError("previous_solution_stale", "此方案使用旧题目，请按当前题目重新生成。", 409)
            previous_plan = decode_plan(previous["plan"])
            previous_config = generation_config(obj(previous.get("generation", {})))
            requirements = previous_config.requirements + (config.revision_prompt,)
            if len(requirements) > 20 or sum(len(r) for r in requirements) > 50000:
                raise ERPError("revision_limit", "此方案调整记录较长，请整理要求后重新生成。", 409)
        config = replace(config, requirements=requirements)
        templates = tuple(budget_template(self.repository.get("template", project_id, template_id))
                          for template_id in config.template_ids)
        for template_id in config.template_ids:
            self._require_upload(project_id, "template", template_id)
        if len(dumps(budget_material(templates))) > 1500000:
            raise ERPError("template_context_limit", "所选预算表内容过多，请精简或减少模板。")
        request = SolveRequest(snapshot, mode, SearchConfig(
            integer(data.get("seed", 42), 0, 2**32 - 1),
            integer(data.get("candidates", 100), 1, 2000),
        ), PlanningContext(config, templates, previous_plan))
        binding = self.model_settings.capture_pool() if self.model_settings else None
        if binding and not binding.client.configured:
            raise ERPError("llm_not_configured", "当前模型预设未配置完成。", 503)

        def operation(control: RunControl) -> JSONValue:
            generator = self.solver.generator
            if binding or isinstance(generator, LLMStrategyGenerator):
                task = TaskContext("solver", "Solver", "策略生成", f"经营方案 · {snapshot.reading.problem.title}")
                client = TaskLLMClient(binding.client if binding else generator.llm, control, task)
                if self.memory:
                    self.memory.remember(snapshot)
                    role_memory = self.memory.scope(project_id, snapshot.version,
                        "solver:" + hashlib.sha256(dumps(config).encode()).hexdigest())
                    role_memory.put("requirements", {"generation": to_json(config)})
                    client = MemoryLLMClient(client, role_memory, "经营策略 " + dumps(config))
                generator = LLMStrategyGenerator(
                    client,
                    lambda: control.checkpoint("repairing_strategy"),
                    search_parameters=mode == SolverMode.LLM_MONTE_CARLO,
                )
            result = self.solver.solve(request, control, generator)
            report_task(control, TaskContext("solver", "Solver", "策略生成", snapshot.reading.problem.title),
                        "succeeded" if result.evaluation.feasible else "needs_input")
            self.repository.put("solution", project_id, result.id, result)
            return {"solution_id": result.id, "verified": result.evaluation.verified,
                    "needs_input": not result.evaluation.feasible}

        return self.jobs.submit(project_id, "solve", operation,
                                {"mode": mode.value, "version": snapshot.version,
                                 "search": to_json(request.search),
                                 "generation": to_json(config), "prompt_version": "solver-4",
                                 "model_preset": binding.metadata() if binding else None})
