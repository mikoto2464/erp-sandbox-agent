import random
from dataclasses import replace
from decimal import ROUND_FLOOR
from importlib.resources import files
from typing import Callable
from uuid import uuid4

from erp_agent.application.contracts import PROPOSAL_SCHEMA, decode_proposal, dumps
from erp_agent.application.budget_context import budget_material
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import (
    LLMClient, LLMRequest, PlanEvaluator, RunControl, StrategyGenerator,
)
from erp_agent.domain.budget_validation import validate_budget_items
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Evaluation, Issue, Plan, ProblemSnapshot, SolutionBundle, SolveRequest,
    PlanTone, PlanningContext, SolverMode, StrategyProposal,
)


class LLMStrategyGenerator:
    def __init__(self, llm: LLMClient, on_repair: Callable[[], None] | None = None,
                 search_parameters: bool = True) -> None:
        self.llm = llm
        self.on_repair = on_repair
        self.search_parameters = search_parameters

    def propose(self, snapshot: ProblemSnapshot,
                context: PlanningContext = PlanningContext()) -> StrategyProposal:
        prompt = files("erp_agent").joinpath("prompts/solver.txt").read_text(encoding="utf-8")
        schema = dumps(PROPOSAL_SCHEMA)
        material = {"project_id": snapshot.project_id, "problem_version": snapshot.version,
                    "problem": snapshot.reading.problem, "rules": snapshot.reading.rules,
                    "issues": snapshot.reading.issues}
        instruction = ("生成包含基线值的搜索参数。" if self.search_parameters
                       else "当前为单 LLM 模式，无需搜索参数，parameters 必须为空数组。")
        tones = {
            PlanTone.CONSERVATIVE: "保守：优先现金安全与履约，控制借款、扩产和前期投入，说明收益取舍。",
            PlanTone.BALANCED: "平稳：兼顾现金周转与增长，分阶段投入，给出适度的风险缓冲。",
            PlanTone.AGGRESSIVE: "激进：在题目硬约束内争取增长与得分，可提高投入或融资，说明压力与退路。",
            PlanTone.CUSTOM: "自定义：采用 custom_prompt 中的经营偏好，仍遵守题目与已确认规则。",
        }
        payload = {"snapshot": material, "mode_instruction": instruction,
                   "generation": context.config, "tone_instruction": tones[context.config.tone],
                   "budget_templates": budget_material(context.templates),
                   "previous_plan": context.previous_plan}
        original_payload = payload
        for attempt in range(2):
            response = None
            try:
                response = self.llm.generate(LLMRequest(
                    prompt + "\nJSON schema:\n" + schema,
                    dumps(payload), "erp_strategy", schema,
                ))
                proposal = decode_proposal(normalize_model_json(response.content), response.model)
                if context.templates and not proposal.baseline.budget_items:
                    raise ERPError("invalid_contract", "上传预算表后方案必须包含预算条目及未确定项。")
                if context.templates and attempt == 0:
                    issues = validate_budget_items(SolveRequest(snapshot, SolverMode.LLM_ONLY, context=context),
                                                   proposal.baseline)
                    errors = [i.message for i in issues if i.severity == "error"]
                    if errors:
                        raise ERPError("invalid_contract", "预算安排与空表不一致：" + "；".join(errors))
                return replace(proposal, raw_response=response.content)
            except ERPError as error:
                if response is None and error.code != "llm_structured_response":
                    raise
                if attempt:
                    raise ERPError("strategy_output_invalid",
                                   "模型连续两次未返回符合要求的方案结构。"
                                   f"校验原因：{error.message} 请检查输出格式后重试。", 502) from error
                if self.on_repair:
                    self.on_repair()
                payload = {**original_payload,
                           "previous_output": response.content if response else None,
                           "error": error.message,
                           "instruction": "修正方案 JSON、字段类型及已定位的模板遗漏/输入错误，保持题目依据。"
                                          "只返回一个完整 JSON 对象，不附加 Markdown 或说明。"}
        raise ERPError("strategy_output_invalid", "未生成有效的方案结构。", 502)


