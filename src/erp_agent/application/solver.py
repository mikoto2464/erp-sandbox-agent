import re
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from uuid import uuid4

from erp_agent.application.contracts import (
    PATCH_SCHEMA,
    PROPOSAL_SCHEMA,
    JSONObject,
    decode_action,
    decode_proposal,
    dumps,
    loads,
    obj,
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
    planning_policy,
    repair_material,
    role_material,
)
from erp_agent.application.solver_plan_repairs import (
    MAX_REPAIR_OBJECTS,
    apply_plan_repair,
    plan_repair_schema,
)
from erp_agent.domain.action_validation import validate_actions
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    Action,
    Evaluation,
    Issue,
    Plan,
    PlanningContext,
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
        policy = planning_policy(context)
        payload = {
            "snapshot": material,
            "mode_instruction": instruction,
            "generation": context.config,
            "tone_instruction": policy["tone_instruction"],
            "planning_policy": policy,
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
        known_actions = []
        for entry in entries:
            try:
                known_actions.append(decode_action(entry))
            except ERPError:
                # 待修对象原样传递，目录不把未解码对象提升为正式行动。
                continue
        payload = {
            **repair_material(snapshot, tuple(known_actions), [raw]),
            "project_id": snapshot.project_id,
            "problem_version": snapshot.version,
            "rule_version": snapshot.reading.rules.version,
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
                        allowed.discard("depends_on")
                        allowed.update({"period", "completion_period", "payment_period"})
                    if issue.code == "plan.action_order":
                        index = int(parts[1])
                        if index > 0:
                            previous_id = proposal.baseline.actions[index - 1].action_id
                            affected.add(previous_id)
                            allowed_fields.setdefault(previous_id, set()).update(
                                {"period", "completion_period", "payment_period"}
                            )
                    if issue.code == "plan.review_value":
                        allowed.update({"amount", "quantity", "amount_meaning", "missing_reason"})
        temporal = {identifier for identifier, names in allowed_fields.items() if "period" in names}
        changed = True
        while changed:
            changed = False
            for action in proposal.baseline.actions:
                if temporal.intersection(action.depends_on) and action.action_id not in temporal:
                    temporal.add(action.action_id)
                    affected.add(action.action_id)
                    allowed_fields.setdefault(action.action_id, set()).update(
                        {"period", "completion_period", "payment_period"}
                    )
                    changed = True
        problem = snapshot.reading.problem
        horizon = (problem.years or 0) * (problem.periods_per_year or 0)
        missing = sorted(set(range(1, horizon + 1)) - {a.period for a in proposal.baseline.actions})
        if len(affected) > MAX_REPAIR_OBJECTS or len(missing) > MAX_REPAIR_OBJECTS:
            raise ERPError(
                "solver_repair_limit", "本轮修复范围或缺失经营期超过 24 项，本次修复停止。"
            )
        allow_additions = bool(horizon and proposal.baseline.actions and (missing or temporal))
        if not proposal.baseline.actions or not (affected or allow_additions):
            raise ERPError("strategy_plan_invalid", "；".join(i.message for i in errors[:8]))
        if self.on_repair:
            self.on_repair()
        subset = tuple(a for a in proposal.baseline.actions if a.action_id in affected)
        payload = {
            **repair_material(
                snapshot,
                proposal.baseline.actions,
                [
                    obj(to_json(a))
                    for a in (proposal.baseline.actions if allow_additions else subset)
                ],
            ),
            "project_id": snapshot.project_id,
            "problem_version": snapshot.version,
            "rule_version": snapshot.reading.rules.version,
            "errors": to_json(errors),
            "allowed_fields": {k: sorted(v) for k, v in allowed_fields.items()},
            "objects": to_json(subset),
            "instruction": "仅替换 errors 对应行动，保留 action_id；"
            "只能修改 allowed_fields；保留时间依赖，不能清空已知完成/支付期。"
            "不得修改题目、版本、其它行动、动作顺序或预算。",
        }
        if allow_additions:
            coverage_context = role_material(snapshot, "coordinator", proposal.baseline.actions)
            payload["problem"] = coverage_context["problem"]
            payload["rules"] = coverage_context["rules"]
            payload.update(
                {
                    "period_coverage": {
                        "horizon": horizon,
                        "covered_periods": sorted({a.period for a in proposal.baseline.actions}),
                        "missing_periods": missing,
                        "potentially_vacated_periods": sorted(
                            {a.period for a in subset if a.action_id in temporal}
                        ),
                    },
                    "plan_context": role_material(
                        snapshot, "coordinator", proposal.baseline.actions
                    ),
                }
            )
            payload["instruction"] += (
                " 本次可用 additions 补充替换后仍缺失的经营期，使用新 action_id；"
                "针对已有依赖、交付/收付或终局核对提出该期具体行动，不用泛化 review 填满。"
                "新行动需合法引用，未知值保留 null，不编造数值；"
                "不得在已覆盖期追加或覆盖已有行动，替换与新增合计最多24项。"
            )
        schema = dumps(plan_repair_schema(allow_additions))
        output_budget = min(16000, max(4000, (len(affected) + len(missing) + len(temporal)) * 600))
        request = LLMRequest(
            "修复已定位的结构、引用、依赖或周期遗漏；只返回受限补丁。\nJSON schema:\n" + schema,
            dumps(payload),
            "erp_action_patch",
            schema,
            max_output_tokens=output_budget,
            reasoning_reserve_tokens=8192,
            stream=True,
            retries=0,
            task=getattr(self.llm, "task", None),
        )
        if len(request.system) + len(request.user) + len(request.schema_json) > getattr(
            self.llm, "max_input_chars", 100_000
        ):
            raise ERPError("solver_input_limit", "周期补丁及相关行动超过当前模型输入容量。", 413)
        response = (
            recovery_call(self.llm, request, self.control)
            if self.control
            else self.llm.generate(request)
        )
        plan = apply_plan_repair(
            proposal.baseline,
            loads(normalize_model_json(response.content)),
            affected,
            allowed_fields,
            horizon=horizon,
            allow_additions=allow_additions,
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
        issues.extend(validate_actions(snapshot, plan))
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
