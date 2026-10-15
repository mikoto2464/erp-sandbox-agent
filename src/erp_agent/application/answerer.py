import base64
import hashlib
import threading
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from erp_agent.application.codec import (
    budget_template,
    parsed_document,
    problem_snapshot,
    solution_bundle,
    workbook_entries,
    workbook_mapping,
)
from erp_agent.application.contracts import (
    JSONObject,
    JSONValue,
    dumps,
    integer,
    obj,
    string,
    to_json,
)
from erp_agent.application.memory import MemoryService
from erp_agent.application.memory_client import MemoryLLMClient
from erp_agent.application.ports import (
    JobRunner,
    ModelSettings,
    ReportComposer,
    ReportRenderer,
    Repository,
    RunControl,
    TaskContext,
    WorkbookMappingAnalyzer,
    WorkbookRenderer,
)
from erp_agent.application.report_composer import REPORT_VERSION, CollaborativeReportComposer
from erp_agent.application.solution_stages import (
    optional_record,
    plan_hash,
    project_solution,
    stage_record,
)
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.application.workbook_mapper import WorkbookMapper
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import (
    ArtifactManifest,
    BudgetTemplate,
    OutputSource,
    ReportData,
    WorkbookEntry,
    WorkbookMapping,
)
from erp_agent.domain.workbook import mapped_value, output_sources, validate_mapping