class DraftPlanEvaluator:
    @property
    def supports_scoring(self) -> bool:
        return False

    def evaluate(self, snapshot: ProblemSnapshot, plan: Plan) -> Evaluation:
        issues = [i for i in snapshot.reading.issues if i.severity == "error"]
        problem = snapshot.reading.problem
        horizon = (problem.years * problem.periods_per_year
                   if problem.years and problem.periods_per_year else None)
        if horizon is None:
            issues.append(Issue("plan.horizon_missing", "warning", "plan", "经营周期未确认。"))
        else:
            missing = set(range(1, horizon + 1)) - {a.period for a in plan.actions}
            if missing or any(a.period > horizon for a in plan.actions):
                issues.append(Issue("plan.period_coverage", "error", "plan",
                                    "行动必须覆盖经营周期的每一期且不能越界。"))
        entities = {f.entity_id for f in problem.facts} | {"company"}
        if not plan.actions:
            issues.append(Issue("plan.empty", "error", "plan", "方案没有行动。"))
        for index, action in enumerate(plan.actions):
            if action.entity_id not in entities:
                issues.append(Issue("plan.entity", "error", f"actions.{index}", "行动引用未知实体。"))
            if action.amount is not None and action.amount < 0:
                issues.append(Issue("plan.amount", "error", f"actions.{index}", "行动金额不能为负。"))
        issues.append(Issue("simulation.unavailable", "warning", "plan",
                            "已检查结构与周期；经营仿真尚未实现，产能、现金和得分未验证。"))
        return Evaluation(not any(i.severity == "error" for i in issues), False, None,
                          tuple(issues), "结构校验草稿，尚未验证经营可行性")


class SolverService:
    def __init__(self, generator: StrategyGenerator, evaluator: PlanEvaluator) -> None:
        self.generator = generator
        self.evaluator = evaluator

    def solve(self, request: SolveRequest, control: RunControl,
              generator: StrategyGenerator | None = None) -> SolutionBundle:
        if request.mode == SolverMode.LLM_MONTE_CARLO and not self.evaluator.supports_scoring:
            raise ERPError("simulation_not_ready", "蒙特卡罗需要可评分的仿真器；当前内核尚未就绪。", 409)
        if request.mode == SolverMode.LLM_MONTE_CARLO and request.context.templates:
            raise ERPError("budget_search_unavailable", "预算条目规划当前仅支持单 LLM 模式。", 409)
        if not 1 <= request.search.candidates <= 2000:
            raise ERPError("search_limit", "候选数必须在 1 到 2000 之间。")
        control.checkpoint("proposing")
        proposal = (generator or self.generator).propose(request.snapshot, request.context)
        control.checkpoint("validating_plan")
        baseline = self.evaluator.evaluate(request.snapshot, proposal.baseline)
        budget_issues = validate_budget_items(request, proposal.baseline)
        if budget_issues:
            baseline = replace(baseline, issues=baseline.issues + budget_issues,
                               feasible=baseline.feasible and not any(
                                   i.severity == "error" for i in budget_issues))
        best_plan, best_eval = proposal.baseline, baseline
        candidates = [proposal.baseline]
        feasible = int(baseline.feasible)
        if request.mode == SolverMode.LLM_MONTE_CARLO:
            if not baseline.feasible or not baseline.verified or baseline.score is None:
                raise ERPError("baseline_invalid", "仿真未产生可行且已验证的基线，无法搜索。", 409)
            rng = random.Random(request.search.seed)
            for index in range(1, request.search.candidates):
                control.checkpoint("searching", index)
                actions = list(proposal.baseline.actions)
                for parameter in proposal.parameters:
                    steps = int(((parameter.maximum - parameter.minimum) / parameter.step)
                                .to_integral_value(rounding=ROUND_FLOOR))
                    value = parameter.minimum + parameter.step * rng.randrange(steps + 1)
                    action = actions[parameter.action_index]
                    actions[parameter.action_index] = (
                        replace(action, quantity=int(value)) if parameter.field == "quantity"
                        else replace(action, amount=value)
                    )
                plan = replace(proposal.baseline, actions=tuple(actions))
                evaluation = self.evaluator.evaluate(request.snapshot, plan)
                candidates.append(plan)
                if evaluation.feasible and evaluation.verified and evaluation.score is not None:
                    feasible += 1
                    if best_eval.score is None or evaluation.score > best_eval.score:
                        best_plan, best_eval = plan, evaluation
            control.checkpoint("validating_best", len(candidates))
            final = self.evaluator.evaluate(request.snapshot, best_plan)
            if not final.feasible or not final.verified or final.score != best_eval.score:
                raise ERPError("verification_failed", "最佳方案复验与搜索结果不一致。", 409)
            best_eval = final
        control.checkpoint("saving", len(candidates))
        return SolutionBundle(
            uuid4().hex, request.snapshot.project_id, request.snapshot.version, request.mode,
            best_plan, best_eval, baseline.score, len(candidates), feasible,
            request.search.seed if request.mode == SolverMode.LLM_MONTE_CARLO else None,
            proposal.model, proposal.raw_response, tuple(candidates), request.context.config,
        )
