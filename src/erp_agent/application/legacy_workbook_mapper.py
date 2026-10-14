from dataclasses import replace
from importlib.resources import files
from uuid import uuid4

from erp_agent.application.codec import workbook_entries
from erp_agent.application.contracts import dumps, loads, obj
from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import BudgetTemplate, ProblemSnapshot, SolutionBundle, WorkbookMapping
from erp_agent.domain.workbook import output_sources, validate_mapping, writable_target


def legacy_mapping(
    solution: SolutionBundle,
    template: BudgetTemplate,
    llm: LLMClient,
    control: RunControl,
    snapshot: ProblemSnapshot | None,
    schema: str,
    prompt_version: str,
) -> WorkbookMapping:
    # 仅兼容未绑定预算的历史调用；正式 API 已要求先生成模板预算。
    if snapshot and any(t.template_id == template.source.id for t in snapshot.template_readings):
        raise ERPError(
            "template_plan_missing",
            "已核对空表须先纳入经营方案预算，不能在填表时重新规划。",
            409,
        )
    prompt = files("erp_agent").joinpath("prompts/mapper.txt").read_text(encoding="utf-8")
    entries = ()
    models = []
    for index, sheet in enumerate(template.sheets):
        if sheet.hidden:
            continue
        material = {
            "legacy": True,
            "sources": output_sources(solution, snapshot),
            "template": {
                "sheets": [
                    {
                        "name": sheet.name,
                        "rows": sheet.rows,
                        "columns": sheet.columns,
                        "labels": [
                            [c.coordinate, c.text] for c in sheet.cells if c.text and not c.formula
                        ],
                    }
                ]
            },
        }
        task = TaskContext(
            f"mapper:legacy:{index}",
            "Answerer",
            "历史填写来源对应",
            template.source.filename + " · " + sheet.name,
            "mapper",
        )
        response = recovery_call(
            TaskLLMClient(llm, control, task),
            LLMRequest(
                prompt,
                dumps(material),
                "erp_workbook_mapping",
                schema,
                max_output_tokens=4000,
                retries=0,
                task=task,
                reasoning_reserve_tokens=8192,
                stream=True,
            ),
            control,
        )
        batch = workbook_entries(
            obj(loads(normalize_model_json(response.content)), {"entries"})["entries"]
        )
        aliases = {
            f"action:{i}:{field}": f"action:{a.action_id}:{field}"
            for i, a in enumerate(solution.plan.actions)
            for field in ("amount", "quantity", "description")
        }
        batch = tuple(
            replace(entry, source_ids=tuple(aliases.get(ref, ref) for ref in entry.source_ids))
            for entry in batch
        )
        cell_index = {c.coordinate: c for c in sheet.cells}
        entries += tuple(
            entry
            for entry in batch
            if entry.source_ids
            or entry.sheet != sheet.name
            or writable_target(sheet, entry.cell, cell_index)
        )
        models.append(response.model)
        report_task(control, task, "succeeded", entry_count=len(batch), model=response.model)
    issues = validate_mapping(template, entries, solution, snapshot)
    return WorkbookMapping(
        uuid4().hex,
        solution.project_id,
        solution.id,
        solution.problem_version,
        template.source.id,
        template.source.sha256,
        entries,
        issues,
        model="、".join(dict.fromkeys(models)),
        prompt_version=prompt_version,
    )
