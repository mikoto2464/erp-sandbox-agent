from importlib.resources import files

from langchain_core.runnables import RunnableLambda

from erp_agent.application.budget_context import budget_material
from erp_agent.application.contracts import (
    ISSUE_SCHEMA,
    TEXT,
    array,
    dumps,
    loads,
    obj,
    object_schema,
    string,
)
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetSheet,
    BudgetTemplate,
    Issue,
    ReadingResult,
    TemplateInputGroup,
    TemplateReading,
    TemplateSheetReading,
)

GROUP_SCHEMA = object_schema({"label": TEXT, "range": TEXT, "unit": TEXT, "description": TEXT})
SHEET_SCHEMA = object_schema(
    {
        "purpose": TEXT,
        "periods": TEXT,
        "input_groups": {"type": "array", "items": GROUP_SCHEMA},
        "issues": {"type": "array", "items": ISSUE_SCHEMA},
    }
)


class TemplateReader:
    def read(
        self, template: BudgetTemplate, client: LLMClient, control: RunControl,
        reading: ReadingResult | None = None,
    ) -> TemplateReading:
        prompt = files("erp_agent").joinpath("prompts/reader_template.txt").read_text()
        schema = dumps(SHEET_SCHEMA)

        def task_for(sheet: BudgetSheet) -> TaskContext:
            return TaskContext(
                "template-read:" + template.source.id + ":" + sheet.name,
                "Reader",
                "空表理解",
                template.source.filename + " · " + sheet.name,
                "reader",
            )

        for sheet in template.sheets:
            report_task(control, task_for(sheet), "queued")

        def extract(sheet: BudgetSheet) -> tuple[TemplateSheetReading, tuple[Issue, ...]]:
            task = task_for(sheet)
            material = {
                "template_id": template.source.id,
                "template": budget_material((BudgetTemplate(template.source, (sheet,)),))[0],
                "current_problem_context": {
                    "title": reading.problem.title,
                    "years": reading.problem.years,
                    "periods_per_year": reading.problem.periods_per_year,
                    "rule_version": reading.rules.version,
                } if reading else None,
            }
            bound = getattr(client, "max_input_chars", 100_000)
            if len(prompt) + len(schema) + len(dumps(material)) + 2500 > bound:
                report_task(control, task, "failed", error="空表工作表超过模型输入上限。")
                raise ERPError("template_input_limit", "空表工作表超过模型输入上限，请精简工作表。")
            scoped = TaskLLMClient(client, control, task)
            for attempt in range(2):
                response = scoped.generate(
                    LLMRequest(
                        prompt + "\nJSON schema:\n" + schema,
                        dumps(material),
                        "erp_template_reading",
                        schema,
                        max_output_tokens=6000,
                        retries=0,
                    )
                )
                try:
                    data = obj(
                        loads(normalize_model_json(response.content)),
                        {"purpose", "periods", "input_groups", "issues"},
                    )
                    groups = []
                    for value in array(data["input_groups"]):
                        entry = obj(value, {"label", "range", "unit", "description"})
                        target = string(entry["range"], 50)
                        first, _, last = target.partition(":")
                        start, end = cell_position(first), cell_position(last or first)
                        if (
                            not start
                            or not end
                            or not 1 <= start[0] <= end[0] <= sheet.rows
                            or not 1 <= start[1] <= end[1] <= sheet.columns
                        ):
                            raise ERPError("template_group_range", "空表理解引用了无效范围。")
                        groups.append(
                            TemplateInputGroup(
                                string(entry["label"]),
                                target,
                                string(entry["unit"]),
                                string(entry["description"]),
                            )
                        )
                    issues = []
                    for value in array(data["issues"]):
                        entry = obj(
                            value, {"code", "severity", "target", "message", "evidence_ids"}
                        )
                        severity = string(entry["severity"])
                        if severity not in {"error", "warning", "info"}:
                            raise ERPError("invalid_contract", "空表疑点等级无效。")
                        issues.append(
                            Issue(
                                "template." + string(entry["code"]),
                                severity,
                                template.source.id,
                                sheet.name + "：" + string(entry["message"]),
                            )
                        )
                    result = TemplateSheetReading(
                        sheet.name, string(data["purpose"]), string(data["periods"]), tuple(groups)
                    )
                    report_task(
                        control,
                        task,
                        "needs_input" if issues else "succeeded",
                        model=response.model,
                        input_group_count=len(groups),
                    )
                    return result, tuple(issues)
                except ERPError as error:
                    if attempt:
                        report_task(control, task, "failed", error=error.message)
                        raise
                    material["error"] = error.message
                    material["instruction"] = "只修正本表结构输出，不编造数值。"
            raise ERPError("template_reading_invalid", "空表理解失败。")

        results = RunnableLambda(extract).batch(
            list(template.sheets), config={"max_concurrency": 3}
        )
        return TemplateReading(
            template.source.id,
            template.source.sha256,
            tuple(result[0] for result in results),
            tuple(issue for result in results for issue in result[1]),
        )
