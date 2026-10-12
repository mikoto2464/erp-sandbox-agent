import base64
import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from erp_agent.application.codec import (
    budget_template, parsed_document, problem_snapshot, solution_bundle, workbook_entries, workbook_mapping,
)
from erp_agent.application.contracts import JSONObject, JSONValue, integer, obj, string, to_json
from erp_agent.application.ports import (
    JobRunner, ModelSettings, ReportRenderer, Repository, RunControl, WorkbookRenderer,
    WorkbookMappingAnalyzer,
)
from erp_agent.application.workbook_mapper import WorkbookMapper
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import ArtifactManifest, OutputSource, ReportData, WorkbookEntry, WorkbookMapping
from erp_agent.domain.workbook import mapped_value, output_sources, validate_mapping


class AnswererService:
    def __init__(self, repository: Repository, jobs: JobRunner, models: ModelSettings,
                 report_renderer: ReportRenderer, workbook_renderer: WorkbookRenderer,
                 mapper: WorkbookMappingAnalyzer | None = None) -> None:
        self.repository, self.jobs, self.models = repository, jobs, models
        self.report_renderer, self.workbook_renderer = report_renderer, workbook_renderer
        self.mapper = mapper or WorkbookMapper()
        self._lock = threading.Lock()

    def context(self, project_id: str, solution_id: str) -> ReportData:
        self.repository.get('project', project_id, project_id)
        solution = solution_bundle(self.repository.get('solution', project_id, solution_id))
        snapshot = problem_snapshot(self.repository.snapshot(project_id, solution.problem_version))
        sources = tuple(e for doc_id in snapshot.document_ids for e in parsed_document(
            self.repository.get('document', project_id, doc_id)).evidence)
        sources += tuple(e for d in snapshot.additional_sources for e in d.evidence)
        return ReportData(snapshot, solution, sources)

    def public_mapping(self, mapping: WorkbookMapping) -> JSONObject:
        report = self.context(mapping.project_id, mapping.solution_id)
        result = obj(to_json(mapping))
        sources = output_sources(report.solution)
        result['sources'] = to_json(sources)
        result['values'] = [None if not entry.source_ids else self._preview_value(entry, sources)
                            for entry in mapping.entries]
        return result

    @staticmethod
    def _preview_value(entry: WorkbookEntry, sources: tuple[OutputSource, ...]) -> JSONValue:
        try:
            value = mapped_value(entry, sources)
        except ERPError:
            return None
        return None if value is None else str(value)

    def mappings(self, project_id: str) -> list[JSONObject]:
        return [self.public_mapping(workbook_mapping(obj(value)))
                for value in self.repository.list('mapping', project_id)]

    def analyze(self, project_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            return self._analyze(project_id, data)

    def _analyze(self, project_id: str, data: JSONObject) -> JSONObject:
        report = self.context(project_id, string(data.get('solution_id'), 100))
        template = budget_template(self.repository.get('template', project_id, string(data.get('template_id'), 100)))
        self._check_active(project_id, 'mapping', report.solution.id, template.source.id)
        needs_llm = not any(i.template_id == template.source.id for i in report.solution.plan.budget_items)
        binding = self.models.capture()
        if needs_llm and not binding.client.configured:
            raise ERPError('llm_not_configured', '新模板映射需要已配置的模型。', 503)

        def operation(control: RunControl) -> JSONValue:
            mapping = self.mapper.analyze(report.solution, template, binding.client, control)
            control.checkpoint('saving_mapping')
            self.repository.put('mapping', project_id, mapping.id, mapping)
            return {'mapping_id': mapping.id, 'needs_input': True}

        return self.jobs.submit(project_id, 'mapping', operation, {
            'solution_id': report.solution.id, 'template_id': template.source.id,
            'model_preset': binding.metadata() if needs_llm else None,
        })

    def confirm(self, project_id: str, mapping_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            mapping = workbook_mapping(self.repository.get('mapping', project_id, mapping_id))
            if integer(data.get('expected_revision')) != mapping.revision:
                raise ERPError('mapping_conflict', '映射已经更新，请重新打开后修改。', 409)
            report = self.context(project_id, mapping.solution_id)
            template = budget_template(self.repository.get('template', project_id, mapping.template_id))
            if template.source.sha256 != mapping.template_sha256:
                raise ERPError('template_changed', '模板内容已变化，请重新分析。', 409)
            entries = workbook_entries(data.get('entries'))
            issues = validate_mapping(template, entries, report.solution)
            if any(i.severity == 'error' for i in issues):
                raise ERPError('mapping_invalid', '映射不能保存：' + '；'.join(i.message for i in issues if i.severity == 'error'))
            saved = replace(mapping, entries=entries, issues=issues, confirmed=True, revision=mapping.revision + 1)
            self.repository.put('mapping', project_id, mapping.id, saved)
            return self.public_mapping(saved)

    def export(self, project_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            return self._export(project_id, data)

    def _export(self, project_id: str, data: JSONObject) -> JSONObject:
        report = self.context(project_id, string(data.get('solution_id'), 100))
        kind = string(data.get('kind'))
        if kind not in {'docx', 'xlsx'}:
            raise ERPError('invalid_artifact_kind', '导出格式应为 DOCX 或 XLSX。')
        self._check_active(project_id, 'artifact', report.solution.id)
        if any(i.severity == 'error' and i.code.startswith('plan.') for i in report.solution.evaluation.issues):
            raise ERPError('plan_invalid', '方案的周期或操作结构有错误，请先调整方案。', 409)
        mapping = None
        template = None
        content = None
        if kind == 'xlsx' and data.get('mapping_id'):
            mapping = workbook_mapping(self.repository.get('mapping', project_id, string(data['mapping_id'])))
            if mapping.solution_id != report.solution.id or not mapping.confirmed:
                raise ERPError('mapping_not_confirmed', '请先确认这份方案的预算表映射。', 409)
            template = budget_template(self.repository.get('template', project_id, mapping.template_id))
            if template.source.sha256 != mapping.template_sha256:
                raise ERPError('template_changed', '模板已变更，请重新分析。', 409)
            if any(i.severity == 'error' for i in validate_mapping(template, mapping.entries, report.solution)):
                raise ERPError('mapping_invalid', '映射检查未通过。', 409)
            original = self.repository.get('template_original', project_id, mapping.template_id)
            content = base64.b64decode(string(original['content'], 150 * 1024 * 1024))
        suffix = ('经营方案' if kind == 'docx' else
                  template.source.filename.rsplit('.', 1)[0] if template else '标准操作预算表')
        filename = suffix + ('_草稿' if not report.solution.evaluation.verified else '') + '.' + kind

        def operation(control: RunControl) -> JSONValue:
            control.checkpoint('rendering_report' if kind == 'docx' else 'filling_workbook')
            rendered = (self.report_renderer.render(report) if kind == 'docx'
                        else self.workbook_renderer.render(report, template, content, mapping))
            control.checkpoint('validating_artifact')
            manifest = ArtifactManifest(
                uuid4().hex, project_id, report.solution.id, report.solution.problem_version,
                kind, filename,
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document' if kind == 'docx'
                else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                hashlib.sha256(rendered.content).hexdigest(), len(rendered.content),
                datetime.now(timezone.utc).isoformat(), not report.solution.evaluation.verified,
                rendered.formula_status, rendered.warnings,
                template.source.id if template else None, mapping.id if mapping else None,
                mapping.revision if mapping else None,
            )
            control.checkpoint('saving_artifact')
            self.repository.put('artifact_file', project_id, manifest.id, {
                'content': base64.b64encode(rendered.content).decode(), 'sha256': manifest.sha256,
            })
            self.repository.put('artifact', project_id, manifest.id, manifest)
            return {'artifact_id': manifest.id}

        return self.jobs.submit(project_id, 'artifact', operation, {
            'solution_id': report.solution.id, 'kind': kind, 'mapping_id': mapping.id if mapping else None,
        })

    def _check_active(self, project_id: str, kind: str, solution_id: str,
                      template_id: str | None = None) -> None:
        for value in self.repository.list('job', project_id):
            job = obj(value)
            metadata = obj(job.get('metadata', {}))
            if (job.get('kind') == kind and job.get('status') in {'queued', 'running'}
                    and metadata.get('solution_id') == solution_id
                    and (template_id is None or metadata.get('template_id') == template_id)):
                raise ERPError('output_in_progress', '相关生成任务正在执行，请等待完成。', 409)

    def download(self, project_id: str, artifact_id: str) -> tuple[JSONObject, bytes]:
        manifest = self.repository.get('artifact', project_id, artifact_id)
        stored = self.repository.get('artifact_file', project_id, artifact_id)
        content = base64.b64decode(string(stored['content'], 150 * 1024 * 1024))
        if hashlib.sha256(content).hexdigest() != manifest['sha256']:
            raise ERPError('artifact_corrupted', '导出文件校验失败，请重新生成。', 500)
        return manifest, content
