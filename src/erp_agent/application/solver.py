from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from itertools import pairwise
from uuid import uuid4

from erp_agent.application.contracts import (
    PROPOSAL_SCHEMA,
    JSONObject,
    decode_proposal,
    dumps,
)
from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import (
    LLMClient,
    LLMRequest,
    PlanEvaluator,
    RunControl,
    StrategyGenerator,
)
from erp_agent.application.solver_collaboration import (
    CollaborativeStrategyGenerator as CollaborativeStrategyGenerator,
)
from erp_agent.application.solver_context import (
    planning_templates,
    problem_material,
)
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
        self.control: RunControl | None = None
        self.defer_budget = False

    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_PLANNING_CONTEXT
    ) -> StrategyProposal:
        prompt = files("erp_agent").joinpath("prompts/solver.txt").read_text(encoding="utf-8")
        schema = dumps(PROPOSAL_SCHEMA)
        material = {
            "project_id": snapshot.project_id,
            "problem_version": snapshot.version,
            "problem": problem_material(snapshot),
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
            "budget_templates": planning_templates(context.templates),
            "previous_plan": context.previous_plan,
            "template_readings": snapshot.template_readings,
            "collaboration": self.collaboration,
        }
        if self.defer_budget:
            prompt += "\n本阶段只统筹完整经营行动和策略，结合模板输入需求安排可执行动作；"
            prompt += "budget_items必须为空，后续预算阶段基于固定行动逐表安排预算。"
        original_payload = payload
        for attempt in range(2):
            response = None
            try:
                if len(prompt) + len(schema) + len(dumps(payload)) > getattr(
                    self.llm, "max_input_chars", 100_000
                ):
                    raise ERPError("solver_input_limit", "方案输入超过模型容量，请减少空表或资料。")
                request = LLMRequest(
                    prompt + "\nJSON schema:\n" + schema,
                    dumps(payload),
                    "erp_strategy",
                    schema,
                    max_output_tokens=16000,
                    reasoning_reserve_tokens=8192,
                    stream=True,
                    retries=0,
                    task=getattr(self.llm, "task", None),
                )
                response = (
                    recovery_call(self.llm, request, self.control)
                    if self.control
                    else self.llm.generate(request)
                )
                proposal = decode_proposal(normalize_model_json(response.content), response.model)
                evaluation = DraftPlanEvaluator().evaluate(snapshot, proposal.baseline)
                errors = [
                    i.message
                    for i in evaluation.issues
                    if i.severity == "error" and i.code.startswith("plan.")
                ]
                if errors:
                    raise ERPError("strategy_plan_invalid", "；".join(errors[:8]))
                if self.defer_budget:
                    if proposal.baseline.budget_items:
                        raise ERPError("invalid_contract", "行动统筹阶段不能生成预算条目。")
                if (
                    context.templates
                    and not self.defer_budget
                    and not proposal.baseline.budget_items
                ):
                    raise ERPError(
                        "invalid_contract", "上传预算表后方案必须包含预算条目及未确定项。"
                    )
                if context.templates and not self.defer_budget and attempt == 0:
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
                    "修正完整经营周期与行动顺序，保持题目依据。"
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
            if missing or any(not 1 <= a.period <= horizon for a in plan.actions):
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
        if any(later.period < earlier.period for earlier, later in pairwise(plan.actions)):
            issues.append(
                Issue(
                    "plan.action_order",
                    "error",
                    "plan.actions",
                    "行动须按经营期递增排列，同期保留按题目规则安排的执行次序。",
                )
            )
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