class AnswererService:
    def __init__(
        self,
        repository: Repository,
        jobs: JobRunner,
        models: ModelSettings,
        report_renderer: ReportRenderer,
        workbook_renderer: WorkbookRenderer,
        mapper: WorkbookMappingAnalyzer | None = None,
        memory: MemoryService | None = None,
        composer: ReportComposer | None = None,
    ) -> None:
        self.repository, self.jobs, self.models = repository, jobs, models
        self.report_renderer, self.workbook_renderer = report_renderer, workbook_renderer
        self.mapper = mapper
        self.memory = memory
        self.composer = composer
        self._lock = threading.Lock()

    def context(self, project_id: str, solution_id: str) -> ReportData:
        self.repository.get("project", project_id, project_id)
        solution = solution_bundle(self.repository.get("solution", project_id, solution_id))
        solution = project_solution(self.repository, solution)
        snapshot = problem_snapshot(self.repository.snapshot(project_id, solution.problem_version))
        sources = tuple(
            e
            for doc_id in snapshot.document_ids
            for e in parsed_document(self.repository.get("document", project_id, doc_id)).evidence
        )
        sources += tuple(e for d in snapshot.additional_sources for e in d.evidence)
        return ReportData(snapshot, solution, sources)

    def public_mapping(self, mapping: WorkbookMapping) -> JSONObject:
        report = self.context(mapping.project_id, mapping.solution_id)
        result = obj(to_json(mapping))
        sources = output_sources(report.solution, report.snapshot)
        result["sources"] = to_json(sources)
        result["values"] = [
            None if not entry.source_ids else self._preview_value(entry, sources)
            for entry in mapping.entries
        ]
        return result

    @staticmethod
    def _preview_value(entry: WorkbookEntry, sources: tuple[OutputSource, ...]) -> JSONValue:
        try:
            value = mapped_value(entry, sources)
        except ERPError:
            return None
        return None if value is None else str(value)

    def mappings(self, project_id: str) -> list[JSONObject]:
        return [
            self.public_mapping(workbook_mapping(obj(value)))
            for value in self.repository.list("mapping", project_id)
        ]

    def analyze(self, project_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            return self._analyze(project_id, data)

    def _analyze(self, project_id: str, data: JSONObject) -> JSONObject:
        report = self.context(project_id, string(data.get("solution_id"), 100))
        template = budget_template(
            self.repository.get("template", project_id, string(data.get("template_id"), 100))
        )
        self._require_planned_template(report, template)
        self._check_active(project_id, "mapping", report.solution.id, template.source.id)
        binding = None if report.solution.plan.preparation else self.models.capture_pool()

        def operation(control: RunControl) -> JSONValue:
            task = TaskContext("mapper", "Answerer", "核对既有方案预算", template.source.filename)
            report_task(control, task, "running")
            client = TaskLLMClient(binding.client, control, task) if binding else None
            shared = None
            if self.memory and binding:
                self.memory.remember(report.snapshot)
                self.memory.remember_solution(report.solution)
                shared = self.memory.scope(
                    project_id,
                    report.snapshot.version,
                    "mapper:"
                    + hashlib.sha256(
                        dumps(
                            {
                                "solution": report.solution.id,
                                "template": template.source.sha256,
                                "version": "mapper-5",
                                "models": binding.metadata(),
                            }
                        ).encode()
                    ).hexdigest(),
                    report.snapshot.document_ids,
                    solution_id=report.solution.id,
                    object_id=template.source.id,
                )
                shared.put(
                    "solution",
                    {
                        "solution_id": report.solution.id,
                        "generation": to_json(report.solution.generation),
                    },
                )
                client = MemoryLLMClient(
                    client, shared, "已有预算来源、单位与期间 " + template.source.filename
                )
            mapper = self.mapper or WorkbookMapper(shared)
            mapping = mapper.analyze(report.solution, template, client, control, report.snapshot)
            if any(issue.severity == "error" for issue in mapping.issues):
                raise ERPError(
                    "mapping_invalid", "方案预算对应关系检查未通过，请回到经营方案修正。", 409
                )
            report_task(control, task, "needs_input", entry_count=len(mapping.entries))
            control.checkpoint("saving_mapping")
            mapping = replace(
                mapping,
                plan_hash=plan_hash(report.solution.plan),
                budget_hash=self._budget_hash(project_id, report.solution.id),
            )
            self.repository.put("mapping", project_id, mapping.id, mapping)
            return {"mapping_id": mapping.id, "needs_input": True}

        return self.jobs.submit(
            project_id,
            "mapping",
            operation,
            {
                "solution_id": report.solution.id,
                "template_id": template.source.id,
                "model_preset": binding.metadata() if binding else None,
            },
        )

    def confirm(self, project_id: str, mapping_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            mapping = workbook_mapping(self.repository.get("mapping", project_id, mapping_id))
            if integer(data.get("expected_revision")) != mapping.revision:
                raise ERPError("mapping_conflict", "映射已经更新，请重新打开后修改。", 409)
            report = self.context(project_id, mapping.solution_id)
            self._check_mapping_binding(mapping, report)
            template = budget_template(
                self.repository.get("template", project_id, mapping.template_id)
            )
            self._require_planned_template(report, template)
            if template.source.sha256 != mapping.template_sha256:
                raise ERPError("template_changed", "模板内容已变化，请重新分析。", 409)
            entries = workbook_entries(data.get("entries"))
            issues = validate_mapping(template, entries, report.solution, report.snapshot)
            if any(i.severity == "error" for i in issues):
                raise ERPError(
                    "mapping_invalid",
                    "映射不能保存："
                    + "；".join(i.message for i in issues if i.severity == "error"),
                )
            saved = replace(
                mapping,
                entries=entries,
                issues=issues,
                confirmed=True,
                revision=mapping.revision + 1,
            )
            self.repository.put("mapping", project_id, mapping.id, saved)
            return self.public_mapping(saved)

    def export(self, project_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            return self._export(project_id, data)

    def _export(self, project_id: str, data: JSONObject) -> JSONObject:
        report = self.context(project_id, string(data.get("solution_id"), 100))
        kind = string(data.get("kind"))
        if kind not in {"docx", "xlsx"}:
            raise ERPError("invalid_artifact_kind", "导出格式应为 DOCX 或 XLSX。")
        self._check_active(project_id, "artifact", report.solution.id)
        if any(
            i.severity == "error" and i.code.startswith("plan.")
            for i in report.solution.evaluation.issues
        ):
            raise ERPError("plan_invalid", "方案的周期或操作结构有错误，请先调整方案。", 409)
        binding = (
            self.models.capture_pool()
            if kind == "docx" and not report.solution.plan.preparation
            else None
        )
        if binding and not binding.client.configured:
            raise ERPError("llm_not_configured", "Word 方案组织需要已配置的模型。", 503)
        mapping = None
        template = None
        content = None
        if kind == "xlsx" and data.get("mapping_id"):
            mapping = workbook_mapping(
                self.repository.get("mapping", project_id, string(data["mapping_id"]))
            )
            self._check_mapping_binding(mapping, report)
            if mapping.solution_id != report.solution.id or not mapping.confirmed:
                raise ERPError("mapping_not_confirmed", "请先确认这份方案的预算表映射。", 409)
            template = budget_template(
                self.repository.get("template", project_id, mapping.template_id)
            )
            self._require_planned_template(report, template)
            if template.source.sha256 != mapping.template_sha256:
                raise ERPError("template_changed", "模板已变更，请重新分析。", 409)
            if any(
                i.severity == "error"
                for i in validate_mapping(
                    template, mapping.entries, report.solution, report.snapshot
                )
            ):
                raise ERPError("mapping_invalid", "映射检查未通过。", 409)
            original = self.repository.get("template_original", project_id, mapping.template_id)
            content = base64.b64decode(string(original["content"], 150 * 1024 * 1024))
        suffix = (
            "经营方案"
            if kind == "docx"
            else template.source.filename.rsplit(".", 1)[0]
            if template
            else "标准操作预算表"
        )
        filename = (
            suffix + ("_草稿" if not report.solution.evaluation.verified else "") + "." + kind
        )

        captured_budget_hash = self._budget_hash(project_id, report.solution.id)

        def render_operation(control: RunControl) -> JSONValue:
            output_report = report
            root = TaskContext(
                "answerer",
                "Answerer",
                "组织方案文档" if kind == "docx" else "填写方案预算",
                filename,
            )
            report_task(control, root, "running")
            render_task = TaskContext(
                "answerer:render",
                "Answerer",
                "生成 Word 文档" if kind == "docx" else "写入预算空格",
                filename,
                "answerer",
            )
            verify_task = TaskContext(
                "answerer:validate",
                "Validator",
                "核对文件与方案一致性",
                filename,
                "answerer",
            )
            report_task(control, render_task, "queued")
            report_task(control, verify_task, "queued")
            if report.solution.plan.preparation:
                output_report = replace(report, outline=report.solution.plan.preparation.outline)
            elif binding:
                control.checkpoint("organizing_report")
                client = binding.client
                shared = None
                if self.memory:
                    self.memory.remember(report.snapshot)
                    self.memory.remember_solution(report.solution)
                    shared = self.memory.scope(
                        project_id,
                        report.snapshot.version,
                        "answerer:"
                        + hashlib.sha256(
                            dumps(
                                {
                                    "solution": report.solution.id,
                                    "version": REPORT_VERSION,
                                    "models": binding.metadata(),
                                }
                            ).encode()
                        ).hexdigest(),
                        report.snapshot.document_ids,
                        solution_id=report.solution.id,
                        object_id="report",
                    )
                    shared.put("solution", {"solution_id": report.solution.id})
                    client = MemoryLLMClient(client, shared, "当前方案战略、时点与执行风险")
                composer = self.composer or CollaborativeReportComposer(shared)
                outline = composer.compose(report, client, control)
                output_report = replace(report, outline=outline)
            report_task(control, render_task, "running")
            control.checkpoint("rendering_report" if kind == "docx" else "filling_workbook")
            rendered = (
                self.report_renderer.render(output_report)
                if kind == "docx"
                else self.workbook_renderer.render(output_report, template, content, mapping)
            )
            report_task(control, render_task, "succeeded")
            report_task(control, verify_task, "running")
            control.checkpoint("validating_artifact")
            manifest = ArtifactManifest(
                uuid4().hex,
                project_id,
                report.solution.id,
                report.solution.problem_version,
                kind,
                filename,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if kind == "docx"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                hashlib.sha256(rendered.content).hexdigest(),
                len(rendered.content),
                datetime.now(UTC).isoformat(),
                not report.solution.evaluation.verified,
                rendered.formula_status,
                rendered.warnings,
                template.source.id if template else None,
                mapping.id if mapping else None,
                mapping.revision if mapping else None,
            )
            manifest = replace(
                manifest,
                plan_hash=plan_hash(report.solution.plan),
                budget_hash=captured_budget_hash,
            )
            control.checkpoint("saving_artifact")
            self.repository.put(
                "artifact_file",
                project_id,
                manifest.id,
                {
                    "content": base64.b64encode(rendered.content).decode(),
                    "sha256": manifest.sha256,
                },
            )
            self.repository.put("artifact", project_id, manifest.id, manifest)
            if output_report.outline:
                self.repository.put(
                    "artifact_report",
                    project_id,
                    manifest.id,
                    {
                        "solution_id": report.solution.id,
                        "problem_version": report.solution.problem_version,
                        "outline": to_json(output_report.outline),
                        "plan_hash": plan_hash(report.solution.plan),
                        "budget_hash": captured_budget_hash,
                        "prompt_version": REPORT_VERSION,
                        "model_preset": binding.metadata() if binding else None,
                    },
                )
            report_task(control, verify_task, "succeeded")
            report_task(control, root, "succeeded")
            return {"artifact_id": manifest.id}

        attempt_id = uuid4().hex
        hashes = tuple(
            (t.template_id, t.sha256)
            for t in report.snapshot.template_readings
            if t.template_id in report.solution.generation.template_ids
        )
        if kind == "docx":
            stage_record(
                self.repository,
                project_id,
                report.solution,
                "report",
                "queued",
                template_hashes=hashes,
                attempt_id=attempt_id,
            )

        def operation(control: RunControl) -> JSONValue:
            try:
                if kind == "docx":
                    stage_record(
                        self.repository,
                        project_id,
                        report.solution,
                        "report",
                        "running",
                        template_hashes=hashes,
                        attempt_id=attempt_id,
                    )
                result = render_operation(control)
                if kind == "docx":
                    stage_record(
                        self.repository,
                        project_id,
                        report.solution,
                        "report",
                        "succeeded",
                        result=result,
                        template_hashes=hashes,
                        attempt_id=attempt_id,
                    )
                return result
            except Exception as error:
                if kind == "docx":
                    stage_record(
                        self.repository,
                        project_id,
                        report.solution,
                        "report",
                        "cancelled" if isinstance(error, Cancelled) else "failed",
                        error={
                            "code": getattr(error, "code", "internal_error"),
                            "message": getattr(error, "message", "报告阶段失败。"),
                        },
                        template_hashes=hashes,
                        attempt_id=attempt_id,
                    )
                raise

        try:
            return self.jobs.submit(
                project_id,
                "artifact",
                operation,
                {
                    "solution_id": report.solution.id,
                    "attempt_id": attempt_id,
                    "kind": kind,
                    "mapping_id": mapping.id if mapping else None,
                    "template_id": template.source.id if template else None,
                    "prompt_version": REPORT_VERSION if kind == "docx" else None,
                    "model_preset": binding.metadata() if binding else None,
                },
            )
        except ERPError as error:
            if kind == "docx":
                stage_record(
                    self.repository,
                    project_id,
                    report.solution,
                    "report",
                    "failed",
                    error={"code": error.code, "message": error.message},
                    template_hashes=hashes,
                    attempt_id=attempt_id,
                )
            raise

    def _require_planned_template(self, report: ReportData, template: BudgetTemplate) -> None:
        template_id = template.source.id
        reading = next(
            (t for t in report.snapshot.template_readings if t.template_id == template_id), None
        )
        if not reading or not reading.reviewed:
            raise ERPError(
                "template_not_reviewed", "空表须先在空表库解读核对并导入，再生成方案。", 409
            )
        if reading.sha256 != template.source.sha256:
            raise ERPError("template_changed", "空表与已读版本不一致，请重新读取并生成方案。", 409)
        if template_id not in report.solution.generation.template_ids:
            raise ERPError(
                "template_not_planned", "请在经营方案处选择这份空表并生成方案后再填写。", 409
            )
        budget = optional_record(
            self.repository, "budget_result", report.solution.project_id, report.solution.id
        )
        if budget:
            completed = obj(budget.get("result") or {}).get("completed_sheets", [])
            if any(
                [template_id, sheet.name] not in completed
                for sheet in reading.sheets
                if sheet.input_groups
            ):
                message = (
                    "此方案生成时未完成这份表的预算来源，请调整经营方案后重新生成。"
                    if report.solution.plan.preparation
                    and report.solution.plan.preparation.failed_sheets
                    else "此空表预算尚未完整处理，可等待或重试预算阶段。"
                )
                raise ERPError("template_budget_pending", message, 409)
        if not any(item.template_id == template_id for item in report.solution.plan.budget_items):
            raise ERPError(
                "template_plan_missing", "此方案没有这份表的预算安排，请返回经营方案重新生成。", 409
            )
        if any(
            i.severity == "error" and i.code.startswith("budget.")
            for i in report.solution.evaluation.issues
        ):
            raise ERPError(
                "template_plan_invalid", "方案预算存在遗漏或输入错误，请先返回经营方案修正。", 409
            )

    def _budget_hash(self, project_id: str, solution_id: str) -> str:
        record = optional_record(self.repository, "budget_result", project_id, solution_id)
        return hashlib.sha256(dumps(record.get("result")).encode()).hexdigest() if record else ""

    def _check_mapping_binding(self, mapping: WorkbookMapping, report: ReportData) -> None:
        if mapping.plan_hash and mapping.plan_hash != plan_hash(report.solution.plan):
            raise ERPError("mapping_plan_changed", "映射绑定的行动方案已变化。", 409)
        if mapping.budget_hash and mapping.budget_hash != self._budget_hash(
            report.solution.project_id, report.solution.id
        ):
            raise ERPError("mapping_budget_changed", "预算阶段已更新，请重新核对填写来源。", 409)

    def _check_active(
        self, project_id: str, kind: str, solution_id: str, template_id: str | None = None
    ) -> None:
        for value in self.repository.list("job", project_id):
            job = obj(value)
            metadata = obj(job.get("metadata", {}))
            if (
                job.get("kind") == kind
                and job.get("status") in {"queued", "running"}
                and metadata.get("solution_id") == solution_id
                and (template_id is None or metadata.get("template_id") == template_id)
            ):
                raise ERPError("output_in_progress", "相关生成任务正在执行，请等待完成。", 409)

    def download(self, project_id: str, artifact_id: str) -> tuple[JSONObject, bytes]:
        manifest = self.repository.get("artifact", project_id, artifact_id)
        stored = self.repository.get("artifact_file", project_id, artifact_id)
        content = base64.b64decode(string(stored["content"], 150 * 1024 * 1024))
        if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise ERPError("artifact_corrupted", "导出文件校验失败，请重新生成。", 500)
        return manifest, content
