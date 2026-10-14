from uuid import uuid4

from erp_agent.application.contracts import (
    TEXT,
    TEXTS,
    array,
    dumps,
    object_schema,
)
from erp_agent.application.legacy_workbook_mapper import legacy_mapping
from erp_agent.application.mapping_sources import mapping_material
from erp_agent.application.memory import RoleMemory
from erp_agent.application.ports import LLMClient, RunControl, TaskContext
from erp_agent.application.task_reporting import report_task
from erp_agent.application.workbook_mapping_tasks import WorkbookMappingTasks
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetTemplate,
    ProblemSnapshot,
    SolutionBundle,
    WorkbookEntry,
    WorkbookMapping,
)
from erp_agent.domain.workbook import output_sources, validate_mapping, writable_target

MAPPING_SCHEMA = object_schema(
    {
        "entries": {
            "type": "array",
            "items": object_schema(
                {
                    "sheet": TEXT,
                    "cell": TEXT,
                    "label": TEXT,
                    "source_ids": TEXTS,
                    "conversion": {
                        "type": "string",
                        "enum": [
                            "identity",
                            "sum",
                            "yuan_to_wan",
                            "wan_to_yuan",
                            "ratio_to_percent",
                        ],
                    },
                    "explanation": TEXT,
                }
            ),
        }
    }
)
MAPPING_BATCH_CHARS = 24000
MAPPING_VERSION = "mapper-5"


class WorkbookMapper:
    def __init__(self, memory: RoleMemory | None = None) -> None:
        self.memory = memory

    def analyze(
        self,
        solution: SolutionBundle,
        template: BudgetTemplate,
        llm: LLMClient,
        control: RunControl,
        snapshot: ProblemSnapshot | None = None,
    ) -> WorkbookMapping:
        planned = tuple(
            (index, item)
            for index, item in enumerate(solution.plan.budget_items)
            if item.template_id == template.source.id
        )
        if not planned:
            return legacy_mapping(
                solution, template, llm, control, snapshot, dumps(MAPPING_SCHEMA), MAPPING_VERSION
            )
        if snapshot:
            reading = next(
                (t for t in snapshot.template_readings if t.template_id == template.source.id), None
            )
            if reading and (not reading.reviewed or reading.sha256 != template.source.sha256):
                raise ERPError(
                    "template_not_reviewed", "空表与已核对版本不一致，请先在空表库核对。", 409
                )
        sheets = {sheet.name: sheet for sheet in template.sheets}
        cells = {
            sheet.name: {cell.coordinate: cell for cell in sheet.cells} for sheet in template.sheets
        }
        for _, item in planned:
            if item.sheet not in sheets or not writable_target(
                sheets[item.sheet], item.cell, cells[item.sheet]
            ):
                raise ERPError(
                    "mapping_readonly", "方案预算位置不是空白可写格，请返回经营方案修正。", 409
                )
        fixed = tuple(
            WorkbookEntry(
                item.sheet,
                item.cell,
                item.label,
                (f"budget:{index}",) if item.value is not None else (),
                explanation=item.explanation,
            )
            for index, item in planned
        )
        WorkbookMappingTasks.raise_errors(validate_mapping(template, fixed, solution, snapshot))
        task = TaskContext(
            "mapper:prepare", "Answerer", "沿用方案预算", template.source.filename, "mapper"
        )
        report_task(
            control,
            task,
            "succeeded",
            entry_count=len(fixed),
            fixed_count=sum(bool(entry.source_ids) for entry in fixed),
        )
        sources = output_sources(solution, snapshot)
        units = []
        for sheet in template.sheets:
            pending = tuple(
                (index, item)
                for index, item in planned
                if item.sheet == sheet.name and item.value is None and not item.gap_id
            )
            if not pending:
                continue
            material = mapping_material(solution, template, snapshot, pending, sources)
            if not array(material["sources"]):
                report_task(
                    control,
                    TaskContext(
                        f"mapper:{sheet.name}",
                        "Answerer",
                        "填写来源核对",
                        template.source.filename + " · " + sheet.name,
                        "mapper",
                    ),
                    "succeeded",
                    unresolved_count=len(pending),
                    detail="暂无可用的已有方案来源，保留空格与原规划说明。",
                )
                continue
            units.append(
                (
                    TaskContext(
                        f"mapper:{sheet.name}",
                        "Answerer",
                        "填写来源核对",
                        template.source.filename + " · " + sheet.name,
                        "mapper",
                    ),
                    material,
                )
            )
        findings, models = WorkbookMappingTasks(self.memory).run(
            units,
            solution,
            template,
            fixed,
            llm,
            control,
            snapshot,
            MAPPING_SCHEMA,
            MAPPING_VERSION,
            MAPPING_BATCH_CHARS,
        )
        by_target = {(entry.sheet, entry.cell): entry for entry in findings}
        entries = tuple(by_target.get((entry.sheet, entry.cell), entry) for entry in fixed)
        control.checkpoint("validating_mapping")
        issues = validate_mapping(template, entries, solution, snapshot)
        report_task(
            control,
            TaskContext(
                "mapper:validation",
                "Answerer",
                "核对预算与可写位置",
                template.source.filename,
                "mapper",
            ),
            "succeeded",
            entry_count=len(entries),
            unresolved_count=sum(not e.source_ids for e in entries),
        )
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
            prompt_version=MAPPING_VERSION,
        )
