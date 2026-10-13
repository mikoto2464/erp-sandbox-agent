import io
from decimal import Decimal
from zipfile import ZipFile

from erp_agent.application.contracts import dumps
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.domain.models import (
    Evaluation, Fact, KnowledgeState, Plan, ProblemSnapshot, ProblemSpec, ReadingResult, RuleSet,
)


def docx_bytes(xml: str | None = None, extras: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    xml = xml or '<w:p><w:r><w:t>初始资本100000元，经营四年，每年4季。</w:t></w:r></w:p>'
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml",
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         f'<w:body>{xml}</w:body></w:document>')
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
        if request.schema_name == "erp_solver_specialist":
            return LLMResponse(dumps({"recommendations": [], "dependencies": [], "risks": []}),
                               "test-model")
        return LLMResponse(self.response, "test-model")


def snapshot() -> ProblemSnapshot:
    problem = ProblemSpec("测试", (Fact("cash", "initial", "company", "公司", "initial_cash",
                                      "100000", "number", "元", KnowledgeState.KNOWN, ()),), 1, 2)
    return ProblemSnapshot("a" * 32, 1, "2026-10-11", ("b" * 32,),
                           ReadingResult(problem, RuleSet(()), (), "test-model"))


def proposal_json() -> str:
    return dumps({"plan": {"title": "测试计划", "summary": "两期计划", "actions": [
        {"period": 1, "kind": "reserve", "entity_id": "company", "amount": "10",
         "quantity": None, "description": "第一期"},
        {"period": 2, "kind": "review", "entity_id": "company", "amount": None,
         "quantity": None, "description": "第二期"},
    ], "assumptions": [], "risks": []}, "parameters": [
        {"action_index": 0, "field": "amount", "minimum": "0", "maximum": "20", "step": "1"},
    ]})


class QuadraticEvaluator:
    supports_scoring = True

    def evaluate(self, snapshot: ProblemSnapshot, plan: Plan) -> Evaluation:
        value = plan.actions[0].amount if plan.actions[0].amount is not None else Decimal(0)
        return Evaluation(value <= 18, True, -((value - 15) ** 2), (), "手算抛物线测试")
