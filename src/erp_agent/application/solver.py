from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from uuid import uuid4

from langchain_core.runnables import RunnableLambda

from erp_agent.application.budget_context import budget_material
from erp_agent.application.contracts import (
    PROPOSAL_SCHEMA,
    TEXT,
    JSONObject,
    array,
    decode_proposal,
    dumps,
    loads,
    obj,
    object_schema,
    string,
)
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import (
    LLMClient,
    LLMRequest,
    PlanEvaluator,
    RunControl,
    StrategyGenerator,
    TaskContext,
)
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.budget_validation import validate_budget_items
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Evaluation,
    Issue,
    Plan,
    PlanningContext,
    PlanTone,
    ProblemSnapshot,
    SolutionBundle,
    SolveRequest,
    SolverMode,
    StrategyProposal,
)

DEFAULT_PLANNING_CONTEXT = PlanningContext()


class LLMStrategyGenerator:
    def __init__(
        self,
        llm: LLMClient,
        on_repair: Callable[[], None] | None = None,
    ) -> None:
        self.llm = llm
        self.on_repair = on_repair
        self.collaboration: tuple[JSONObject, ...] = ()

    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_PLANNING_CONTEXT
    ) -> StrategyProposal:
        prompt = files("erp_agent").joinpath("prompts/solver.txt").read_text(encoding="utf-8")
        schema = dumps(PROPOSAL_SCHEMA)
        material = {
            "project_id": snapshot.project_id,
            "problem_version": snapshot.version,
            "problem": snapshot.reading.problem,
            "rules": snapshot.reading.rules,
            "issues": snapshot.reading.issues,
        }
        instruction = "只生成完整经营方案，parameters 必须为空数组。"
        tones = {
            PlanTone.CONSERVATIVE: "保守：优先现金安全与履约，控制借款、扩产和前期投入，"
                                   "说明收益取舍。",
            PlanTone.BALANCED: "平稳：兼顾现金周转与增长，分阶段投入，给出适度的风险缓冲。",
            PlanTone.AGGRESSIVE: "激进：在题目硬约束内争取增长与得分，可提高投入或融资，"
                                 "说明压力与退路。",
            PlanTone.CUSTOM: "自定义：采用 custom_prompt 中的经营偏好，仍遵守题目与已确认规则。",
        }
        payload = {
            "snapshot": material,
            "mode_instruction": instruction,
            "generation": context.config,
            "tone_instruction": tones[context.config.tone],
            "budget_templates": budget_material(context.templates),
            "previous_plan": context.previous_plan,
            "template_readings": snapshot.template_readings,
            "collaboration": self.collaboration,
        }
        original_payload = payload
        for attempt in range(2):
            response = None
            try:
                if len(prompt) + len(schema) + len(dumps(payload)) > getattr(
                    self.llm, "max_input_chars", 100_000
                ):
                    raise ERPError("solver_input_limit", "方案输入超过模型容量，请减少空表或资料。")
                response = self.llm.generate(
                    LLMRequest(
                        prompt + "\nJSON schema:\n" + schema,
                        dumps(payload),
                        "erp_strategy",
                        schema,
                    )
                )
                proposal = decode_proposal(normalize_model_json(response.content), response.model)
                if context.templates and not proposal.baseline.budget_items:
                    raise ERPError(
                        "invalid_contract", "上传预算表后方案必须包含预算条目及未确定项。"
                    )
                if context.templates and attempt == 0:
                    issues = validate_budget_items(
                        SolveRequest(snapshot, SolverMode.LLM_ONLY, context=context),
                        proposal.baseline,
                    )
                    errors = [i.message for i in issues if i.severity == "error"]
                    if errors:
                        raise ERPError(
                            "invalid_contract", "预算安排与空表不一致：" + "；".join(errors)
                        )
                return replace(proposal, parameters=(), raw_response=response.content)
            except ERPError as error:
                if response is None and error.code != "llm_structured_response":
                    raise
                if attempt:
                    raise ERPError(
                        "strategy_output_invalid",
                        "模型连续两次未返回符合要求的方案结构。"
                        f"校验原因：{error.message} 请检查输出格式后重试。",
                        502,
                    ) from error
                if self.on_repair:
                    self.on_repair()
                payload = {
                    **original_payload,
                    "previous_output": response.content if response else None,
                    "error": error.message,
                    "instruction": "修正方案 JSON、字段类型及已定位的模板遗漏/输入错误，"
                    "保持题目依据。"
                    "只返回一个完整 JSON 对象，不附加 Markdown 或说明。",
                }
        raise ERPError("strategy_output_invalid", "未生成有效的方案结构。", 502)


