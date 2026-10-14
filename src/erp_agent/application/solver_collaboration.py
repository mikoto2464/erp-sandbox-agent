import hashlib
from dataclasses import replace
from importlib.resources import files

from langchain_core.runnables import RunnableLambda

from erp_agent.application.contracts import (
    SPECIALIST_SCHEMA,
    JSONObject,
    array,
    decode_action,
    decode_proposal,
    dumps,
    loads,
    obj,
    string,
    to_json,
)
from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.solver_context import (
    action_material,
    entity_catalog,
    planning_policy,
    planning_requirements,
    repair_material,
    role_material,
    selected_snapshot,
)
from erp_agent.application.solver_repairs import apply_specialist_patch, specialist_patch
from erp_agent.application.solver_specialist import decode_specialist as decode_specialist
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Action, PlanningContext, ProblemSnapshot, StrategyProposal

DEFAULT_PLANNING_CONTEXT = PlanningContext()


class CollaborativeStrategyGenerator:
    def __init__(
        self, llm: LLMClient, control: RunControl, memory: RoleMemory | None = None
    ) -> None:
        self.llm, self.control, self.memory = llm, control, memory

    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_PLANNING_CONTEXT
    ) -> StrategyProposal:
        from erp_agent.application.solver import LLMStrategyGenerator

        snapshot = selected_snapshot(snapshot, context.templates)
        prompt = files("erp_agent").joinpath("prompts/solver_specialist.txt").read_text()
        schema = dumps(SPECIALIST_SCHEMA)
        synthesis_task = TaskContext(
            "solver:synthesis",
            "Solver",
            "统筹逐期经营行动",
            snapshot.reading.problem.title,
            "solver",
        )
        operations_task = TaskContext(
            "solver:operations",
            "Solver",
            "产销与资源规划",
            snapshot.reading.problem.title,
            "solver",
        )
        finance_task = TaskContext(
            "solver:finance", "Solver", "资金与风险核对", snapshot.reading.problem.title, "solver"
        )

        def material_for(role: str, candidates: tuple[Action, ...] = ()) -> JSONObject:
            material = {
                **role_material(snapshot, role, candidates),
                "role": role,
                "generation": to_json(context.config),
                "planning_policy": planning_policy(context),
                "template_requirements": planning_requirements(snapshot),
                "entity_catalog": entity_catalog(snapshot),
            }
            if role == "operations":
                material["previous_plan"] = to_json(
                    replace(context.previous_plan, budget_items=())
                    if context.previous_plan
                    else None
                )
            if role == "finance":
                material["candidate_plan"] = to_json(action_material(candidates))
            if role == "budget":
                material["entity_catalog"] = [
                    entry
                    for entry in material["entity_catalog"]
                    if "order_pool" not in entry["categories"]
                ]
                material["scope_instruction"] += "模板 ID、工作表、输入组/范围不是经营实体，"
                material["scope_instruction"] += (
                    "跨对象模板建议用 company；entity_id 只能选 entity_catalog 的 id。"
                )
            return material

        def run(unit: tuple[TaskContext, JSONObject, tuple[Action, ...]]) -> JSONObject:
            task, material, candidates = unit
            role = string(material["role"])
            schema_data = obj(loads(schema))
            schema_data["properties"]["recommendations"]["items"]["properties"]["entity_id"] = {
                "type": "string",
                "enum": [entry["id"] for entry in material["entity_catalog"]],
            }
            if role == "budget":
                schema_data["properties"]["candidate_actions"]["maxItems"] = 0
            task_schema = dumps(schema_data)
            request = LLMRequest(
                prompt + "\nJSON schema:\n" + task_schema,
                dumps(material),
                "erp_solver_specialist",
                task_schema,
                max_output_tokens=6000,
                reasoning_reserve_tokens=8192,
                stream=True,
                retries=0,
                task=task,
            )
            key = (
                "solver-unit:"
                + hashlib.sha256(
                    dumps({"version": "solver-14", "request": request}).encode()
                ).hexdigest()
            )
            saved = self.memory.get(key) if self.memory else None
            if saved and "result" in saved:
                data = decode_specialist(
                    obj(saved["result"]), snapshot, candidate_actions=candidates, role=role
                )
                report_task(self.control, task, "cached")
                return {"role": material["role"], **data}
            client = TaskLLMClient(self.llm, self.control, task)
            report_task(self.control, task, "running")
            try:
                if len(request.system) + len(request.user) + len(request.schema_json) > getattr(
                    self.llm, "max_input_chars", 100_000
                ):
                    raise ERPError(
                        "llm_input_limit", "协作任务输入超过模型容量，请缩小当前任务范围。", 413
                    )
                response = recovery_call(client, request, self.control)
                try:
                    raw = obj(loads(normalize_model_json(response.content)))
                    data = decode_specialist(raw, snapshot, candidate_actions=candidates, role=role)
                except ERPError as error:
                    if error.code == "solver_failed":
                        raise
                    raw = locals().get("raw")
                    patch = specialist_patch(raw, error) if raw is not None else None
                    if patch:
                        known_actions = list(candidates)
                        for value in array(raw["candidate_actions"]):
                            try:
                                known_actions.append(decode_action(value))
                            except ERPError:
                                # 错误对象原样留在补丁范围，只读依赖目录仅使用完整可解码行动。
                                continue
                        focused = {
                            key: material[key]
                            for key in ("project_id", "problem_version", "rule_version", "role")
                        }
                        focused.update(
                            repair_material(snapshot, tuple(known_actions), list(patch.objects))
                        )
                        focused["reference_catalog"]["action_ids"] = list(
                            dict.fromkeys(
                                [a.action_id for a in candidates]
                                + [
                                    value["action_id"]
                                    for value in array(raw["candidate_actions"])
                                    if isinstance(value, dict)
                                    and isinstance(value.get("action_id"), str)
                                ]
                            )
                        )
                        focused.update(
                            {
                                "error": error.message,
                                "objects": list(patch.objects),
                                "object": patch.objects[0] if len(patch.objects) == 1 else None,
                                "instruction": "一次修复目录内的定位问题和受影响下游时间字段；"
                                "replacement 只返回 allowed_fields 中的字段，"
                                "其余内容由 Python 原样保留，未开放的 condition 不要重新输出。"
                                "保持稳定 ID 和目录外对象。"
                                "不能删掉时间依赖或清空已知完成/支付期来绕过校验；"
                                "实体 ID 从 entity_catalog 选，不将模板或输入组 ID 当实体。",
                            }
                        )
                        repair_request = replace(
                            request,
                            user=dumps(focused),
                            schema_name="erp_specialist_patch",
                            schema_json=dumps(patch.schema),
                            system="只返回目录内对象的受限修正。\nJSON schema:\n"
                            + dumps(patch.schema),
                            max_output_tokens=min(12000, max(2000, len(patch.objects) * 600)),
                        )
                        repaired = recovery_call(client, repair_request, self.control)
                        patched = apply_specialist_patch(
                            raw,
                            obj(loads(normalize_model_json(repaired.content))),
                            patch,
                        )
                        data = decode_specialist(
                            patched, snapshot, candidate_actions=candidates, role=role
                        )
                    else:
                        if raw is not None and error.code not in {
                            "solver_empty",
                            "solver_status",
                            "solver_missing_input",
                            "solver_role_scope",
                        }:
                            raise
                        repair_payload = {
                            **material,
                            "error": error.message,
                            "instruction": "输出无法完整解析或不符合任务边界，"
                            "核对 planning_policy 与完整 facts/fact_tables 后有限重生成。"
                            "不要求唯一经营路线，选竞单目标本身属于候选决策；"
                            "不要把未赢单或未指定偏好当作全任务失败。"
                            "真实缺口仍保持未知；有依据的部分行动/建议用 partial，"
                            "无法形成任何有依据的结论才继续 missing_input。",
                        }
                        repaired = recovery_call(
                            client, replace(request, user=dumps(repair_payload)), self.control
                        )
                        data = decode_specialist(
                            obj(loads(normalize_model_json(repaired.content))),
                            snapshot,
                            candidate_actions=candidates,
                            role=role,
                        )
                self.control.checkpoint("saving_planning_progress")
                if self.memory:
                    self.memory.put(key, {"result": data})
                report_task(
                    self.control,
                    task,
                    "succeeded",
                    result_status=data["status"],
                    planning_limit=data["reason"]
                    if data["status"] in {"partial", "missing_input"}
                    else "",
                    recommendation_count=len(array(data["recommendations"])),
                )
                return {"role": material["role"], **data}
            except Cancelled:
                raise
            except ERPError as error:
                report_task(self.control, task, "failed", error=error.message)
                raise ERPError(
                    error.code,
                    f"{task.object} · {task.role}：{error.message} 已有的成功缓存会保留。",
                    error.status,
                ) from error

        independent = [(operations_task, material_for("operations"), ())]
        if context.templates:
            template_task = TaskContext(
                "solver:template_requirements",
                "Solver",
                "已核对模板需求",
                " / ".join(t.source.filename for t in context.templates),
                "solver",
            )
            independent.append((template_task, material_for("budget"), ()))
        for task, _, _ in independent:
            report_task(self.control, task, "queued")
        results = RunnableLambda(run).batch(
            independent, config={"max_concurrency": 2}, return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                raise result
        findings = [result for result in results if isinstance(result, dict)]
        operations = findings[0]
        candidates = tuple(decode_action(a) for a in array(operations["candidate_actions"]))
        report_task(self.control, finance_task, "queued", depends_on=[operations_task.id])
        finance = run((finance_task, material_for("finance", candidates), candidates))
        findings.append(finance)
        self.control.checkpoint("synthesizing_strategy")
        generator = LLMStrategyGenerator(
            TaskLLMClient(self.llm, self.control, synthesis_task),
            lambda: self.control.checkpoint("repairing_strategy"),
        )
        generator.control = self.control
        generator.collaboration = tuple(findings)
        finance_candidates = tuple(decode_action(a) for a in array(finance["candidate_actions"]))
        generator.candidate_actions = candidates + finance_candidates
        generator.defer_budget = True
        synthesis_key = (
            "solver-synthesis:"
            + hashlib.sha256(
                dumps(
                    {
                        "version": "solver-14",
                        "constraints": role_material(snapshot, "coordinator", candidates),
                        "generation": context.config,
                        "template_readings": snapshot.template_readings,
                        "findings": findings,
                        "prompt": files("erp_agent").joinpath("prompts/solver.txt").read_text(),
                    }
                ).encode()
            ).hexdigest()
        )
        saved = self.memory.get(synthesis_key) if self.memory else None
        if saved:
            proposal = decode_proposal(
                dumps(saved["proposal"]), string(saved["model"]), allow_legacy=False
            )
            report_task(self.control, synthesis_task, "cached", model=proposal.model)
        else:
            report_task(self.control, synthesis_task, "running")
            try:
                proposal = generator.propose(snapshot, context)
                if self.memory:
                    self.memory.put(
                        synthesis_key,
                        {
                            "proposal": {"plan": to_json(proposal.baseline), "parameters": []},
                            "model": proposal.model,
                        },
                    )
            except ERPError as error:
                report_task(self.control, synthesis_task, "failed", error=error.message)
                raise
        # 原分析限制已传统筹；不将原始“请用户确认”文字再追加到正式方案。
        report_task(self.control, synthesis_task, "succeeded", model=proposal.model)
        return proposal
