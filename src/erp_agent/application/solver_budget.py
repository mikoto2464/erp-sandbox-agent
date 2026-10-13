import hashlib
from dataclasses import replace
from importlib.resources import files

from erp_agent.application.contracts import (
    PROPOSAL_SCHEMA,
    JSONObject,
    array,
    decode_plan,
    dumps,
    loads,
    obj,
    to_json,
)
from erp_agent.application.llm_recovery import TRANSIENT_ERRORS, recovery_call
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.solver_context import problem_material
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.application.template_reader import row_material, template_material
from erp_agent.domain.budget_validation import cell_position, validate_budget_items
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import (
    BudgetTemplate,
    Plan,
    PlanningContext,
    ProblemSnapshot,
    SolveRequest,
    SolverMode,
)


class PlanBudgetPlanner:
    def __init__(self, llm: LLMClient, control: RunControl, memory: RoleMemory | None) -> None:
        self.llm, self.control, self.memory = llm, control, memory

    def plan(self, snapshot: ProblemSnapshot, context: PlanningContext, plan: Plan) -> Plan:
        prompt = files("erp_agent").joinpath("prompts/solver_budget.txt").read_text()
        budget_schema = obj(obj(obj(PROPOSAL_SCHEMA["properties"])["plan"])["properties"])[
            "budget_items"
        ]
        schema = dumps(
            {
                "type": "object",
                "properties": {"budget_items": budget_schema},
                "required": ["budget_items"],
                "additionalProperties": False,
            }
        )

        def extract(
            template: BudgetTemplate, material: JSONObject, task: TaskContext, depth: int = 0
        ) -> Plan:
            payload = {
                "problem_version": snapshot.version,
                "problem": problem_material(snapshot),
                "rules": to_json(snapshot.reading.rules),
                "generation": to_json(context.config),
                "plan": to_json(replace(plan, budget_items=())),
                "template": {**material, "source": to_json(template.source)},
                "template_readings": to_json(
                    tuple(
                        t for t in snapshot.template_readings if t.template_id == template.source.id
                    )
                ),
            }
            key = (
                "solver-budget:"
                + hashlib.sha256(
                    dumps({"payload": payload, "prompt": prompt, "schema": schema}).encode()
                ).hexdigest()
            )
            saved = self.memory.get(key) if self.memory else None
            self.control.checkpoint("planning_budget_values")
            raw = obj(material["sheet"])
            lo, hi = array(material.get("row_window", [1, raw["rows"]]))
            lo, hi = int(lo), int(hi)
            allowed_ranges = []
            for address, _ in map(array, array(raw["input_ranges"])):
                first, _, last = str(address).partition(":")
                allowed_ranges.append((cell_position(first), cell_position(last or first)))

            def decode(data: JSONObject) -> Plan:
                obj(data, {"budget_items"})
                result = decode_plan({**obj(to_json(plan)), "budget_items": data["budget_items"]})
                issues = validate_budget_items(
                    SolveRequest(
                        snapshot,
                        SolverMode.LLM_ONLY,
                        context=replace(context, templates=(template,)),
                    ),
                    result,
                )
                errors = [issue.message for issue in issues if issue.severity == "error"]
                if errors:
                    raise ERPError("budget_plan_invalid", "；".join(errors[:8]))
                for item in result.budget_items:
                    position = cell_position(item.cell)
                    if item.sheet != raw["name"] or not position or not lo <= position[0] <= hi:
                        raise ERPError("budget_plan_invalid", "预算条目超出本次工作表或行范围。")
                    if not any(
                        first
                        and last
                        and first[0] <= position[0] <= last[0]
                        and first[1] <= position[1] <= last[1]
                        for first, last in allowed_ranges
                    ):
                        raise ERPError("budget_plan_invalid", "预算条目不在本次空白输入区域。")
                return result

            if saved and "result" in saved:
                result = decode(obj(saved["result"]))
                report_task(
                    self.control, task, "cached", input_group_count=len(result.budget_items)
                )
                return result
            children = []
            if depth < 5 and hi - lo >= 8:
                middle = (lo + hi) // 2
                for first, last in ((lo, middle), (middle + 1, hi)):
                    part = row_material(material, first, last)
                    if array(obj(part["sheet"])["input_ranges"]):
                        children.append(part)

            def split() -> Plan:
                if self.memory:
                    self.memory.put(key, {"split": True})
                report_task(
                    self.control,
                    task,
                    "split",
                    stage="splitting_strategy",
                    child_count=len(children),
                )
                items = []
                for index, child in enumerate(children):
                    child_task = TaskContext(
                        f"{task.id}:{index}",
                        "Solver",
                        task.role,
                        f"{task.object} · 行 {child['row_window']}",
                        task.id,
                    )
                    report_task(self.control, child_task, "queued")
                    items.extend(extract(template, child, child_task, depth + 1).budget_items)
                result = decode({"budget_items": to_json(items)})
                if self.memory:
                    self.memory.put(key, {"result": {"budget_items": to_json(result.budget_items)}})
                report_task(self.control, task, "succeeded", input_group_count=len(items))
                return result

            if len(children) > 1 and (
                (saved and saved.get("split")) or len(dumps(payload)) > 28000
            ):
                return split()
            client = TaskLLMClient(self.llm, self.control, task)
            current = payload
            for attempt in range(2):
                response = None
                try:
                    request = LLMRequest(
                        prompt,
                        dumps(current),
                        "erp_solver_budget",
                        schema,
                        max_output_tokens=6000,
                        retries=0,
                        task=task,
                    )
                    if len(request.system) + len(request.user) + len(schema) > getattr(
                        self.llm, "max_input_chars", 100000
                    ):
                        raise ERPError("llm_input_limit", "预算输入超过模型容量。", 413)
                    report_task(
                        self.control,
                        task,
                        "running",
                        input_chars=len(request.user),
                        output_limit=6000,
                    )
                    response = recovery_call(client, request, self.control)
                    result = decode(obj(loads(normalize_model_json(response.content))))
                    self.control.checkpoint("saving_planning_progress")
                    if self.memory:
                        self.memory.put(
                            key, {"result": {"budget_items": to_json(result.budget_items)}}
                        )
                    report_task(
                        self.control,
                        task,
                        "succeeded",
                        model=response.model,
                        input_group_count=len(result.budget_items),
                    )
                    return result
                except Cancelled:
                    raise
                except ERPError as error:
                    if len(children) > 1 and error.code in TRANSIENT_ERRORS | {
                        "llm_input_limit",
                        "llm_incomplete",
                        "llm_truncated",
                    }:
                        return split()
                    if attempt == 0 and (
                        response is not None or error.code == "llm_structured_response"
                    ):
                        current = {
                            **payload,
                            "error": error.message,
                            "instruction": "只修复当前预算结构、范围及引用，不改变经营行动。",
                        }
                        continue
                    report_task(self.control, task, "failed", error=error.message)
                    raise ERPError(
                        error.code,
                        f"{task.object}：{error.message} 已保存完成的规划，重试可继续。",
                        error.status,
                    ) from error
            raise ERPError("budget_plan_invalid", "预算规划未完成。")

        tasks = []
        for index, template in enumerate(context.templates):
            for sheet_index, sheet in enumerate(template.sheets):
                material = template_material(template, sheet)
                if not array(obj(material["sheet"])["input_ranges"]):
                    continue
                task = TaskContext(
                    f"solver:values:{index}:{sheet_index}",
                    "Solver",
                    "逐表安排预算",
                    f"{template.source.filename} · {sheet.name}",
                    "solver",
                )
                report_task(self.control, task, "queued")
                tasks.append((replace(template, sheets=(sheet,)), material, task))
        items = []
        for template, material, task in tasks:
            items.extend(extract(template, material, task).budget_items)
        result = replace(plan, budget_items=tuple(items))
        issues = validate_budget_items(
            SolveRequest(snapshot, SolverMode.LLM_ONLY, context=context), result
        )
        if any(issue.severity == "error" for issue in issues):
            raise ERPError(
                "budget_plan_invalid", "；".join(i.message for i in issues if i.severity == "error")
            )
        return result
