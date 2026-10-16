"""可恢复的任务问题。问题与答复属于当前任务，不修改冻结题目。"""

from erp_agent.application.contracts import JSONObject
from erp_agent.domain.errors import ERPError


class HumanInputRequired(ERPError):
    def __init__(self, question: JSONObject) -> None:
        super().__init__("human_input_required", "任务等待选择处理方案。", 409)
        self.question = question


class PlanningProblem(ERPError):
    def __init__(self, code: str, message: str, task_id: str, details: JSONObject | None = None) -> None:
        super().__init__(code, message)
        self.task_id = task_id
        self.details = details or {}
