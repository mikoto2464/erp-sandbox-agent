"""按真实经营周期约束生成边界，持久化仍使用平面行动契约。"""

import re
from copy import deepcopy

from erp_agent.application.contracts import (
    ACTION_SCHEMA,
    PROPOSAL_SCHEMA,
    JSONObject,
    array,
    obj,
    object_schema,
)
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import Plan


def period_proposal_schema(horizon: int) -> JSONObject:
    if horizon < 1:
        return PROPOSAL_SCHEMA
    schema = deepcopy(PROPOSAL_SCHEMA)
    action = deepcopy(ACTION_SCHEMA)
    action["properties"].pop("period")
    action["required"].remove("period")
    for name in ("completion_period", "payment_period"):
        action["properties"][name]["maximum"] = horizon
    review = deepcopy(action)
    review["properties"]["kind"] = {"type": "string", "const": "review"}
    for name in ("completion_period", "payment_period"):
        review["properties"][name] = {"type": "null"}
    action["properties"]["kind"]["enum"].remove("review")
    schema["$defs"] = {"period_action": {"anyOf": [action, review]}}
    plan = schema["properties"]["plan"]
    schema["properties"]["parameters"]["maxItems"] = 0
    plan["properties"]["budget_items"]["maxItems"] = 0
    plan["properties"].pop("actions")
    plan["required"].remove("actions")
    plan["properties"]["period_actions"] = object_schema(
        {
            str(period): {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/period_action"},
            }
            for period in range(1, horizon + 1)
        }
    )
    plan["required"].append("period_actions")
    return schema


def flatten_period_proposal(data: JSONObject, horizon: int) -> JSONObject:
    plan = obj(data.get("plan"))
    if "period_actions" not in plan:
        # 只规范本次生成的检查元数据，不改历史快照或业务动作的提前期。
        if isinstance(plan.get("actions"), list):
            return {
                **data,
                "plan": {
                    **plan,
                    "actions": [_review_timing(obj(action)) for action in array(plan["actions"])],
                },
            }
        return data
    if "actions" in plan:
        raise ERPError("invalid_contract", "不能同时输出平面行动与按期行动。")
    periods = obj(plan["period_actions"])
    expected = {str(period) for period in range(1, horizon + 1)}
    if set(periods) != expected:
        missing = sorted(expected - set(periods), key=int)
        outside = sorted(set(periods) - expected)
        raise ERPError("invalid_contract", f"按期行动目录不完整：漏期 {missing}，越界 {outside}。")
    actions = []
    for period in range(1, horizon + 1):
        values = array(periods[str(period)])
        if not values:
            raise ERPError("invalid_contract", f"经营期 {period} 没有具体行动。")
        for value in values:
            action = obj(value)
            if "period" in action and action["period"] != period:
                raise ERPError("invalid_contract", f"行动期间与经营期 {period} 分组不一致。")
            actions.append(_review_timing({**action, "period": period}))
    flattened = {key: value for key, value in plan.items() if key != "period_actions"}
    return {**data, "plan": {**flattened, "actions": actions}}


def _review_timing(action: JSONObject) -> JSONObject:
    if action.get("kind") == "review":
        return {**action, "completion_period": None, "payment_period": None}
    return action


CONFIRMATION = re.compile(
    r"(?<!无)(?<!不)(?<!没有)(?<!不再)(?:"
    r"待(?:用户)?确认|请(?:用户)?(?:核对并)?确认|"
    r"(?:需要|需|须)(?:进一步)?确认|"
    r"(?:需要|要求|等待|须|需)(?:用户|人工)(?:确认|补充|提供)|"
    r"请(?:补充|提供)[^。\n]*(?:参数|资料|数据)|等待用户)"
)


def check_planning_content(plan: Plan) -> None:
    texts = [plan.summary, *plan.assumptions, *plan.risks]
    for action in plan.actions:
        texts.extend((action.description, action.missing_reason, action.condition.explanation))
    if any(CONFIRMATION.search(text) for text in texts):
        raise ERPError(
            "strategy_confirmation",
            "方案生成阶段不能请求用户确认或补充数据。请自主选择规划，并将未知项改为明确假设、"
            "策略边界或自动执行条件；不能把假设改成赛题事实。",
        )
