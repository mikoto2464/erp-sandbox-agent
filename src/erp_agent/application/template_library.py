import base64
import hashlib
import threading
from dataclasses import replace
from uuid import uuid4

from erp_agent.application.codec import budget_template, template_reading
from erp_agent.application.contracts import (
    JSONObject,
    JSONValue,
    array,
    obj,
    string,
    strings,
    to_json,
)
from erp_agent.application.ports import (
    BudgetTemplateParser,
    JobRunner,
    LLMClient,
    ModelSettings,
    Repository,
    RunControl,
)
from erp_agent.application.template_reader import (
    LIBRARY_SCOPE,
    TEMPLATE_READER_VERSION,
    TemplateReader,
)
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetTemplate,
    Issue,
    SourceDocument,
    TemplateIssueReview,
    TemplateReading,
)


class TemplateLibrary:
    def __init__(
        self,
        repository: Repository,
        jobs: JobRunner,
        parser: BudgetTemplateParser | None,
        client: LLMClient,
        models: ModelSettings | None,
    ) -> None:
        self.repository, self.jobs, self.parser = repository, jobs, parser
        self.client, self.models = client, models
        self.lock = threading.RLock()

    def _active(self, template_id: str) -> None:
        if self.repository.upload_deleted(LIBRARY_SCOPE, "template", template_id):
            raise ERPError("template_deleted", "空表已从模板库删除。", 404)

    def items(self) -> list[JSONObject]:
        result = []
        for original in self.repository.list("template_original", LIBRARY_SCOPE):
            source = obj(obj(original)["source"])
            tid = string(source["id"])
            if self.repository.upload_deleted(LIBRARY_SCOPE, "template", tid):
                continue
            item: JSONObject = {"id": tid, "source": source, "reading": None, "parsed": False}
            try:
                template = self.repository.get("template", LIBRARY_SCOPE, tid)
                item["parsed"] = True
                item["sheet_names"] = [obj(s)["name"] for s in array(template["sheets"])]
                item["reading"] = self.repository.get("template_reading", LIBRARY_SCOPE, tid)
            except ERPError as error:
                if error.code != "not_found":
                    raise
            result.append(item)
        return result

    def detail(self, template_id: str) -> JSONObject:
        self._active(template_id)
        template = self.repository.get("template", LIBRARY_SCOPE, template_id)
        if template.get("parser_version") != "budget-3" and self.parser:
            original = self.repository.get("template_original", LIBRARY_SCOPE, template_id)
            source = budget_template(template).source
            template = obj(
                to_json(
                    self.parser.parse(
                        source, base64.b64decode(string(original["content"], 150 * 1024 * 1024))
                    )
                )
            )
            self.repository.put("template", LIBRARY_SCOPE, template_id, template)
        try:
            reading = self.repository.get("template_reading", LIBRARY_SCOPE, template_id)
        except ERPError as error:
            if error.code != "not_found":
                raise
            reading = None
        return {"template": template, "reading": reading}

    def upload(self, filename: str, content: bytes) -> JSONObject:
        if not filename or len(filename) > 200 or any(c in filename for c in "/\\\x00"):
            raise ERPError("invalid_filename", "文件名无效。")
        fmt = filename.rsplit(".", 1)[-1].lower()
        if fmt not in {"xlsx", "xls"}:
            raise ERPError("unsupported_template", "预算空表支持 XLSX 或 XLS。")
        if not self.parser:
            raise ERPError("template_parser_unavailable", "预算表解析器未配置。", 503)
        digest = hashlib.sha256(content).hexdigest()
        # 删除后重传是新的库记录，已导入项目仍绑定原副本。

        tid = digest[:32]
        with self.lock:
            if self.repository.upload_deleted(LIBRARY_SCOPE, "template", tid):
                tid = uuid4().hex
            existing = next((t for t in self.items() if obj(t["source"])["sha256"] == digest), None)
            if existing:
                return {"template_id": existing["id"], "reused": True}
            source = SourceDocument(tid, filename, digest, len(content), fmt)
            self.repository.put(
                "template_original",
                LIBRARY_SCOPE,
                tid,
                {"source": to_json(source), "content": base64.b64encode(content).decode()},
            )
            return self._parse(source, content)

    def _parse(self, source: SourceDocument, content: bytes) -> JSONObject:
        def operation(control: RunControl) -> JSONValue:
            control.checkpoint("parsing_template")
            self._active(source.id)
            if not self.parser:
                raise ERPError("template_parser_unavailable", "预算表解析器未配置。", 503)
            template = self.parser.parse(source, content)
            control.checkpoint("saving_template")
            self._active(source.id)
            self.repository.put("template", LIBRARY_SCOPE, source.id, template)
            return {"template_id": source.id}

        return self.jobs.submit(
            LIBRARY_SCOPE,
            "template",
            operation,
            {"filename": source.filename, "template_id": source.id},
        )

    def read(self, template_id: str) -> JSONObject:
        with self.lock:
            self._active(template_id)
            if any(
                obj(j).get("status") in {"running", "queued"}
                and obj(obj(j).get("metadata", {})).get("template_id") == template_id
                for j in self.repository.list("job", LIBRARY_SCOPE)
            ):
                raise ERPError("template_busy", "这份空表正在处理，请等待完成。", 409)
            try:
                template = budget_template(obj(self.detail(template_id)["template"]))
            except ERPError as error:
                if error.code != "not_found":
                    raise
                original = self.repository.get("template_original", LIBRARY_SCOPE, template_id)
                source = obj(original["source"])
                return self._parse(
                    SourceDocument(
                        string(source["id"]),
                        string(source["filename"]),
                        string(source["sha256"]),
                        int(source["size"]),
                        string(source["format"]),
                    ),
                    base64.b64decode(string(original["content"], 150 * 1024 * 1024)),
                )
            try:
                existing = template_reading(
                    self.repository.get("template_reading", LIBRARY_SCOPE, template_id)
                )
                if (
                    existing.sha256 == template.source.sha256
                    and existing.prompt_version == TEMPLATE_READER_VERSION
                ):
                    return {"template_id": template_id, "reused": True}
            except ERPError as error:
                if error.code != "not_found":
                    raise
            binding = self.models.capture_pool() if self.models else None
            client = binding.client if binding else self.client
            if not client.configured:
                raise ERPError("llm_not_configured", "请先配置模型。", 503)

            def operation(control: RunControl) -> JSONValue:
                reading = TemplateReader(self.repository).read(template, client, control)
                control.checkpoint("saving_template_reading")
                self._active(template_id)
                self.repository.put("template_reading", LIBRARY_SCOPE, template_id, reading)
                return {"template_id": template_id, "needs_input": True}

            return self.jobs.submit(
                LIBRARY_SCOPE,
                "read",
                operation,
                {
                    "template_id": template_id,
                    "filename": template.source.filename,
                    "model_preset": binding.metadata() if binding else None,
                },
            )

    def review(self, template_id: str, data: JSONObject) -> JSONObject:
        with self.lock:
            self._active(template_id)
            reading = template_reading(
                self.repository.get("template_reading", LIBRARY_SCOPE, template_id)
            )
            if "issue" in data:
                entry = obj(data["issue"], {
                    "code", "severity", "target", "message", "evidence_ids",
                })
                issue = Issue(
                    string(entry["code"]), string(entry["severity"]), string(entry["target"]),
                    string(entry["message"]), strings(entry.get("evidence_ids", [])),
                )
                if issue not in reading.issues or issue.severity == "info":
                    raise ERPError(
                        "template_review_issue", "疑点已变更，请刷新空表解读后处理。", 409
                    )
                answer = string(data.get("answer", ""), maximum=5000).strip()
                if not answer:
                    raise ERPError("template_review_answer", "请填写对这条疑点的回答后确认。")
                reviews = tuple(r for r in reading.issue_reviews if r.issue != issue)
                saved = replace(reading, reviewed=False,
                                issue_reviews=reviews + (TemplateIssueReview(issue, answer),))
            else:
                reviewed = data.get("reviewed") is True
                answered = {r.issue for r in reading.issue_reviews if r.answer.strip()}
                unresolved = [
                    i for i in reading.issues if i.severity != "info" and i not in answered
                ]
                if reviewed and unresolved:
                    raise ERPError(
                        "template_review_answer",
                        f"还有 {len(unresolved)} 条疑点未逐条确认，"
                        "请分别回答后再确认空表。",
                    )
                saved = replace(
                    reading, reviewed=reviewed,
                    explanation=string(data.get("explanation", reading.explanation)).strip(),
                )
            self.repository.put("template_reading", LIBRARY_SCOPE, template_id, saved)
            return obj(to_json(saved))

    def publish(
        self, template: BudgetTemplate, original: JSONObject, reading: TemplateReading | None
    ) -> None:
        with self.lock:
            existing = next(
                (i for i in self.items() if obj(i["source"])["sha256"] == template.source.sha256),
                None,
            )
            tid = string(existing["id"]) if existing else template.source.sha256[:32]
            if self.repository.upload_deleted(LIBRARY_SCOPE, "template", tid):
                return
            if existing and existing.get("parsed"):
                return
            source = replace(template.source, id=tid)
            self.repository.put(
                "template_original", LIBRARY_SCOPE, tid, {**original, "source": to_json(source)}
            )
            self.repository.put("template", LIBRARY_SCOPE, tid, replace(template, source=source))
            # 库已确认的口径不被其他项目的解释静默覆盖。
            if reading is not None and (
                not existing
                or not existing.get("reading")
                or not obj(existing["reading"]).get("reviewed")
            ):
                self.repository.put(
                    "template_reading",
                    LIBRARY_SCOPE,
                    tid,
                    replace(
                        reading,
                        template_id=tid,
                        issues=tuple(replace(i, target=tid) for i in reading.issues),
                        issue_reviews=tuple(
                            replace(r, issue=replace(r.issue, target=tid))
                            for r in reading.issue_reviews
                        ),
                    ),
                )

    def delete(self, template_id: str) -> JSONObject:
        with self.lock:
            self._active(template_id)
            for value in self.repository.list("job", LIBRARY_SCOPE):
                job = obj(value)
                if obj(job.get("metadata", {})).get("template_id") == template_id:
                    self.jobs.cancel(LIBRARY_SCOPE, string(job["id"]))
            return self.repository.delete_upload(LIBRARY_SCOPE, "template", template_id)
