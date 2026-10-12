from importlib.resources import files
from uuid import uuid4

from erp_agent.application.budget_context import budget_material
from erp_agent.application.codec import workbook_entries
from erp_agent.application.contracts import TEXT, TEXTS, dumps, loads, obj, object_schema
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import BudgetTemplate, SolutionBundle, WorkbookEntry, WorkbookMapping
from erp_agent.domain.workbook import output_sources, validate_mapping

MAPPING_SCHEMA = object_schema({"entries": {"type": "array", "items": object_schema({
    "sheet": TEXT, "cell": TEXT, "label": TEXT, "source_ids": TEXTS,
    "conversion": {"type": "string", "enum": ["identity", "sum", "yuan_to_wan", "wan_to_yuan", "ratio_to_percent"]},
    "explanation": TEXT,
})}})


class WorkbookMapper:
    def analyze(self, solution: SolutionBundle, template: BudgetTemplate, llm: LLMClient,
                control: RunControl) -> WorkbookMapping:
        control.checkpoint("mapping_template")
        existing = [(i, item) for i, item in enumerate(solution.plan.budget_items)
                    if item.template_id == template.source.id]
        model = ""
        if existing:
            entries = tuple(WorkbookEntry(item.sheet, item.cell, item.label,
                                          (f"budget:{i}",) if item.value is not None else (),
                                          explanation=item.explanation) for i, item in existing)
        else:
            prompt = files("erp_agent").joinpath("prompts/mapper.txt").read_text(encoding="utf-8")
            schema = dumps(MAPPING_SCHEMA)
            payload = {"template": budget_material((template,))[0],
                       "sources": output_sources(solution), "plan_summary": solution.plan.summary}
            original = payload
            for attempt in range(2):
                response = None
                try:
                    response = llm.generate(LLMRequest(prompt + "\nJSON schema:\n" + schema,
                                                       dumps(payload), "erp_workbook_mapping", schema))
                    data = obj(loads(normalize_model_json(response.content)), {"entries"})
                    entries = workbook_entries(data["entries"])
                    model = response.model
                    break
                except ERPError as exc:
                    if response is None and exc.code != "llm_structured_response":
                        raise
                    if attempt:
                        raise ERPError("mapping_output_invalid", "模型未返回有效表格映射。" + exc.message, 502) from exc
                    control.checkpoint("repairing_mapping")
                    payload = {**original, "previous_output": response.content if response else None,
                               "error": exc.message, "instruction": "只修正映射JSON，不能创造来源或数值。"}
        control.checkpoint("validating_mapping")
        issues = validate_mapping(template, entries, solution)
        return WorkbookMapping(uuid4().hex, solution.project_id, solution.id, solution.problem_version,
                               template.source.id, template.source.sha256, entries, issues, model=model)
