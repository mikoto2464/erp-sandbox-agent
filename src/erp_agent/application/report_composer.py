import hashlib
from collections import defaultdict
from dataclasses import replace
from importlib.resources import files

from langchain_core.runnables import RunnableLambda

from erp_agent.application.contracts import (
    TEXT,
    TEXTS,
    JSONObject,
    array,
    dumps,
    integer,
    loads,
    obj,
    object_schema,
    string,
    to_json,
)
from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.solver_context import problem_material
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import ReportData
from erp_agent.domain.reporting import ReportNote, ReportOutline, validate_report_outline

REPORT_VERSION = "answerer-1"
INDICES = {"type": "array", "items": {"type": "integer", "minimum": 0}}
NOTE_SCHEMA = object_schema(
    {
        "title": TEXT,
        "explanation": TEXT,
        "action_indices": INDICES,
        "fact_ids": TEXTS,
        "rule_ids": TEXTS,
        "budget_indices": INDICES,
        "period": {"type": ["integer", "null"], "minimum": 1},
        "condition": TEXT,
        "response": TEXT,
    }
)
REPORT_SCHEMA = object_schema(
    {
        name: {"type": "array", "items": NOTE_SCHEMA, "maxItems": 12}
        for name in ("strategy", "milestones", "risks")
    }
)


def decode_outline(data: JSONObject, report: ReportData) -> ReportOutline:
    data = obj(data, {"strategy", "milestones", "risks"})
    sections = {}
    for section in data:
        entries = array(data[section])
        if len(entries) > 12:
            raise ERPError("report_outline_invalid", "每个报告章节最多保留十二条关键说明。")
        notes = []
        for value in entries:
            entry = obj(value, set(NOTE_SCHEMA["properties"]))
            period = entry["period"]
            note = ReportNote(
                title=string(entry["title"], 160),
                explanation=string(entry["explanation"], 1200),
                action_indices=tuple(integer(i, 0) for i in array(entry["action_indices"])),
                fact_ids=tuple(string(i, 200) for i in array(entry["fact_ids"])),
                rule_ids=tuple(string(i, 200) for i in array(entry["rule_ids"])),
                budget_indices=tuple(integer(i, 0) for i in array(entry["budget_indices"])),
                period=None if period is None else integer(period, 1),
                condition=string(entry["condition"], 800),
                response=string(entry["response"], 800),
            )
            notes.append(note)
        sections[section] = tuple(notes)
    outline = ReportOutline(**sections)
    errors = validate_report_outline(
        outline,
        report.solution.plan,
        report.snapshot.reading.problem,
        report.snapshot.reading.rules,
    )
    if errors:
        raise ERPError("report_outline_invalid", "；".join(errors[:5]))
    return outline


