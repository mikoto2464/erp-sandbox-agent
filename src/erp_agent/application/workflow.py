import base64
import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from erp_agent.application.codec import parsed_document, problem_snapshot
from erp_agent.application.contracts import (
    JSONObject, JSONValue, decode_reading, dumps, integer, obj, string, strings, to_json,
)
from erp_agent.application.ports import DocumentParser, JobRunner, ModelSettings, Repository, RunControl
from erp_agent.application.reader import StructuredReader
from erp_agent.application.solver import LLMStrategyGenerator, SolverService
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    ProblemSnapshot, SearchConfig, SolveRequest, SolverMode, SourceDocument,
)
from erp_agent.domain.validation import validate_reading


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowService:
    def __init__(self, repository: Repository, jobs: JobRunner, parser: DocumentParser,
                 reader: StructuredReader, solver: SolverService,
                 default_solver: SolverMode = SolverMode.LLM_ONLY,
                 model_settings: ModelSettings | None = None) -> None:
        self.repository, self.jobs, self.parser = repository, jobs, parser
        self.reader, self.solver = reader, solver
        self.default_solver = default_solver
        self.model_settings = model_settings
        self._read_lock = threading.Lock()

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

    def upload(self, project_id: str, filename: str, content: bytes) -> JSONObject:
        self.project(project_id)
        if not filename or len(filename) > 200 or any(c in filename for c in "/\\\x00"):
            raise ERPError("invalid_filename", "文件名无效。")
        fmt = filename.rsplit(".", 1)[-1].lower()
        if fmt not in {"doc", "docx"}:
            raise ERPError("unsupported_format", "请上传 DOCX 或 DOC 题目。")
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
        original = self.repository.get("original", project_id, document_id)
        data = obj(original["source"])
        source = SourceDocument(string(data["id"]), string(data["filename"]),
                                string(data["sha256"]),
                                integer(data["size"], 0, 100 * 1024 * 1024), string(data["format"]))
        content = string(original["content"], maximum=150 * 1024 * 1024)
        return self._parse_job(project_id, source, base64.b64decode(content))

    def documents(self, project_id: str) -> list[JSONValue]:
        self.project(project_id)
        return self.repository.list("document", project_id)

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
        for value in self.repository.list("job", project_id):
            job = obj(value)
            if job.get("kind") == "read" and job.get("status") in {"running", "queued"}:
                raise ERPError("reading_in_progress", "当前项目正在读题，请等待完成。", 409)
        if not documents:
            raise ERPError("no_documents", "请先上传并等待文档解析完成。")
        versions = self.repository.versions(project_id)
        expected_version = versions[0] if versions else 0
        binding = self.model_settings.capture() if self.model_settings else None
        if binding and not binding.client.configured:
            raise ERPError("llm_not_configured", "当前模型预设未配置完成。", 503)

        def operation(control: RunControl) -> JSONValue:
            result = (self.reader.read(documents, control, binding.client) if binding
                      else self.reader.read(documents, control))
            result = replace(result, rules=replace(result.rules, version=expected_version + 1))
            control.checkpoint("saving_reading")
            snapshot = ProblemSnapshot(project_id, expected_version + 1, timestamp(),
                                       tuple(d.source.id for d in documents), result)
            saved = self.repository.save_snapshot(
                project_id, obj(to_json(snapshot)), expected_version,
            )
            return {"version": saved["version"], "issues": len(result.issues),
                    "needs_input": any(i.severity == "error" for i in result.issues)}

        return self.jobs.submit(project_id, "read", operation,
                                {"document_ids": to_json(tuple(d.source.id for d in documents)),
                                 "expected_version": expected_version,
                                 "model_preset": binding.metadata() if binding else None})

    def revise(self, project_id: str, data: JSONObject) -> JSONObject:
        self.project(project_id)
        expected = integer(data.get("expected_version"))
        old = problem_snapshot(self.repository.snapshot(project_id, expected))
        note = string(data.get("note", "")).strip()
        if not note:
            raise ERPError("revision_note_required", "请说明修改依据或假设。")
        payload = obj(data.get("reading"))
        reading = decode_reading(dumps(payload), old.reading.model)
        evidence = tuple(
            e for document_id in old.document_ids
            for e in parsed_document(
                self.repository.get("document", project_id, document_id),
            ).evidence
        )
        reading = validate_reading(reading, evidence)
        reading = replace(reading, rules=replace(reading.rules, version=expected + 1),
                          raw_response=old.reading.raw_response)
        snapshot = ProblemSnapshot(project_id, expected + 1, timestamp(), old.document_ids,
                                   reading, note)
        return self.repository.save_snapshot(project_id, obj(to_json(snapshot)), expected)

    def solve(self, project_id: str, data: JSONObject) -> JSONObject:
        self.project(project_id)
        snapshot = problem_snapshot(self.repository.snapshot(project_id,
                                                            integer(data.get("version"))))
        if any(issue.severity == "error" for issue in snapshot.reading.issues):
            raise ERPError("reading_needs_revision", "请先修正读题错误并保存新版本。", 409)
        if not snapshot.reading.problem.years or not snapshot.reading.problem.periods_per_year:
            raise ERPError("reading_horizon_missing", "请在读题复核中补充经营年数和每年期数，保存校验后再生成方案。", 409)
        try:
            mode = SolverMode(string(data.get("mode", self.default_solver.value)))
        except ValueError as exc:
            raise ERPError("invalid_solver_mode", "解题策略无效。") from exc
        if mode == SolverMode.LLM_MONTE_CARLO and not self.solver.evaluator.supports_scoring:
            raise ERPError("simulation_not_ready", "蒙特卡罗模式等待经营仿真内核实现。", 409)
        if not self.reader.llm.configured:
            raise ERPError("llm_not_configured", "请在服务端配置模型。", 503)
        request = SolveRequest(snapshot, mode, SearchConfig(
            integer(data.get("seed", 42), 0, 2**32 - 1),
            integer(data.get("candidates", 100), 1, 2000),
        ))
        binding = self.model_settings.capture() if self.model_settings else None
        if binding and not binding.client.configured:
            raise ERPError("llm_not_configured", "当前模型预设未配置完成。", 503)

        def operation(control: RunControl) -> JSONValue:
            generator = self.solver.generator
            if binding or isinstance(generator, LLMStrategyGenerator):
                generator = LLMStrategyGenerator(
                    binding.client if binding else generator.llm,
                    lambda: control.checkpoint("repairing_strategy"),
                    search_parameters=mode == SolverMode.LLM_MONTE_CARLO,
                )
            result = self.solver.solve(request, control, generator)
            self.repository.put("solution", project_id, result.id, result)
            return {"solution_id": result.id, "verified": result.evaluation.verified,
                    "needs_input": not result.evaluation.feasible}

        return self.jobs.submit(project_id, "solve", operation,
                                {"mode": mode.value, "version": snapshot.version,
                                 "search": to_json(request.search),
                                 "model_preset": binding.metadata() if binding else None})