class DraftPlanEvaluator:
    @property
    def supports_scoring(self) -> bool:
        return False

    def evaluate(self, snapshot: ProblemSnapshot, plan: Plan) -> Evaluation:
        issues = [i for i in snapshot.reading.issues if i.severity == "error"]
        problem = snapshot.reading.problem
        horizon = (
            problem.years * problem.periods_per_year
            if problem.years and problem.periods_per_year
            else None
        )
        if horizon is None:
            issues.append(Issue("plan.horizon_missing", "warning", "plan", "经营周期未确认。"))
        else:
            missing = set(range(1, horizon + 1)) - {a.period for a in plan.actions}
            if missing or any(a.period > horizon for a in plan.actions):
                issues.append(
                    Issue(
                        "plan.period_coverage",
                        "error",
                        "plan",
                        "行动必须覆盖经营周期的每一期且不能越界。",
                    )
                )
        entities = {f.entity_id for f in problem.facts} | {"company"}
        if not plan.actions:
            issues.append(Issue("plan.empty", "error", "plan", "方案没有行动。"))
        for index, action in enumerate(plan.actions):
            if action.entity_id not in entities:
                issues.append(
                    Issue("plan.entity", "error", f"actions.{index}", "行动引用未知实体。")
                )
            if action.amount is not None and action.amount < 0:
                issues.append(
                    Issue("plan.amount", "error", f"actions.{index}", "行动金额不能为负。")
                )
        issues.append(
            Issue(
                "simulation.unavailable",
                "warning",
                "plan",
                "已检查结构与周期；经营仿真尚未实现，产能、现金和得分未验证。",
            )
        )
        return Evaluation(
            not any(i.severity == "error" for i in issues),
            False,
            None,
            tuple(issues),
            "结构校验草稿，尚未验证经营可行性",
        )


