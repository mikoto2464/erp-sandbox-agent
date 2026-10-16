"""Solver v18：职责分析、来源交接、分段决策、确定性核验及冻结输出。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

from erp_agent.application.contracts import (
    TEXT,
    TEXTS,
    JSONObject,
    array,
    dumps,
    loads,
    obj,
    object_schema,
    string,
    strings,
    to_json,
)
from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.planning_analysis import validate_analysis
from erp_agent.application.planning_budget import allocate_planning_budgets, planning_call_limits
from erp_agent.application.planning_context import PlanningKnowledge, context_digest
from erp_agent.application.planning_decisions import compile_decisions, decision_schema
from erp_agent.application.planning_handoff import (
    calculation_handoff,
    provided_references,
    requested_calculations,
    requested_references,
)
from erp_agent.application.planning_prompts import planning_prompt
from erp_agent.application.planning_repairs import (
    apply_decision_repair,
    repair_periods,
    repair_schema,
)
from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse, RunControl, TaskContext
from erp_agent.application.solver_context import planning_policy, selected_snapshot
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.calculations import CalculationRecord
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    ParsedDocument,
    Plan,
    PlanningContext,
    ProblemSnapshot,
    StrategyProposal,
)
from erp_agent.domain.planning import PlanPreparation
from erp_agent.domain.reporting import ReportNote, ReportOutline

PLANNING_VERSION = "solver-18"
DEFAULT_CONTEXT = PlanningContext()
ANALYSIS_SCHEMA = object_schema(
    {
        "context_hash": TEXT,
        "summary": {"type": "string", "minLength": 1, "maxLength": 1600},
        "findings": {
            "type": "array",
            "maxItems": 12,
            "items": object_schema(
                {
                    "statement": {"type": "string", "minLength": 1, "maxLength": 1200},
                    "references": TEXTS,
                    "implication": {"type": "string", "maxLength": 1200},
                }
            ),
        },
    }
)

ANALYSIS_SCHEMA["properties"]["findings"]["items"]["properties"]["calculation_ids"] = TEXTS


def frozen_outline(plan: Plan) -> ReportOutline:
    # 文字在生成时确定；具体金额/数量由渲染器从同一行动字段插入。
    first = plan.actions[0].action_id
    milestones = tuple(
        ReportNote(
            "关键业务启动",
            "按当前行动的前置条件与完成时点安排执行。",
            period=a.period,
            action_ids=(a.action_id,),
        )
        for a in plan.actions
        if a.kind in {"research", "certify", "market_open", "install"}
    )
    return ReportOutline(
        strategy=(
            ReportNote("经营路线", "依据已冻结的经营决策与方案战略执行。", action_ids=(first,)),
        ),
        milestones=milestones,
        risks=(),
    )


class ParallelPlanningGenerator:
    def __init__(
        self,
        llm: LLMClient,
        control: RunControl,
        memory=None,
        documents: tuple[ParsedDocument, ...] = (),
    ) -> None:
        self.llm, self.control, self.memory, self.documents = llm, control, memory, documents

    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_CONTEXT
    ) -> StrategyProposal:
        from erp_agent.application.solver import DraftPlanEvaluator
        from erp_agent.application.solver_budget import PlanBudgetPlanner
        from erp_agent.application.solver_period_output import check_planning_content

        snapshot = selected_snapshot(snapshot, context.templates)
        knowledge = PlanningKnowledge(snapshot, self.documents)
        policy = planning_policy(context)
        horizon = (snapshot.reading.problem.years or 0) * (
            snapshot.reading.problem.periods_per_year or 0
        )
        if horizon < 1:
            raise ERPError("planning.horizon", "题目没有有效经营周期。")
        calculations: dict[str, CalculationRecord] = {}
        budget_calls = sum(
            1 for r in snapshot.template_readings for sheet in r.sheets if sheet.input_groups
        )

        allocations = allocate_planning_budgets(
            self.control, planning_call_limits(self.llm), min(4, (horizon + 3) // 4), budget_calls
        )

        def request_for(
            role: str,
            schema: JSONObject,
            references: tuple[str, ...] = (),
            findings: tuple[JSONObject, ...] = (),
        ) -> tuple[TaskContext, LLMRequest]:
            task = TaskContext(
                f"solver:{role}",
                "Solver",
                {
                    "operations": "产销与资源分析",
                    "finance": "资金与风险分析",
                    "synthesis": "合并分段方案与全周期校验" if horizon > 4 else "统筹逐期经营决策",
                }[role],
                snapshot.reading.problem.title,
                "solver",
            )
            material = knowledge.project(
                "coordinator" if role == "synthesis" else role, references=references
            )
            payload = {
                "context": material,
                "generation": to_json(context.config),
                "planning_policy": policy,
                "findings": list(findings),
                **calculation_handoff(calculations, requested_calculations(findings)),
            }
            if role == "synthesis" and context.previous_plan:
                previous = context.previous_plan
                payload["previous_plan_summary"] = {
                    "title": previous.title,
                    "summary": previous.summary,
                    "action_count": len(previous.actions),
                    "scope": "同题目版本的旧决策，当前题目与新要求优先；需要时按实体/关键词"
                    "查询 planning_lookup(kind=previous_actions)，不能把旧决定当题目事实。",
                }
            bounded_schema = deepcopy(schema)
            bounded_schema["properties"]["context_hash"] = {
                "type": "string",
                "const": knowledge.hash,
            }
            request = LLMRequest(
                system=planning_prompt(role),
                user=dumps(payload),
                schema_name="erp_planning_decisions"
                if role == "synthesis"
                else "erp_planning_analysis",
                schema_json=dumps(bounded_schema),
                max_output_tokens=16000 if role == "synthesis" else 4000,
                reasoning_reserve_tokens=8192,
                stream=True,
                retries=0,
                task=task,
                tool_context={
                    **knowledge.data,
                    "reserved_model_calls": 1
                    if allocations.get(task.id) and allocations[task.id].max_calls > 1
                    else 0,
                    "calculation_records": payload["calculation_results"],
                    "available_calculation_records": to_json(tuple(calculations.values())),
                    "previous_actions": to_json(context.previous_plan.actions)
                    if context.previous_plan
                    else [],
                },
                distribute=role != "synthesis",
            )
            return task, request

        def execute(unit: tuple[TaskContext, LLMRequest]) -> LLMResponse:
            task, request = unit
            supplied = provided_references(obj(loads(request.user)))
            # 初始资料及本任务前次实际访问可继承，不继承其它角色的裸已读标记。
            request = replace(
                request,
                tool_context={
                    **(request.tool_context or {}),
                    "accessed_reference_ids": sorted(
                        supplied
                        | set((request.tool_context or {}).get("accessed_reference_ids", []))
                    ),
                },
            )
            report_task(self.control, task, "queued", context_hash=knowledge.hash)
            # 缓存键对应调用者的稳定请求，运行时已读登记不改变缓存身份。
            key = "planning-18:" + context_digest({"request": unit[1]})
            saved = self.memory.get(key) if self.memory else None
            if saved and saved.get("context_hash") == knowledge.hash:
                from erp_agent.application.planning_codec import decode_calculation

                report_task(self.control, task, "cached", context_hash=knowledge.hash)
                return LLMResponse(
                    string(saved["content"]),
                    string(saved["model"]),
                    calculations=tuple(
                        decode_calculation(obj(c)) for c in array(saved.get("calculations", []))
                    ),
                    accessed_reference_ids=strings(saved.get("accessed_reference_ids", [])),
                )
            try:
                try:
                    response = recovery_call(
                        TaskLLMClient(self.llm, self.control, task), request, self.control
                    )
                except ERPError as error:
                    if error.code != "llm_structured_response":
                        raise
                    response = LLMResponse(
                        dumps({"context_hash": knowledge.hash, "invalid_output": error.message}), ""
                    )
                return replace(
                    response,
                    accessed_reference_ids=tuple(
                        sorted(
                            supplied
                            | set(response.accessed_reference_ids)
                            | set(request.tool_context["accessed_reference_ids"])
                        )
                    ),
                )
            except ERPError as error:
                report_task(self.control, task, "failed", error=error.message)
                raise

        allowed = {f.id for f in snapshot.reading.problem.facts} | {
            r.id for r in snapshot.reading.rules.rules
        }
        allowed |= {f"{i.code}:{i.target}" for i in snapshot.reading.issues}

        def cache_validated(unit: tuple[TaskContext, LLMRequest], result: LLMResponse) -> None:
            if self.memory:
                self.memory.put(
                    "planning-18:" + context_digest({"request": unit[1]}),
                    {
                        "content": result.content,
                        "model": result.model,
                        "calculations": to_json(result.calculations),
                        "context_hash": knowledge.hash,
                        "accessed_reference_ids": list(result.accessed_reference_ids),
                    },
                )

        def analyze(unit: tuple[TaskContext, LLMRequest]) -> LLMResponse:
            task, request = unit
            records: dict[str, CalculationRecord] = {}
            accessed: set[str] = set()
            loaded = {
                v["id"]
                for k in ("facts", "rules", "issues")
                for v in obj(obj(loads(request.user))["context"])[k]
            }
            for attempt in range(2):
                response = execute((task, request))
                data = None
                try:
                    for record in response.calculations:
                        if record.context_hash != knowledge.hash:
                            raise ERPError("planning.context", "职责计算引用其它题目或版本。")
                        if record.id in records and records[record.id] != record:
                            raise ERPError("planning.calculation", f"计算 ID 冲突：{record.id}")
                        records[record.id] = record
                    accessed.update(response.accessed_reference_ids)
                    loaded.update(accessed)
                    data = obj(loads(normalize_model_json(response.content)))
                    data = validate_analysis(
                        data,
                        knowledge.hash,
                        allowed,
                        loaded,
                        records,
                    )
                    response = replace(
                        response,
                        content=dumps(data),
                        calculations=tuple(records.values()),
                        accessed_reference_ids=tuple(sorted(accessed)),
                    )
                    report_task(
                        self.control,
                        task,
                        "succeeded",
                        model=response.model,
                        context_hash=knowledge.hash,
                    )
                    # 独立分析已经完整校验，可以保存检查点；后续路线失败不要求重复付费分析。
                    cache_validated(unit, response)
                    return response
                except ERPError as error:
                    if error.code == "planning.context" or attempt:
                        message = (
                            f"{task.role}经一次修正仍无效：{error.message}"
                            if attempt
                            else error.message
                        )
                        report_task(self.control, task, "failed", error=message)
                        raise ERPError(error.code, message) from error
                    material = knowledge.project(
                        "operations" if task.id.endswith("operations") else "finance",
                        references=requested_references(data),
                    )
                    accessed.update(
                        v["id"] for k in ("facts", "rules", "issues") for v in material[k]
                    )
                    loaded.update(accessed)
                    request = replace(
                        request,
                        tool_context={
                            **(request.tool_context or {}),
                            "calculation_records": to_json(tuple(records.values())),
                            "accessed_reference_ids": sorted(accessed),
                        },
                        user=dumps(
                            {
                                **obj(loads(unit[1].user)),
                                "context": material,
                                "diagnostics": error.message,
                                "repair_instruction": "只修正职责分析格式与引用，不新增事实。"
                                "保留已完成计算；calculation_ids 引用计算，"
                                "references 引用题目事实/规则/缺口。",
                                "previous_analysis": response.content[:16000],
                                "calculation_results": [to_json(r) for r in records.values()],
                            }
                        ),
                    )
            raise ERPError("planning.analysis", "职责分析未完成。")

        from erp_agent.application.task_budget import task_budget

        task_budget(self.control, TaskContext("solver", "Solver", "经营方案生成", knowledge.hash))
        units = [request_for(role, ANALYSIS_SCHEMA) for role in ("operations", "finance")]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(analyze, unit) for unit in units]
            responses = [future.result() for future in futures]
        findings, references = [], []
        for (task, _), response in zip(units, responses, strict=True):
            data = obj(loads(normalize_model_json(response.content)))
            for entry in map(obj, array(data["findings"])):
                references.extend(strings(entry["references"]))
            findings.append({"role": task.id, **data})
            for record in response.calculations:
                if record.context_hash != knowledge.hash:
                    raise ERPError("planning.context", "计算结果不属于当前题目。")
                if record.id in calculations and calculations[record.id] != record:
                    raise ERPError("planning.calculation", "并行角色计算 ID 冲突，请使用职责前缀。")
                calculations[record.id] = record
        synthesis = request_for(
            "synthesis", decision_schema(horizon), tuple(references), tuple(findings)
        )
        cache_entries = list(zip(units, responses, strict=True))
        if horizon > 4:
            from erp_agent.application.planning_windows import generate_windows

            result = generate_windows(
                snapshot, knowledge, synthesis, calculations, execute, self.control, budget_calls
            )
            plan, response = result.plan, result.response
            data = obj(loads(response.content))
            cache_entries.extend(result.cache_entries)
        else:
            response = execute(synthesis)
            synthesis_refs = set(response.accessed_reference_ids)
            for record in response.calculations:
                calculations[record.id] = record
            original_data = None
            affected_periods: set[str] = set()
            for attempt in range(2):
                data = None
                plan = None
                errors = []
                try:
                    data = obj(loads(normalize_model_json(response.content)))
                    if attempt and original_data is not None:
                        data = apply_decision_repair(original_data, data, affected_periods)
                    synthesis_refs.update(response.accessed_reference_ids)
                    available_refs = synthesis_refs
                    plan = compile_decisions(
                        data,
                        snapshot,
                        knowledge.hash,
                        tuple(calculations.values()),
                        available_refs,
                    )
                    check_planning_content(plan)
                    evaluation = DraftPlanEvaluator().evaluate(snapshot, plan)
                    errors = [
                        i
                        for i in evaluation.issues
                        if i.severity == "error" and i.code.startswith("plan.")
                    ]
                    if errors:
                        raise ERPError(
                            "planning.validation", "；".join(i.message for i in errors[:12])
                        )
                    break
                except (ERPError, KeyError) as error:
                    if attempt or (
                        isinstance(error, ERPError) and error.code == "planning.context"
                    ):
                        if isinstance(error, KeyError):
                            raise ERPError(
                                "planning.structure", "方案修正后仍缺少必要字段。"
                            ) from error
                        raise
                    task, request = synthesis
                    original_data = data
                    affected_periods = repair_periods(data, str(error), plan, tuple(errors))
                    evidence = knowledge.reference_batch(
                        tuple(
                            ref for ref in requested_references(data) if ref not in synthesis_refs
                        )
                    )
                    synthesis_refs.update(provided_references({"reference_evidence": evidence}))
                    repair = replace(
                        request,
                        tool_context={
                            **request.tool_context,
                            "reserved_model_calls": 0,
                            "calculation_records": to_json(tuple(calculations.values())),
                            "accessed_reference_ids": sorted(synthesis_refs),
                        },
                        schema_json=dumps(
                            repair_schema(obj(loads(request.schema_json)), affected_periods)
                        ),
                        user=dumps(
                            {
                                **obj(loads(request.user)),
                                "allowed_periods": sorted(affected_periods, key=int),
                                "previous_decisions": data,
                                "reference_evidence": evidence,
                                **calculation_handoff(
                                    calculations, requested_calculations(data) & calculations.keys()
                                ),
                                "diagnostics": str(error),
                                "repair_instruction": "仅改正诊断相关决策，保留其它行动 key、"
                                "数值与依赖。存在 allowed_periods 时只返回这些期的 periods "
                                "和 context_hash。",
                            }
                        ),
                    )
                    response = execute((task, repair))
                    for record in response.calculations:
                        if record.context_hash != knowledge.hash:
                            raise ERPError(
                                "planning.context", "公式结果的题目绑定失效。"
                            ) from error
                        calculations[record.id] = record
            cache_entries.append(
                (
                    synthesis,
                    replace(
                        response,
                        content=dumps(data),
                        calculations=tuple(calculations.values()),
                        accessed_reference_ids=tuple(sorted(synthesis_refs)),
                    ),
                )
            )
        report_task(
            self.control,
            synthesis[0],
            "succeeded",
            model=response.model,
            context_hash=knowledge.hash,
        )
        self.control.checkpoint("preparing_plan_parameters")
        # 预算来源匹配与计算属于生成阶段，导出阶段不再让模型生成参数。
        plan = replace(
            plan,
            preparation=PlanPreparation(
                knowledge.hash,
                tuple((t.source.id, t.source.sha256) for t in context.templates),
                tuple(calculations.values()),
                frozen_outline(plan),
                snapshot_hash=context_digest(snapshot),
            ),
        )
        task_budget(
            self.control,
            TaskContext("solver-budget", "Solver", "生成预算参数", knowledge.hash, "solver"),
        )
        partial_budgets = []
        try:
            budget = PlanBudgetPlanner(self.llm, self.control, self.memory).generate(
                snapshot,
                context,
                plan,
                on_sheet=partial_budgets.append,
            )
        except ERPError as error:
            if error.code != "budget.stage_failed" or not partial_budgets:
                raise
            budget = partial_budgets[-1]
            plan = replace(plan, risks=plan.risks + ("部分空表参数来源尚未完成：" + error.message,))
        plan = replace(
            plan,
            budget_items=budget.items,
            preparation=PlanPreparation(
                context_hash=knowledge.hash,
                template_hashes=tuple((t.source.id, t.source.sha256) for t in context.templates),
                calculations=tuple(calculations.values()),
                outline=frozen_outline(plan),
                snapshot_hash=context_digest(snapshot),
                budget_gaps=budget.gaps,
                completed_sheets=budget.completed_sheets,
                failed_sheets=budget.failed_sheets,
            ),
        )
        # 决策任务须整体通过后缓存；独立分析检查点不等于成功经营方案。
        for unit, result in cache_entries:
            cache_validated(unit, result)
        return StrategyProposal(plan, (), "", response.model)
