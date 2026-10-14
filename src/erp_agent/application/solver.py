import re
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from uuid import uuid4

from erp_agent.application.contracts import (
    PATCH_SCHEMA,
    PROPOSAL_SCHEMA,
    JSONObject,
    apply_action_patch,
    decode_proposal,
    dumps,
    loads,
    to_json,
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
    role_material,
)
from erp_agent.domain.action_validation import validate_actions
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Action,
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
        self.defer_budget = True
        self.candidate_actions: tuple[Action, ...] = ()

    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_PLANNING_CONTEXT
    ) -> StrategyProposal:
        prompt = files("erp_agent").joinpath("prompts/solver.txt").read_text(encoding="utf-8")
        schema = dumps(PROPOSAL_SCHEMA)
        material = role_material(
            snapshot, "coordinator" if self.collaboration else "operations", self.candidate_actions
        )
        instruction = "只生成完整经营行动草稿，parameters 必须为空数组，budget_items 必须为空数组。"
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
            "template_requirements": to_json(snapshot.template_readings),
            "previous_plan": context.previous_plan,
            "collaboration": self.collaboration,
        }
        if self.defer_budget:
            prompt += "\n本阶段只统筹完整经营行动和策略，结合模板输入需求安排可执行动作；"
            prompt += "budget_items必须为空，后续预算独立执行。"
        original_payload = payload
        for attempt in range(2):
            response = None
            attempted_patch = False
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
                normalized = normalize_model_json(response.content)
                try:
                    proposal = decode_proposal(normalized, response.model, allow_legacy=False)
                except ERPError as error:
                    if attempt == 0 and re.search(r"actions\.(\d+)", error.message):
                        attempted_patch = True
                        proposal = self._repair_raw_action(
                            snapshot, normalized, response.model, error
                        )
                    else:
                        raise
                evaluation = DraftPlanEvaluator().evaluate(snapshot, proposal.baseline)
                errors = [
                    i
                    for i in evaluation.issues
                    if i.severity == "error" and i.code.startswith("plan.")
                ]
                if errors and attempt == 0 and not attempted_patch:
                    attempted_patch = True
                    proposal = self._repair_actions(snapshot, proposal, errors)
                    evaluation = DraftPlanEvaluator().evaluate(snapshot, proposal.baseline)
                    errors = [
                        i
                        for i in evaluation.issues
                        if i.severity == "error" and i.code.startswith("plan.")
                    ]
                if errors:
                    raise ERPError(
                        "strategy_plan_invalid", "；".join(i.message for i in errors[:8])
                    )
                if proposal.baseline.budget_items:
                    raise ERPError("invalid_contract", "行动阶段不能生成预算条目。")
                return replace(proposal, parameters=(), raw_response=response.content)
            except ERPError as error:
                if attempted_patch or error.code in {
                    "strategy_plan_invalid",
                    "patch_scope",
                    "patch_empty",
                }:
                    raise
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

    def _repair_raw_action(
        self, snapshot: ProblemSnapshot, content: str, model: str, error: ERPError
    ) -> StrategyProposal:
        from erp_agent.application.contracts import array, obj, string

        match = re.search(r"actions\.(\d+)", error.message)
        data = obj(loads(content))
        plan = obj(data["plan"])
        entries = list(array(plan["actions"]))
        index = int(match.group(1))
        raw = obj(entries[index])
        identifier = string(raw.get("action_id"))
        payload = {
            **role_material(snapshot, "coordinator"),
            "error": error.message,
            "action_id": identifier,
            "object": raw,
            "instruction": "仅修复这个行动的结构或类型，保留 action_id 与已正确值；"
            "不得改变其它行动、题目事实或版本。",
        }
        schema = dumps(PATCH_SCHEMA)
        request = LLMRequest(
            "只返回当前行动的受限补丁。\nJSON schema:\n" + schema,
            dumps(payload),
            "erp_action_patch",
            schema,
            max_output_tokens=4000,
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
        patches = array(obj(loads(normalize_model_json(response.content)), {"patches"})["patches"])
        if len(patches) != 1:
            raise ERPError("patch_scope", "结构补丁仅允许修复当前行动。")
        patch = obj(patches[0], {"action_id", "replacement"})
        replacement = obj(patch["replacement"])
        if patch["action_id"] != identifier or replacement.get("action_id") != identifier:
            raise ERPError("patch_scope", "结构补丁不能改写行动 ID。")
        entries[index] = replacement
        modified = {**data, "plan": {**plan, "actions": entries}}
        return decode_proposal(dumps(modified), model, allow_legacy=False)

    def _repair_actions(
        self, snapshot: ProblemSnapshot, proposal: StrategyProposal, errors: list[Issue]
    ) -> StrategyProposal:
        affected = set()
        allowed_fields: dict[str, set[str]] = {}
        for issue in errors:
            parts = issue.target.split(".")
            if len(parts) >= 2 and parts[0] == "actions" and parts[1].isdigit():
                identifier = proposal.baseline.actions[int(parts[1])].action_id
                affected.add(identifier)
                if len(parts) > 2:
                    allowed = allowed_fields.setdefault(identifier, set())
                    allowed.add(parts[2])
                    if issue.code == "plan.amount_unit":
                        allowed.update({"amount_unit", "amount_meaning"})
                    if issue.code == "plan.dependency_time":
                        allowed.update({"period", "depends_on"})
                    if issue.code == "plan.review_value":
                        allowed.update({"amount", "quantity", "amount_meaning", "missing_reason"})
        if not affected:
            raise ERPError("strategy_plan_invalid", "；".join(i.message for i in errors[:8]))
        if self.on_repair:
            self.on_repair()
        subset = tuple(a for a in proposal.baseline.actions if a.action_id in affected)
        payload = {
            **role_material(snapshot, "coordinator", subset),
            "errors": to_json(errors),
            "allowed_fields": {k: sorted(v) for k, v in allowed_fields.items()},
            "objects": to_json(subset),
            "dependency_catalog": [
                [a.action_id, a.period, a.completion_period] for a in proposal.baseline.actions
            ],
            "instruction": "仅替换 errors 对应行动，保留 action_id；"
            "不得修改题目、版本、其它行动或预算。",
        }
        schema = dumps(PATCH_SCHEMA)
        request = LLMRequest(
            "修复已定位的结构、引用或依赖错误；只返回受限补丁。\nJSON schema:\n" + schema,
            dumps(payload),
            "erp_action_patch",
            schema,
            max_output_tokens=4000,
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
        plan = apply_action_patch(
            proposal.baseline,
            loads(normalize_model_json(response.content)),
            affected,
            allowed_fields,
        )
        diagnostics = validate_actions(snapshot, plan)
        remaining = [i for i in diagnostics if i.severity == "error"]
        if remaining:
            raise ERPError(
                "strategy_plan_invalid",
                "局部修复仍未通过：" + "；".join(i.message for i in remaining[:8]),
            )
        return replace(proposal, baseline=plan)


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
        issues.extend(validate_actions(snapshot, plan, coverage=False))
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