class CollaborativeStrategyGenerator:
    def __init__(self, llm: LLMClient, control: RunControl) -> None:
        self.llm, self.control = llm, control

    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_PLANNING_CONTEXT
    ) -> StrategyProposal:
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
            ("operations", "产销与资源规划", "协调产品、订单池、产能、材料、人力、研发与认证。"),
            ("finance", "资金与风险规划", "协调融资、费用、现金时序、商誉、碳及得分条件。"),
        ]
        if context.templates:
            roles.append(("budget", "模板预算规划", "按已核对空表识别输入项目与经营动作依赖。"))
        entities = {f.entity_id for f in snapshot.reading.problem.facts} | {"company"}
        fact_ids = {f.id for f in snapshot.reading.problem.facts}
        rule_ids = {r.id for r in snapshot.reading.rules.rules}
        prompt = files("erp_agent").joinpath("prompts/solver_specialist.txt").read_text()

        synthesis_task = TaskContext(
            "solver:synthesis", "Solver", "统筹逐期方案与预算",
            snapshot.reading.problem.title, "solver",
        )

        def task_for(role: tuple[str, str, str]) -> TaskContext:
            return TaskContext(
                "solver:" + role[0], "Solver", role[1], snapshot.reading.problem.title, "solver"
            )

        for role in roles:
            report_task(self.control, task_for(role), "queued")
        report_task(self.control, synthesis_task, "queued")

        def run(role: tuple[str, str, str]) -> JSONObject:
            key, name, responsibility = role
            task = task_for(role)
            client = TaskLLMClient(self.llm, self.control, task)
            material = {
                "role": key,
                "responsibility": responsibility,
                "problem": snapshot.reading.problem,
                "rules": snapshot.reading.rules,
                "issues": snapshot.reading.issues,
                "generation": context.config,
                "previous_plan": context.previous_plan,
                "template_readings": snapshot.template_readings,
            }
            if key == "budget":
                material["budget_templates"] = budget_material(context.templates)
            for attempt in range(2):
                self.control.checkpoint("planning_" + key)
                if len(prompt) + len(schema) + len(dumps(material)) > getattr(
                    self.llm, "max_input_chars", 100_000
                ):
                    report_task(self.control, task, "failed", error="输入超过模型容量。")
                    raise ERPError(
                        "solver_input_limit", name + "输入超过模型容量，请减少空表或资料。"
                    )
                response = client.generate(
                    LLMRequest(
                        prompt + "\nJSON schema:\n" + schema,
                        dumps(material),
                        "erp_solver_specialist",
                        schema,
                        max_output_tokens=6000,
                        retries=0,
                    )
                )
                try:
                    data = obj(
                        loads(normalize_model_json(response.content)),
                        {"recommendations", "dependencies", "risks"},
                    )
                    for value in array(data["recommendations"]):
                        entry = obj(
                            value,
                            {"entity_id", "periods", "recommendation", "fact_ids", "rule_ids"},
                        )
                        if (
                            string(entry["entity_id"]) not in entities
                            or {string(v) for v in array(entry["fact_ids"])} - fact_ids
                            or {string(v) for v in array(entry["rule_ids"])} - rule_ids
                        ):
                            raise ERPError("solver_source", "协作建议引用未知实体、字段或规则。")
                        string(entry["periods"])
                        string(entry["recommendation"])
                    for value in array(data["dependencies"]) + array(data["risks"]):
                        string(value)
                    report_task(
                        self.control,
                        task,
                        "succeeded",
                        model=response.model,
                        recommendation_count=len(array(data["recommendations"])),
                    )
                    return {"role": name, **data}
                except ERPError as error:
                    if attempt:
                        report_task(self.control, task, "failed", error=error.message)
                        raise
                    material["error"] = error.message
            raise ERPError("solver_specialist_invalid", "协作规划失败。")

        findings = RunnableLambda(run).batch(roles, config={"max_concurrency": 3})
        self.control.checkpoint("synthesizing_strategy")
        generator = LLMStrategyGenerator(
            TaskLLMClient(self.llm, self.control, synthesis_task),
            lambda: self.control.checkpoint("repairing_strategy"),
        )
        generator.collaboration = tuple(findings)
        proposal = generator.propose(snapshot, context)
        report_task(self.control, synthesis_task, "succeeded", model=proposal.model)
        return proposal


class SolverService:
    def __init__(self, generator: StrategyGenerator, evaluator: PlanEvaluator) -> None:
        self.generator = generator
        self.evaluator = evaluator

    def solve(
        self, request: SolveRequest, control: RunControl, generator: StrategyGenerator | None = None
    ) -> SolutionBundle:
        if request.mode != SolverMode.LLM_ONLY:
            raise ERPError("solver_mode_removed", "蒙特卡罗已移除，请使用协作生成方案。", 409)
        control.checkpoint("proposing")
        proposal = (generator or self.generator).propose(request.snapshot, request.context)
        control.checkpoint("validating_plan")
        baseline = self.evaluator.evaluate(request.snapshot, proposal.baseline)
        budget_issues = validate_budget_items(request, proposal.baseline)
        if budget_issues:
            baseline = replace(
                baseline,
                issues=baseline.issues + budget_issues,
                feasible=baseline.feasible
                and not any(i.severity == "error" for i in budget_issues),
            )
        best_plan, best_eval = proposal.baseline, baseline
        candidates = [proposal.baseline]
        feasible = int(baseline.feasible)
        control.checkpoint("saving", len(candidates))
        return SolutionBundle(
            uuid4().hex,
            request.snapshot.project_id,
            request.snapshot.version,
            request.mode,
            best_plan,
            best_eval,
            baseline.score,
            len(candidates),
            feasible,
            None,
            proposal.model,
            proposal.raw_response,
            tuple(candidates),
            request.context.config,
        )
