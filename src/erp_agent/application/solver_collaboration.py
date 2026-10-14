import hashlib
import re
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
    integer,
    loads,
    obj,
    object_schema,
    string,
    strings,
    to_json,
)
from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.solver_context import action_material, role_material, selected_snapshot
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.action_validation import validate_actions
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Action, Plan, PlanningContext, ProblemSnapshot, StrategyProposal

DEFAULT_PLANNING_CONTEXT = PlanningContext()


def decode_specialist(
    value: JSONObject, snapshot: ProblemSnapshot, *, candidate_actions: tuple[Action, ...] = ()
) -> JSONObject:
    data = obj(value, set(SPECIALIST_SCHEMA["properties"]))
    status = string(data["status"])
    reason = string(data["reason"], 1200)
    if status not in {"applicable", "not_applicable", "missing_input", "failed"}:
        raise ERPError("solver_status", "协作任务状态无效。")
    if status in {"missing_input", "failed"}:
        raise ERPError("solver_" + status, reason or "任务未提供可用结果。")
    if status == "not_applicable" and not reason.strip():
        raise ERPError("solver_status", "不适用结果必须明确说明原因。")
    proposed_items = []
    for index, value in enumerate(array(data["candidate_actions"])):
        try:
            proposed_items.append(decode_action(value))
        except ERPError as error:
            raise ERPError(
                error.code, f"candidate_actions.{index}：{error.message}", error.status
            ) from error
    proposed = tuple(proposed_items)
    all_actions = candidate_actions + proposed
    plan = Plan("协作候选", "", all_actions, (), ())
    diagnostics = (
        validate_actions(snapshot, plan, coverage=False, ordering=False) if all_actions else ()
    )
    if diagnostics:
        rendered = []
        for issue in diagnostics[:8]:
            parts = issue.target.split(".")
            target = issue.target
            if len(parts) > 1 and parts[0] == "actions" and parts[1].isdigit():
                local_index = int(parts[1]) - len(candidate_actions)
                if local_index >= 0:
                    target = ".".join(["candidate_actions", str(local_index), *parts[2:]])
            rendered.append({**to_json(issue), "target": target})
        raise ERPError("solver_action", dumps(rendered))
    facts = {f.id for f in snapshot.reading.problem.facts}
    rules = {r.id for r in snapshot.reading.rules.rules}
    entities = {f.entity_id for f in snapshot.reading.problem.facts} | {"company"}
    gaps = {f"{i.code}:{i.target}" for i in snapshot.reading.issues}
    identifiers = {a.action_id for a in all_actions}
    horizon = (snapshot.reading.problem.years or 4) * (
        snapshot.reading.problem.periods_per_year or 4
    )
    counts = {name: len(array(data[name])) for name in ("recommendations", "dependencies", "risks")}
    if counts["recommendations"] > 24 or counts["dependencies"] > 16 or counts["risks"] > 16:
        raise ERPError("solver_limit", "协作结果超过当前职责的结论数量上限。")
    if status == "applicable" and not (proposed or any(counts.values())):
        raise ERPError("solver_empty", "空结果必须说明不适用、缺少输入或失败，不能标为有效建议。")
    if status == "not_applicable" and (proposed or any(counts.values())):
        raise ERPError("solver_status", "不适用结果不能同时提供行动或结论。")
    for name in ("recommendations", "dependencies", "risks"):
        expected = set(obj(SPECIALIST_SCHEMA["properties"])[name]["items"]["properties"])
        seen = set()
        for index, item in enumerate(array(data[name])):
            entry = obj(item, expected)
            target = f"{name}.{index}"
            for key, allowed in (
                ("fact_ids", facts),
                ("rule_ids", rules),
                ("gap_ids", gaps),
                ("action_ids", identifiers),
            ):
                for identifier in strings(entry.get(key, [])):
                    if identifier not in allowed:
                        raise ERPError(
                            "solver_source", f"{target}.{key} 引用未知 ID {identifier}。"
                        )
            if "entity_id" in entry and string(entry["entity_id"]) not in entities:
                raise ERPError("solver_source", f"{target}.entity_id 引用未知实体。")
            for period in array(entry.get("periods", [])):
                integer(period, 1, horizon)
            if "id" in entry:
                identifier = string(entry["id"])
                if not identifier.strip() or identifier in seen:
                    raise ERPError("solver_source", f"{target}.id 为空或重复。")
                seen.add(identifier)
            if name == "dependencies":
                dependent = string(entry["action_id"])
                required = string(entry["requires_action_id"])
                if (
                    dependent not in identifiers
                    or required not in identifiers
                    or dependent == required
                ):
                    raise ERPError("solver_source", f"{target} 的行动依赖无效。")
                if entry["required_period"] is not None:
                    integer(entry["required_period"], 1, horizon)
            if name == "recommendations" and not string(entry["explanation"]).strip():
                raise ERPError("solver_source", f"{target}.explanation 为空。")
            if name == "risks":
                condition = obj(entry["condition"])
                allowed = {"fact": facts, "rule": rules, "action": identifiers, "gap": gaps}
                kind = string(condition.get("kind"))
                if kind != "always" and (
                    kind not in allowed or condition.get("reference_id") not in allowed[kind]
                ):
                    raise ERPError("solver_source", f"{target}.condition 引用无效。")
                string(entry["response"])
    return data


