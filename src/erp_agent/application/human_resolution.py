"""可恢复的任务问题。问题与答复属于当前任务，不修改冻结题目。"""

from erp_agent.application.contracts import JSONObject
from erp_agent.domain.errors import ERPError


class HumanInputRequired(ERPError):
    def __init__(self, question: JSONObject) -> None:
        super().__init__("human_input_required", "任务等待选择处理方案。", 409)
        self.question = question


class PlanningProblem(ERPError):
    def __init__(
        self, code: str, message: str, task_id: str, details: JSONObject | None = None
    ) -> None:
        super().__init__(code, message)
        self.task_id = task_id
        self.details = details or {}


class PlanningValidationError(ERPError):
    def __init__(self, code: str, message: str, issues: tuple[JSONObject, ...]) -> None:
        super().__init__(code, message)
        self.issues = issues


def resolution_content(error: ERPError) -> JSONObject:
    """用户选择业务处理方式，原始字段诊断仅供展开查看及模型修正。"""
    details = dict(error.details) if isinstance(error, PlanningProblem) else {}
    if isinstance(error, PlanningValidationError):
        details["validation_issues"] = list(error.issues)
    issues = details.get("validation_issues", [])
    reference_error = error.code in {
        "planning.reference",
        "planning.references",
        "planning.not_loaded",
    } or any(
        issue.get("code") in {"planning.reference", "planning.references", "planning.not_loaded"}
        for issue in issues
    )
    result = {
        "title": "方案生成需要你的处理",
        "message": "当前任务生成的结果未通过检查，其它已完成结果已保留。",
        "help_text": "可以让系统修正当前任务；如果你有具体经营要求，也可以补充说明。",
        "input_label": "补充说明（选填）",
        "input_placeholder": "可以留空，直接选择继续；也可补充希望如何调整经营方案。",
        "technical_details": error.message[:5000],
        "details": details,
        "reference_error": reference_error,
    }
    if reference_error:
        result.update(
            title="AI生成的操作未能关联题目依据",
            message="部分操作使用的依据编号无法匹配当前题目，系统需要重新核对其来源。",
            help_text="这是生成时的资料关联问题，不代表题目一定缺少资料。"
            "你不需要查找或填写 fact 编号。建议直接选择重新核对；只有想补充经营口径时才填写说明。",
            input_placeholder="无需填写编号。例如：高级技工按约定月薪逐季发放；也可留空。",
        )
        if error.code == "planning.not_loaded":
            result.update(
                title="操作依据尚未读取完成",
                message="部分操作尚未读取其引用的题目资料，需要补取原始依据后重新核对。",
            )
    elif error.code == "planning.cash_unit":
        result.update(
            message="部分付款的金额单位无法与分段限额比较，请核对下方金额及单位。",
            help_text="确认单位意味着数值不变；如果还需要计算或调整金额，请填写说明并让系统重新处理。",
        )
    elif error.code == "planning.cash_limit":
        result.update(
            message="本段计划的现金支出超过路线预留额度，需要调整本段投入安排。",
            help_text="可以直接让系统重新安排，也可说明优先保留哪些经营投入；现金与限额检查仍会保留。",
        )
    elif error.code in {"task_call_limit", "task_deadline"}:
        result.update(
            message="本次生成的调用额度或执行时间已用完，尚未完成的方案没有保存为成功结果。",
            help_text="请选择重新开始生成或取消；不需要填写技术参数，成功分析可从缓存复用。",
        )
    affected = []
    seen = set()
    for issue in issues:
        identity = (issue.get("period"), issue.get("key"))
        if identity in seen or not issue.get("description"):
            continue
        seen.add(identity)
        affected.append(
            {k: issue[k] for k in ("period_label", "description", "entity_name") if k in issue}
        )
    if affected:
        details["affected_actions"] = affected[:24]
    return result
