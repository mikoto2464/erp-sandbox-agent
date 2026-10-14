import io
from decimal import Decimal
from zipfile import ZipFile

from erp_agent.application.contracts import dumps
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.domain.models import (
    Evaluation,
    Fact,
    KnowledgeState,
    Plan,
    ProblemSnapshot,
    ProblemSpec,
    ReadingResult,
    RuleSet,
)


def docx_bytes(xml: str | None = None, extras: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    xml = xml or "<w:p><w:r><w:t>初始资本100000元，经营四年，每年4季。</w:t></w:r></w:p>"
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{xml}</w:body></w:document>",
        )
        for name, data in (extras or {}).items():
            archive.writestr(name, data)
    return output.getvalue()


class Control:
    def __init__(self) -> None:
        self.stages: list[str] = []

    def checkpoint(self, stage: str, completed: int = 0) -> None:
        self.stages.append(stage)


class FakeLLM:
    configured = True

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if request.schema_name == "erp_report_outline":
            from erp_agent.application.contracts import loads

            payload = loads(request.user)
            note = {
                "title": "按当前方案执行",
                "explanation": "依据已有行动安排经营，保留待验证条件。",
                "action_indices": [0],
                "fact_ids": [],
                "rule_ids": [],
                "budget_indices": [],
                "period": None,
                "condition": "执行条件尚未满足",
                "response": "核对现有安排后执行。",
            }
            return LLMResponse(
                dumps(
                    {
                        "strategy": [note] if payload["role"] == "strategy" else [],
                        "milestones": [note] if payload["role"] == "strategy" else [],
                        "risks": [note] if payload["role"] == "execution" else [],
                    }
                ),
                "test-model",
            )
        if request.schema_name == "erp_solver_specialist":
            return LLMResponse(
                dumps(
                    {
                        "status": "not_applicable",
                        "reason": "此受控任务无额外建议。",
                        "candidate_actions": [],
                        "recommendations": [],
                        "dependencies": [],
                        "risks": [],
                    }
                ),
                "test-model",
            )
        if request.schema_name == "erp_solver_budget":
            from erp_agent.application.contracts import loads

            payload = loads(request.user)
            model_fixture = loads(self.response)
            budget = model_fixture.get("plan", {}).get("budget_items", [])
            mappings, gaps = [], []
            for group in payload["input_groups"]:
                matching = [
                    item
                    for item in budget
                    if item["sheet"] == payload["sheet"] and item["cell"] in group["range"]
                ]
                item = matching[0] if matching else None
                indices = item["action_indices"] if item else [0]
                refs = [
                    f"action:{payload['actions'][i]['action_id']}:amount"
                    for i in indices
                    if i < len(payload["actions"]) and payload["actions"][i]["amount"] is not None
                ]
                gap = None if refs else "fixture-missing"
                if gap and not gaps:
                    gaps.append(
                        {
                            "id": gap,
                            "code": "missing",
                            "message": "受控夹具无对应预算来源。",
                            "source_ids": [],
                            "rule_id": "",
                        }
                    )
                mappings.append(
                    {
                        "group_id": group["id"],
                        "target_range": group["range"],
                        "source_ids": refs,
                        "period_axis": "single",
                        "periods": [item["period"] if item else 1],
                        "conversion": "identity",
                        "calculation": "direct",
                        "gap_id": gap,
                    }
                )
            return LLMResponse(
                dumps({"status": "applicable", "mappings": mappings, "gaps": gaps}), "test-model"
            )
        if request.schema_name == "erp_strategy" and "budget_items必须为空" in request.system:
            from erp_agent.application.contracts import loads
            from erp_agent.application.model_output import normalize_model_json

            try:
                payload = loads(normalize_model_json(self.response))
            except Exception:
                return LLMResponse(self.response, "test-model")
            request_payload = loads(request.user)
            problem = request_payload.get("snapshot", {}).get("problem", {})
            fact_rows = problem.get("facts", [])
            fact_ids = [row[0] for row in fact_rows]
            fact_ids += [
                identifier
                for table in problem.get("fact_tables", [])
                for row in table.get("rows", [])
                for identifier in row[3]
            ]
            payload["plan"]["budget_items"] = []
            for index, action in enumerate(payload["plan"]["actions"]):
                if action.get("schema_version") == "2":
                    action["fact_ids"] = fact_ids[:1]
                    continue
                # 受控夹具声明明确动作，保留旧快照夹具用于兼容检查。
                action.update(
                    structured_action(
                        index=index,
                        period=action["period"],
                        amount=action["amount"],
                        description=action["description"],
                    )
                )
                action["fact_ids"] = fact_ids[:1]
            returned = dumps(payload)
            if self.response.startswith("```json"):
                returned = "```json\n" + returned + "\n```"
            return LLMResponse(returned, "test-model")
        return LLMResponse(self.response, "test-model")


