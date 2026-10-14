import hashlib
from dataclasses import replace
from difflib import get_close_matches
from importlib.resources import files

from langchain_core.runnables import RunnableLambda

from erp_agent.application.contracts import (
    TEXT,
    JSONObject,
    array,
    decode_proposal,
    dumps,
    loads,
    obj,
    object_schema,
    string,
    to_json,
)
from erp_agent.application.llm_recovery import TRANSIENT_ERRORS, recovery_call
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.solver_budget import PlanBudgetPlanner
from erp_agent.application.solver_context import (
    planning_templates,
    problem_material,
    selected_snapshot,
)
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.application.template_reader import row_material
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import PlanningContext, ProblemSnapshot, StrategyProposal

DEFAULT_PLANNING_CONTEXT = PlanningContext()


class CollaborativeStrategyGenerator:
    def __init__(
        self, llm: LLMClient, control: RunControl, memory: RoleMemory | None = None
    ) -> None:
        self.llm, self.control, self.memory = llm, control, memory

    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_PLANNING_CONTEXT
    ) -> StrategyProposal:
        from erp_agent.application.solver import DraftPlanEvaluator, LLMStrategyGenerator

        schema = dumps(
            object_schema(
                {
                    "recommendations": {
                        "type": "array",
                        "items": object_schema(
                            {
                                "entity_id": TEXT,
                                "periods": TEXT,
                                "recommendation": TEXT,
                                "fact_ids": {"type": "array", "items": TEXT},
                                "rule_ids": {"type": "array", "items": TEXT},
                            }
                        ),
                    },
                    "dependencies": {"type": "array", "items": TEXT},
                    "risks": {"type": "array", "items": TEXT},
                }
            )
        )
        roles = [
            (
                "operations",
                "产销与资源规划",
                "从可履约订单窗口倒推市场、认证、研发、安装、培训、采购及生产启动；"
                "明确产能材料人力约束、订单取舍与各年度阶段，区分待竞目标和已签合同。",
            ),
            (
                "finance",
                "资金与风险规划",
                "沿经营动作的实际支付顺序安排资金，区分销售、应收与回款；"
                "核对融资成本及到期、固定费用、商誉、碳和完整末期收官风险。",
            ),
        ]
        if context.templates:
            roles.append(("budget", "模板预算规划", "按已核对空表识别输入项目与经营动作依赖。"))
        entities = {f.entity_id for f in snapshot.reading.problem.facts} | {"company"}
        fact_ids = {f.id for f in snapshot.reading.problem.facts}
        rule_ids = {r.id for r in snapshot.reading.rules.rules}
        prompt = files("erp_agent").joinpath("prompts/solver_specialist.txt").read_text()

        synthesis_task = TaskContext(
            "solver:synthesis",
            "Solver",
            "统筹逐期经营行动",
            snapshot.reading.problem.title,
            "solver",
        )

        def task_for(role: tuple[str, str, str]) -> TaskContext:
            return TaskContext(
                "solver:" + role[0], "Solver", role[1], snapshot.reading.problem.title, "solver"
            )

        snapshot = selected_snapshot(snapshot, context.templates)
        budget_roots = planning_templates(context.templates)
        units: list[tuple[TaskContext, JSONObject]] = []
        for key, name, responsibility in roles:
            base: JSONObject = {
                "role": key,
                "responsibility": responsibility,
                "problem_version": snapshot.version,
                "problem": problem_material(snapshot, order_details=key != "budget"),
                "rules": to_json(snapshot.reading.rules),
                "issues": to_json(snapshot.reading.issues),
                "generation": to_json(context.config),
                "previous_plan": to_json(
                    replace(context.previous_plan, budget_items=())
                    if context.previous_plan
                    else None
                ),
            }
            if key == "budget":
                for index, template in enumerate(budget_roots):
                    tid = obj(template["source"])["id"]
                    material = {
                        **base,
                        "budget_templates": [template],
                        "template_readings": to_json(
                            tuple(t for t in snapshot.template_readings if t.template_id == tid)
                        ),
                    }
                    units.append(
                        (
                            TaskContext(
                                f"solver:budget:{index}",
                                "Solver",
                                name,
                                str(obj(template["source"])["filename"]),
                                "solver:budget",
                            ),
                            material,
                        )
                    )
            else:
                units.append((task_for((key, name, responsibility)), base))
        report_task(self.control, synthesis_task, "queued")

        def request_for(task: TaskContext, material: JSONObject) -> LLMRequest:
            return LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps(material),
                "erp_solver_specialist",
                schema,
                max_output_tokens=4000,
                reasoning_reserve_tokens=8192,
                stream=True,
                retries=0,
                task=task,
            )

        def unit_key(material: JSONObject) -> str:
            return (
                "solver-unit:"
                + hashlib.sha256(
                    dumps(
                        {
                            "version": "solver-8",
                            "material": material,
                            "prompt": prompt,
                            "schema": schema,
                        }
                    ).encode()
                ).hexdigest()
            )

        def decode(value: JSONObject) -> JSONObject:
            data = obj(value, {"recommendations", "dependencies", "risks"})
            for value in array(data["recommendations"]):
                entry = obj(
                    value, {"entity_id", "periods", "recommendation", "fact_ids", "rule_ids"}
                )
                for label, used, allowed in (
                    ("实体", {string(entry["entity_id"])}, entities),
                    ("字段", {string(v) for v in array(entry["fact_ids"])}, fact_ids),
                    ("规则", {string(v) for v in array(entry["rule_ids"])}, rule_ids),
                ):
                    unknown = sorted(used - allowed)
                    if unknown:
                        hints = [
                            f"{value}（相近合法ID："
                            f"{', '.join(get_close_matches(value, sorted(allowed), n=2))}）"
                            for value in unknown[:3]
                        ]
                        raise ERPError(
                            "solver_source",
                            f"协作建议引用未知{label}："
                            + "；".join(hints)
                            + "。请核对输入，只用原ID或删除无依据引用。",
                        )
                string(entry["periods"])
                string(entry["recommendation"])
            for value in array(data["dependencies"]) + array(data["risks"]):
                string(value)
            return data

        def split_budget(material: JSONObject) -> tuple[JSONObject, ...]:
            templates = array(material.get("budget_templates", []))
            if len(templates) != 1:
                return ()
            template = obj(templates[0])
            sheets = array(template["sheets"])
            if len(sheets) > 1:
                parts = [[sheet] for sheet in sheets]
            elif sheets:
                sheet = obj(sheets[0])
                lo, hi = array(material.get("row_window", [1, sheet["rows"]]))
                lo, hi = int(lo), int(hi)
                if hi - lo < 8 or len(array(sheet["input_ranges"])) < 2:
                    return ()
                middle = (lo + hi) // 2
                parts = []
                for first, last in ((lo, middle), (middle + 1, hi)):
                    sliced = row_material({"sheet": sheet}, first, last)
                    if array(obj(sliced["sheet"])["input_ranges"]):
                        parts.append([sliced["sheet"]])
                if len(parts) < 2:
                    return ()
                return tuple(
                    {
                        **material,
                        "budget_templates": [{**template, "sheets": part}],
                        "row_window": [first, last],
                    }
                    for part, (first, last) in zip(
                        parts, ((lo, middle), (middle + 1, hi)), strict=True
                    )
                )
            else:
                return ()
            results = []
            for part in parts:
                names = {obj(sheet)["name"] for sheet in part}
                readings = [
                    {
                        **obj(reading),
                        "sheets": [
                            sheet
                            for sheet in array(obj(reading)["sheets"])
                            if obj(sheet)["name"] in names
                        ],
                    }
                    for reading in array(material.get("template_readings", []))
                ]
                results.append(
                    {
                        **material,
                        "budget_templates": [{**template, "sheets": part}],
                        "template_readings": readings,
                    }
                )
            return tuple(results)

        def run(unit: tuple[TaskContext, JSONObject], depth: int = 0) -> JSONObject:
            self.control.checkpoint("planning_collaboration")
            task, material = unit
            request = request_for(task, material)
            key = unit_key(material)
            saved = self.memory.get(key) if self.memory else None
            if saved and "result" in saved:
                data = decode(obj(saved["result"]))
                report_task(
                    self.control,
                    task,
                    "cached",
                    recommendation_count=len(array(data["recommendations"])),
                )
                return {"role": task.role, "object": task.object, **data}
            children = split_budget(material) if depth < 4 else ()

            def split() -> JSONObject:
                if self.memory:
                    self.memory.put(key, {"split": True})
                report_task(
                    self.control,
                    task,
                    "split",
                    stage="splitting_strategy",
                    child_count=len(children),
                )
                findings = []
                for index, child in enumerate(children):
                    child_task = TaskContext(
                        f"{task.id}:{index}",
                        "Solver",
                        task.role,
                        task.object
                        + " · "
                        + " / ".join(
                            str(obj(s)["name"])
                            for t in array(child["budget_templates"])
                            for s in array(obj(t)["sheets"])
                        )
                        + (f" · 行 {child['row_window']}" if "row_window" in child else ""),
                        task.id,
                    )
                    report_task(self.control, child_task, "queued")
                    findings.append(run((child_task, child), depth + 1))
                data = {
                    name: [value for finding in findings for value in array(finding[name])]
                    for name in ("recommendations", "dependencies", "risks")
                }
                if self.memory:
                    self.memory.put(key, {"result": data})
                report_task(
                    self.control,
                    task,
                    "succeeded",
                    recommendation_count=len(array(data["recommendations"])),
                )
                return {"role": task.role, "object": task.object, **data}

            if children and (
                (saved and saved.get("split"))
                or len(dumps(material.get("budget_templates", []))) > 24000
            ):
                return split()
            client = TaskLLMClient(self.llm, self.control, task)
            payload = material
            for attempt in range(2):
                self.control.checkpoint("planning_" + str(material["role"]))
                response = None
                try:
                    request = request_for(task, payload)
                    if len(request.system) + len(request.user) + len(schema) > getattr(
                        self.llm, "max_input_chars", 100000
                    ):
                        raise ERPError("llm_input_limit", "规划任务输入超过模型容量。", 413)
                    report_task(
                        self.control,
                        task,
                        "running",
                        input_chars=len(request.user),
                        output_limit=4000,
                    )
                    response = recovery_call(client, request, self.control)
                    data = decode(obj(loads(normalize_model_json(response.content))))
                    self.control.checkpoint("saving_planning_progress")
                    if self.memory:
                        self.memory.put(key, {"result": data})
                    report_task(
                        self.control,
                        task,
                        "succeeded",
                        model=response.model,
                        recommendation_count=len(array(data["recommendations"])),
                    )
                    return {"role": task.role, "object": task.object, **data}
                except Cancelled:
                    raise
                except ERPError as error:
                    if children and error.code in TRANSIENT_ERRORS | {
                        "llm_input_limit",
                        "llm_incomplete",
                        "llm_truncated",
                    }:
                        return split()
                    if attempt == 0 and (
                        response is not None or error.code == "llm_structured_response"
                    ):
                        payload = {
                            **material,
                            "error": error.message,
                            "instruction": "仅修复结构或引用，保留本任务职责及依据，不扩写方案。",
                        }
                        continue
                    report_task(self.control, task, "failed", error=error.message)
                    raise ERPError(
                        error.code,
                        f"{task.object} · {task.role}：{error.message}"
                        " 已保存完成的协作任务，重试可继续未完成部分。",
                        error.status,
                    ) from error
            raise ERPError("solver_specialist_invalid", "协作规划失败。")

        requests = tuple(request_for(task, material) for task, material in units)
        for task, _ in units:
            report_task(self.control, task, "queued")
        planner = getattr(self.llm, "plan_solver", None)
        if planner:
            unfinished = tuple(
                request
                for request, (_, material) in zip(requests, units, strict=True)
                if not self.memory or not (self.memory.get(unit_key(material)) or {}).get("result")
            )
            unfinished = tuple(
                r
                for r in unfinished
                if len(r.system) + len(r.user) + len(r.schema_json)
                <= getattr(self.llm, "max_input_chars", 100000)
            )
            planner(unfinished, self.control, 2)
        results = RunnableLambda(run).batch(
            units, config={"max_concurrency": 2}, return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                raise result
        self.control.checkpoint("synthesizing_strategy")
        findings = [result for result in results if isinstance(result, dict)]
        generator = LLMStrategyGenerator(
            TaskLLMClient(self.llm, self.control, synthesis_task),
            lambda: self.control.checkpoint("repairing_strategy"),
        )
        generator.control = self.control
        generator.defer_budget = bool(context.templates)
        generator.collaboration = tuple(findings)
        synthesis_key = (
            "solver-synthesis:"
            + hashlib.sha256(
                dumps(
                    {
                        "problem": problem_material(snapshot),
                        "rules": snapshot.reading.rules,
                        "issues": snapshot.reading.issues,
                        "template_readings": snapshot.template_readings,
                        "generation": context.config,
                        "templates": planning_templates(context.templates),
                        "previous_plan": context.previous_plan,
                        "findings": findings,
                        "prompt": files("erp_agent").joinpath("prompts/solver.txt").read_text(),
                    }
                ).encode()
            ).hexdigest()
        )
        saved = self.memory.get(synthesis_key) if self.memory else None
        if saved:
            proposal = decode_proposal(dumps(saved["proposal"]), string(saved["model"]))
            report_task(self.control, synthesis_task, "cached", model=proposal.model)
        else:
            report_task(self.control, synthesis_task, "running")
            try:
                proposal = generator.propose(snapshot, context)
            except Cancelled:
                raise
            except ERPError as error:
                report_task(self.control, synthesis_task, "failed", error=error.message)
                raise ERPError(
                    error.code,
                    error.message + " 已保存协作建议，重试可继续统筹方案。",
                    error.status,
                ) from error
            evaluation = DraftPlanEvaluator().evaluate(snapshot, proposal.baseline)
            if any(issue.severity == "error" for issue in evaluation.issues):
                raise ERPError(
                    "strategy_plan_invalid",
                    "；".join(i.message for i in evaluation.issues if i.severity == "error"),
                )
            if self.memory:
                self.memory.put(
                    synthesis_key,
                    {
                        "proposal": {"plan": to_json(proposal.baseline), "parameters": []},
                        "model": proposal.model,
                    },
                )
        report_task(self.control, synthesis_task, "succeeded", model=proposal.model)
        if context.templates:
            plan = PlanBudgetPlanner(self.llm, self.control, self.memory).plan(
                snapshot, context, proposal.baseline
            )
            proposal = replace(proposal, baseline=plan)
        return proposal
