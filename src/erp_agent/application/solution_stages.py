"""冻结行动与独立产物阶段的绑定、状态和只读投影。"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

from erp_agent.application.contracts import JSONObject, obj, to_json
from erp_agent.application.ports import Repository
from erp_agent.domain.budget import BudgetResult
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Plan, SolutionBundle


def plan_hash(plan: Plan) -> str:
    material = obj(to_json(plan))
    material.pop("budget_items", None)
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def solution_binding(
    solution: SolutionBundle, template_hashes: tuple[tuple[str, str], ...] = ()
) -> JSONObject:
    return {
        "solution_id": solution.id,
        "problem_version": solution.problem_version,
        "plan_hash": plan_hash(solution.plan),
        "template_hashes": to_json(template_hashes),
    }


def stage_record(
    repository: Repository,
    project_id: str,
    solution: SolutionBundle,
    kind: str,
    status: str,
    *,
    result: BudgetResult | JSONObject | None = None,
    error: JSONObject | None = None,
    template_hashes: tuple[tuple[str, str], ...] = (),
    attempt_id: str = "",
) -> JSONObject:
    binding = solution_binding(solution, template_hashes)
    current = optional_record(repository, kind + "_result", project_id, solution.id)
    if (
        status != "queued"
        and current
        and attempt_id
        and current.get("attempt_id") not in {"", attempt_id}
    ):
        raise ERPError("stage_attempt_changed", "阶段已开始新一次处理，旧响应不再保存。", 409)
    record = {
        **binding,
        "id": solution.id,
        "status": status,
        "attempt_id": attempt_id,
        "updated_at": datetime.now(UTC).isoformat(),
        "result": to_json(result) if result is not None else current.get("result"),
        "error": error,
    }
    repository.put(kind + "_result", project_id, solution.id, record)
    return record


def optional_record(
    repository: Repository, kind: str, project_id: str, record_id: str
) -> JSONObject:
    try:
        return repository.get(kind, project_id, record_id)
    except ERPError as error:
        if error.code == "not_found":
            return {}
        raise


def project_solution(repository: Repository, solution: SolutionBundle) -> SolutionBundle:
    record = optional_record(repository, "budget_result", solution.project_id, solution.id)
    if not record or not record.get("result"):
        return solution
    if (
        record.get("plan_hash") != plan_hash(solution.plan)
        or record.get("problem_version") != solution.problem_version
    ):
        raise ERPError("stage_binding_changed", "预算与当前行动方案不一致。", 409)
    from erp_agent.application.budget_codec import decode_budget_result

    result = decode_budget_result(obj(record["result"]), solution.plan)
    if (
        result.plan_hash != record["plan_hash"]
        or result.solution_id != solution.id
        or result.problem_version != solution.problem_version
        or to_json(result.template_hashes) != record.get("template_hashes")
    ):
        raise ERPError("stage_binding_changed", "预算结果与行动方案不一致。", 409)
    return replace(solution, plan=replace(solution.plan, budget_items=result.items))


def public_solution(repository: Repository, solution: SolutionBundle) -> JSONObject:
    value = obj(to_json(project_solution(repository, solution)))
    value["plan_hash"] = plan_hash(solution.plan)
    value["stages"] = {"actions": {"status": "draft", "verified": solution.evaluation.verified}}
    stages = obj(value["stages"])
    for kind in ("budget", "report"):
        stages[kind] = optional_record(
            repository, kind + "_result", solution.project_id, solution.id
        ) or {
            "status": "not_started"
            if kind == "report" or solution.generation.template_ids
            else "not_applicable"
        }
    return value
