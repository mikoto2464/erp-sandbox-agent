from dataclasses import replace
from importlib.resources import files
from uuid import uuid4

from erp_agent.application.budget_context import budget_material
from erp_agent.application.codec import workbook_entries
from erp_agent.application.contracts import JSONObject, TEXT, TEXTS, array, dumps, loads, obj, object_schema
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetTemplate, ProblemSnapshot, SolutionBundle, WorkbookEntry, WorkbookMapping,
)
from erp_agent.domain.workbook import output_sources, validate_mapping, writable_target

MAPPING_SCHEMA = object_schema({"entries": {"type": "array", "items": object_schema({
    "sheet": TEXT, "cell": TEXT, "label": TEXT, "source_ids": TEXTS,
    "conversion": {"type": "string", "enum": ["identity", "sum", "yuan_to_wan", "wan_to_yuan", "ratio_to_percent"]},
    "explanation": TEXT,
})}})
MAPPING_BATCH_CHARS = 80000


class WorkbookMapper:
    def analyze(self, solution: SolutionBundle, template: BudgetTemplate, llm: LLMClient,
                control: RunControl, snapshot: ProblemSnapshot | None = None) -> WorkbookMapping:
        material = budget_material((template,), include_formula_references=True)[0]
        batches = (template,)
        if len(dumps(material)) > MAPPING_BATCH_CHARS:
            batches = tuple(replace(template, sheets=(sheet,)) for sheet in template.sheets if not sheet.hidden)
        entries: tuple[WorkbookEntry, ...] = ()
        model = ""
        for index, batch in enumerate(batches):
            control.checkpoint("mapping_template", index)
            names = {s.name for s in batch.sheets}
            batch_material: JSONObject = {
                **material,
                "sheets": [s for value in array(material["sheets"])
                           for s in (obj(value),) if s["name"] in names],
            }
            batch_entries, model = self._map_batch(solution, batch, llm, control, snapshot,
                                                   tuple(s.name for s in template.sheets), batch_material)
            entries += batch_entries
        control.checkpoint("validating_mapping")
        issues = validate_mapping(template, entries, solution, snapshot)
        return WorkbookMapping(uuid4().hex, solution.project_id, solution.id, solution.problem_version,
                               template.source.id, template.source.sha256, entries, issues,
                               model=model, prompt_version="mapper-2")

    def _map_batch(self, solution: SolutionBundle, template: BudgetTemplate, llm: LLMClient,
                   control: RunControl, snapshot: ProblemSnapshot | None,
                   sheet_names: tuple[str, ...], material: JSONObject) -> tuple[tuple[WorkbookEntry, ...], str]:
        prompt = files("erp_agent").joinpath("prompts/mapper.txt").read_text(encoding="utf-8")
        schema = dumps(MAPPING_SCHEMA)
        original = {"template": material,
                    "workbook_sheet_names": sheet_names,
                    "sources": output_sources(solution, snapshot), "plan": solution.plan,
                    "problem": snapshot.reading.problem if snapshot else None,
                    "rules": snapshot.reading.rules if snapshot else None,
                    "evaluation": solution.evaluation}
        payload = original
        for attempt in range(2):
            response = None
            try:
                response = llm.generate(LLMRequest(prompt + "\nJSON schema:\n" + schema,
                                                   dumps(payload), "erp_workbook_mapping", schema))
                data = obj(loads(normalize_model_json(response.content)), {"entries"})
                entries = workbook_entries(data["entries"])
                sheets = {s.name: s for s in template.sheets}
                cells = {s.name: {c.coordinate: c for c in s.cells} for s in template.sheets}
                # 公式和已有数据不进入待填写清单；尝试覆盖它们仍须报错。
                entries = tuple(e for e in entries if e.source_ids or e.sheet not in sheets
                                or writable_target(sheets[e.sheet], e.cell, cells[e.sheet]))
                errors = [i for i in validate_mapping(template, entries, solution, snapshot)
                          if i.severity == "error"]
                if errors and not attempt:
                    control.checkpoint("repairing_mapping")
                    payload = {**original, "previous_output": response.content,
                               "errors": errors, "instruction": "修正映射位置与来源；只保留输入项，不能创造数值。"}
                    continue
                return entries, response.model
            except ERPError as exc:
                if response is None and exc.code != "llm_structured_response":
                    raise
                if attempt:
                    raise ERPError("mapping_output_invalid", "模型未返回有效表格映射。" + exc.message, 502) from exc
                control.checkpoint("repairing_mapping")
                payload = {**original, "previous_output": response.content if response else None,
                           "error": exc.message, "instruction": "只修正映射JSON，不能创造来源或数值。"}
        raise ERPError("mapping_output_invalid", "模型未返回有效表格映射。", 502)
