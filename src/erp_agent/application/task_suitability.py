from dataclasses import dataclass

from erp_agent.application.contracts import JSONValue, loads
from erp_agent.application.llm_budget import request_input_chars
from erp_agent.application.model_dispatch import task_responsibility
from erp_agent.application.ports import LLMRequest, ModelBinding
from erp_agent.domain.errors import ERPError

MODEL_ROLES = frozenset({"reader", "operations", "finance", "coordinator", "budget", "report"})
MODEL_TIERS = frozenset({"general", "light", "strong"})


@dataclass(frozen=True)
class TaskSuitability:
    difficulty: str
    reason: str

    def rank(self, model: ModelBinding) -> int | None:
        tier = str(model.configuration.get("task_tier", "general"))
        order = (
            ("light", "general", "strong")
            if self.difficulty == "simple"
            else (("strong", "general") if self.difficulty == "complex" else ("general", "strong"))
        )
        return order.index(tier) if tier in order else None


def _has_conflict(value: JSONValue) -> bool:
    if isinstance(value, dict):
        return value.get("state") == "conflicted" or any(
            bool(item) if key in {"conflicts", "structured_conflicts"} else _has_conflict(item)
            for key, item in value.items()
            if key not in {"previous_output", "shared_memory", "task_memory"}
        )
    return isinstance(value, list) and any(_has_conflict(item) for item in value)


def classify_task(
    request: LLMRequest, simple_max_chars: int = 32000, moderate_max_chars: int = 64000
) -> TaskSuitability:
    role = task_responsibility(request)
    size = request_input_chars(request)
    if role in {"operations", "finance", "coordinator"}:
        return TaskSuitability("complex", "跨期经营规划、资金核对或全周期统筹")
    try:
        data = loads(request.user)
    except ERPError:
        data = {}
    if _has_conflict(data):
        return TaskSuitability("complex", "存在结构化来源冲突，需关联判断")
    if size > moderate_max_chars:
        return TaskSuitability(
            "complex", f"完整输入 {size} 字符超过复杂任务阈值 {moderate_max_chars}"
        )
    identity = request.schema_name + " " + (request.task.role if request.task else "")
    if request.task_complexity == "complex":
        return TaskSuitability("complex", "任务声明了复杂关联依赖")
    if request.task_complexity == "moderate" or any(
        marker in identity for marker in ("review", "复核")
    ):
        return TaskSuitability("moderate", "跨来源关联复核或工作表输入依赖")
    if size > simple_max_chars:
        return TaskSuitability(
            "moderate", f"完整输入 {size} 字符超过轻量任务阈值 {simple_max_chars}"
        )
    return TaskSuitability("simple", "有界章节/整表整理、既有来源映射或报告组织")