def specialist_patch(
    raw: JSONObject, error: ERPError
) -> tuple[JSONObject, JSONObject, str, int] | None:
    match = re.search(r"(recommendations|dependencies|risks)\.(\d+)", error.message)
    if match:
        collection, index = match.group(1), int(match.group(2))
    else:
        match = re.search(r"candidate_actions\.(\d+)", error.message)
        if not match:
            return None
        collection, index = "candidate_actions", int(match.group(1))
    entries = array(raw.get(collection, []))
    if index >= len(entries):
        return None
    entry = obj(entries[index])
    properties = obj(SPECIALIST_SCHEMA["properties"])
    entry_schema = obj(properties[collection])["items"]
    schema = object_schema(
        {
            "collection": {"type": "string", "const": collection},
            "index": {"type": "integer", "const": index},
            "replacement": entry_schema,
        }
    )
    return {"collection": collection, "index": index, "object": entry}, schema, collection, index


def apply_specialist_patch(
    raw: JSONObject, value: JSONObject, collection: str, index: int
) -> JSONObject:
    entry = obj(value, {"collection", "index", "replacement"})
    if entry["collection"] != collection or entry["index"] != index:
        raise ERPError("patch_scope", "协作补丁不能修改其它对象。")
    original = obj(array(raw[collection])[index])
    replacement = obj(entry["replacement"])
    identity = "action_id" if collection in {"candidate_actions", "dependencies"} else "id"
    if replacement.get(identity) != original.get(identity):
        raise ERPError("patch_scope", "协作补丁不能改写对象 ID。")
    updated = list(array(raw[collection]))
    updated[index] = replacement
    return {**raw, collection: updated}


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
                material["template_requirements"] = to_json(snapshot.template_readings)
            return material

        def run(unit: tuple[TaskContext, JSONObject, tuple[Action, ...]]) -> JSONObject:
            task, material, candidates = unit
            request = LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps(material),
                "erp_solver_specialist",
                schema,
                max_output_tokens=6000,
                reasoning_reserve_tokens=8192,
                stream=True,
                retries=0,
                task=task,
            )
            key = (
                "solver-unit:"
                + hashlib.sha256(
                    dumps({"version": "solver-9", "request": request}).encode()
                ).hexdigest()
            )
            saved = self.memory.get(key) if self.memory else None
            if saved and "result" in saved:
                data = decode_specialist(
                    obj(saved["result"]), snapshot, candidate_actions=candidates
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
                    data = decode_specialist(raw, snapshot, candidate_actions=candidates)
                except ERPError as error:
                    if error.code in {"solver_missing_input", "solver_failed"}:
                        raise
                    raw = locals().get("raw")
                    patch = specialist_patch(raw, error) if raw is not None else None
                    if patch:
                        scoped, patch_schema, collection, index = patch
                        focused = {
                            key: material[key]
                            for key in ("project_id", "problem_version", "rule_version", "role")
                        }
                        focused.update(
                            {
                                "error": error.message,
                                "object": scoped,
                                "reference_catalog": {
                                    "fact_ids": [f.id for f in snapshot.reading.problem.facts],
                                    "entity_ids": list(
                                        {f.entity_id for f in snapshot.reading.problem.facts}
                                        | {"company"}
                                    ),
                                    "rule_ids": [r.id for r in snapshot.reading.rules.rules],
                                    "action_ids": [a.action_id for a in candidates]
                                    + [
                                        obj(a)["action_id"] for a in array(raw["candidate_actions"])
                                    ],
                                },
                                "instruction": "仅修复当前对象，"
                                "不修改其它结论、事实、版本或行动 ID。",
                            }
                        )
                        repair_request = replace(
                            request,
                            user=dumps(focused),
                            schema_name="erp_specialist_patch",
                            schema_json=dumps(patch_schema),
                            system="只返回当前对象的受限修正。\nJSON schema:\n"
                            + dumps(patch_schema),
                        )
                        repaired = recovery_call(client, repair_request, self.control)
                        patched = apply_specialist_patch(
                            raw,
                            obj(loads(normalize_model_json(repaired.content))),
                            collection,
                            index,
                        )
                        data = decode_specialist(patched, snapshot, candidate_actions=candidates)
                    else:
                        if raw is not None and error.code not in {"solver_empty", "solver_status"}:
                            raise
                        repair_payload = {
                            **material,
                            "error": error.message,
                            "instruction": "输出无法完整解析或不符合任务边界，"
                            "有限重生成符合 schema 的本任务结果。",
                        }
                        repaired = recovery_call(
                            client, replace(request, user=dumps(repair_payload)), self.control
                        )
                        data = decode_specialist(
                            obj(loads(normalize_model_json(repaired.content))),
                            snapshot,
                            candidate_actions=candidates,
                        )
                self.control.checkpoint("saving_planning_progress")
                if self.memory:
                    self.memory.put(key, {"result": data})
                report_task(
                    self.control,
                    task,
                    "succeeded",
                    recommendation_count=len(array(data["recommendations"])),
                )
                return {"role": material["role"], **data}
            except Cancelled:
                raise
            except ERPError as error:
                report_task(self.control, task, "failed", error=error.message)
                raise ERPError(
                    error.code,
                    f"{task.object} · {task.role}：{error.message} 已完成任务已保存。",
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
        generator.candidate_actions = candidates
        generator.defer_budget = True
        synthesis_key = (
            "solver-synthesis:"
            + hashlib.sha256(
                dumps(
                    {
                        "version": "solver-9",
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
        report_task(self.control, synthesis_task, "succeeded", model=proposal.model)
        return proposal