class CollaborativeReportComposer:
    def __init__(self, memory: RoleMemory | None = None) -> None:
        self.memory = memory

    def compose(self, report: ReportData, llm: LLMClient, control: RunControl) -> ReportOutline:
        prompt = files("erp_agent").joinpath("prompts/answerer.txt").read_text(encoding="utf-8")
        schema = dumps(REPORT_SCHEMA)
        plan = report.solution.plan
        action_entities = {action.entity_id for action in plan.actions}
        problem = report.snapshot.reading.problem
        focused = replace(
            report.snapshot,
            reading=replace(
                report.snapshot.reading,
                problem=replace(
                    problem,
                    facts=tuple(
                        fact
                        for fact in problem.facts
                        if fact.category != "order_pool"
                        and (
                            fact.entity_id in action_entities
                            or fact.category in {"initial", "finance", "discount"}
                        )
                    ),
                ),
            ),
        )
        budget_groups: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        for item in plan.budget_items:
            group = budget_groups[(item.template_id, item.sheet)]
            group[0] += 1
            group[1] += int(item.value is None)
        material: JSONObject = {
            "solution_id": report.solution.id,
            "problem_version": report.solution.problem_version,
            "problem": problem_material(focused, order_details=False),
            "rules": to_json(report.snapshot.reading.rules),
            "generation": to_json(report.solution.generation),
            "plan": {
                "title": plan.title,
                "summary": plan.summary,
                "action_columns": [
                    "index",
                    "period",
                    "kind",
                    "entity_id",
                    "quantity",
                    "amount",
                    "description",
                ],
                "actions": [
                    [
                        i,
                        action.period,
                        action.kind,
                        action.entity_id,
                        action.quantity,
                        None if action.amount is None else str(action.amount),
                        action.description,
                    ]
                    for i, action in enumerate(plan.actions)
                ],
                "assumptions": list(plan.assumptions),
                "risks": list(plan.risks),
            },
            "budget_summary_columns": ["template_id", "sheet", "entry_count", "unresolved_count"],
            "budget_summary": [
                [tid, sheet, *counts] for (tid, sheet), counts in budget_groups.items()
            ],
            "evaluation": to_json(report.solution.evaluation),
        }
        roles = (
            (
                "strategy",
                "战略与关键时点",
                "围绕现有行动解释战略取舍及跨期前置条件，"
                "只填写 strategy 与 milestones，risks 为空。不改变启动/完成/交货时点。",
            ),
            (
                "execution",
                "执行与风险复核",
                "按营销、生产、人力、财务检查执行条件、现金与收官风险，"
                "只填写 risks，strategy 与 milestones 为空。不补写行动或预算。",
            ),
        )
        units = []
        for key, role, responsibility in roles:
            task = TaskContext("answerer:" + key, "Answerer", role, plan.title, "answerer")
            payload = {**material, "role": key, "responsibility": responsibility}
            request = LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps(payload),
                "erp_report_outline",
                schema,
                max_output_tokens=4000,
                reasoning_reserve_tokens=8192,
                stream=True,
                retries=0,
                task=task,
            )
            cache_key = (
                "report-unit:"
                + hashlib.sha256(
                    dumps(
                        {
                            "version": REPORT_VERSION,
                            "request": request,
                        }
                    ).encode()
                ).hexdigest()
            )
            units.append((task, request, cache_key))
            report_task(control, task, "queued")
        planner = getattr(llm, "plan_output", None) or getattr(llm, "plan_solver", None)
        pending = tuple(
            request for _, request, key in units if not self.memory or not self.memory.get(key)
        )
        if planner and pending:
            planner(pending, control, 2)

        def run(unit: tuple[TaskContext, LLMRequest, str]) -> ReportOutline:
            task, request, key = unit

            def decode(value: JSONObject) -> ReportOutline:
                result = decode_outline(value, report)
                if any(
                    note.budget_indices
                    for name in ("strategy", "milestones", "risks")
                    for note in getattr(result, name)
                ):
                    raise ERPError(
                        "report_role_invalid",
                        "逐格预算由程序完整插入，不在说明中引用未提供的条目。",
                    )
                if task.id.endswith("strategy"):
                    valid = bool(result.strategy) and bool(result.milestones) and not result.risks
                else:
                    valid = bool(result.risks) and not result.strategy and not result.milestones
                if not valid:
                    raise ERPError(
                        "report_role_invalid", "必须提供本角色的关键说明，其他职责章节留空。"
                    )
                if not report.solution.evaluation.verified and any(
                    marker in note.explanation
                    for name in ("strategy", "milestones", "risks")
                    for note in getattr(result, name)
                    for marker in ("已验证经营", "已验证可行", "已核算利润", "保证最优")
                ):
                    raise ERPError(
                        "report_verification_claim", "当前方案未经经营仿真，不能宣称已验证或最优。"
                    )
                return result

            cached = self.memory.get(key) if self.memory else None
            if cached:
                result = decode(obj(cached["result"]))
                report_task(control, task, "cached")
                return result
            client = TaskLLMClient(llm, control, task)
            original = obj(loads(request.user))
            for attempt in range(2):
                response = None
                try:
                    if (
                        len(request.system) + len(request.user) + len(schema)
                        > client.max_input_chars
                    ):
                        raise ERPError(
                            "report_input_limit",
                            "文档组织上下文超过模型容量，请选择容量更大的模型。",
                            413,
                        )
                    report_task(control, task, "running")
                    response = recovery_call(client, request, control)
                    result = decode(obj(loads(normalize_model_json(response.content))))
                    control.checkpoint("saving_report_progress")
                    if self.memory:
                        self.memory.put(key, {"result": obj(to_json(result))})
                    report_task(
                        control,
                        task,
                        "succeeded",
                        model=response.model,
                        note_count=sum(
                            len(getattr(result, name))
                            for name in ("strategy", "milestones", "risks")
                        ),
                    )
                    return result
                except Cancelled:
                    raise
                except ERPError as error:
                    if attempt == 0 and (
                        response is not None or error.code == "llm_structured_response"
                    ):
                        request = replace(
                            request,
                            user=dumps(
                                {
                                    **original,
                                    "error": error.message,
                                    "instruction": "只修复结构和引用，数值及期间交由引用字段展示，"
                                    "不扩写或改变方案。",
                                }
                            ),
                        )
                        continue
                    report_task(control, task, "failed", error=error.message)
                    raise ERPError(
                        error.code,
                        f"{task.role}：{error.message} 已保存完成的报告任务，可重试继续。",
                        error.status,
                    ) from error
            raise ERPError("report_output_invalid", "未取得有效报告说明。")

        results = RunnableLambda(run).batch(
            units, config={"max_concurrency": 2}, return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                raise result
        control.checkpoint("assembling_report")
        outlines = [result for result in results if isinstance(result, ReportOutline)]
        return ReportOutline(
            **{
                name: tuple(note for outline in outlines for note in getattr(outline, name))
                for name in ("strategy", "milestones", "risks")
            }
        )