def snapshot() -> ProblemSnapshot:
    problem = ProblemSpec(
        "测试",
        (
            Fact(
                "cash",
                "initial",
                "company",
                "公司",
                "initial_cash",
                "100000",
                "number",
                "元",
                KnowledgeState.KNOWN,
                (),
            ),
        ),
        1,
        2,
    )
    return ProblemSnapshot(
        "a" * 32,
        1,
        "2026-10-11",
        ("b" * 32,),
        ReadingResult(problem, RuleSet(()), (), "test-model"),
    )


def proposal_json() -> str:
    return dumps(
        {
            "plan": {
                "title": "测试计划",
                "summary": "两期计划",
                "actions": [
                    {
                        "period": 1,
                        "kind": "reserve",
                        "entity_id": "company",
                        "amount": "10",
                        "quantity": None,
                        "description": "第一期",
                    },
                    {
                        "period": 2,
                        "kind": "review",
                        "entity_id": "company",
                        "amount": None,
                        "quantity": None,
                        "description": "第二期",
                    },
                ],
                "assumptions": [],
                "risks": [],
            },
            "parameters": [
                {
                    "action_index": 0,
                    "field": "amount",
                    "minimum": "0",
                    "maximum": "20",
                    "step": "1",
                },
            ],
        }
    )


class QuadraticEvaluator:
    supports_scoring = True

    def evaluate(self, snapshot: ProblemSnapshot, plan: Plan) -> Evaluation:
        value = plan.actions[0].amount if plan.actions[0].amount is not None else Decimal(0)
        return Evaluation(value <= 18, True, -((value - 15) ** 2), (), "手算抛物线测试")


def structured_action(*, index=0, period=1, amount=None, description="检查经营条件"):
    return {
        "action_id": f"action-{index}",
        "schema_version": "2",
        "period": period,
        "kind": "payment" if amount is not None else "review",
        "entity_id": "company",
        "amount": amount,
        "quantity": None,
        "description": description,
        "quantity_unit": "",
        "amount_unit": "元" if amount is not None else "",
        "amount_meaning": "payment" if amount is not None else "none",
        "related_entity_ids": [],
        "depends_on": [],
        "fact_ids": ["cash"],
        "rule_ids": [],
        "gap_ids": [],
        "completion_period": None,
        "payment_period": None,
        "missing_reason": "无需数量，尚未仿真。",
        "condition": {
            "kind": "always",
            "reference_id": None,
            "operator": None,
            "value": None,
            "explanation": "",
        },
        "details": {"finance_entity_id": "company", "obligation_action_id": None}
        if amount is not None
        else {"target_entity_id": "company"},
    }


def structured_proposal_json():
    from erp_agent.application.contracts import loads

    payload = loads(proposal_json())
    payload["plan"]["actions"] = [
        structured_action(index=0, period=1, amount="10"),
        structured_action(index=1, period=2),
    ]
    payload["plan"]["budget_items"] = []
    payload["parameters"] = []
    return dumps(payload)
