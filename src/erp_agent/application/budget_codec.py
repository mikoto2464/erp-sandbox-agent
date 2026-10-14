from erp_agent.application.contracts import (
    JSONObject,
    array,
    integer,
    obj,
    string,
    strings,
)
from erp_agent.domain.budget import BudgetGap, BudgetResult, BudgetTarget
from erp_agent.domain.models import BudgetItem, Plan


def decode_budget_result(data: JSONObject, plan: Plan) -> BudgetResult:
    data = obj(data)
    items = []
    for raw in array(data["items"]):
        item = obj(raw)
        indices = tuple(
            integer(i, 0, max(0, len(plan.actions) - 1))
            for i in array(item.get("action_indices", []))
        )
        if any(i >= len(plan.actions) for i in indices):
            from erp_agent.domain.errors import ERPError

            raise ERPError("budget.action", "历史预算引用未知行动。")
        items.append(
            BudgetItem(
                string(item["template_id"]),
                string(item["sheet"]),
                string(item["cell"]),
                string(item["label"]),
                None if item["period"] is None else integer(item["period"]),
                string(item["unit"]),
                None if item["value"] is None else string(item["value"]),
                indices,
                string(item["explanation"]),
                strings(item.get("action_ids", []))
                or tuple(plan.actions[i].action_id for i in indices),
                strings(item.get("source_ids", [])),
                string(item.get("conversion", "identity")),
                None if item.get("gap_id") is None else string(item["gap_id"]),
            )
        )
    gaps = tuple(
        BudgetGap(
            string(g["id"]),
            string(g["code"]),
            string(g["message"]),
            tuple(
                BudgetTarget(string(t["template_id"]), string(t["sheet"]), string(t["cell"]))
                for t in map(obj, array(g["targets"]))
            ),
            strings(g.get("source_ids", [])),
            string(g.get("rule_id", "")),
        )
        for g in map(obj, array(data["gaps"]))
    )
    return BudgetResult(
        tuple(items),
        gaps,
        string(data["status"]),
        string(data["solution_id"]),
        integer(data["problem_version"]),
        tuple(tuple(strings(pair)) for pair in array(data["template_hashes"])),
        string(data["plan_hash"]),
        tuple(tuple(strings(pair)) for pair in array(data.get("completed_sheets", []))),
        tuple(tuple(strings(pair)) for pair in array(data.get("failed_sheets", []))),
    )
