from erp_agent.application.contracts import JSONObject
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError

MAX_RESPONSE_CHARS = 16 * 1024 * 1024


def validate_completion(metadata: JSONObject, usage: JSONObject, settings: Settings) -> None:
    reason = metadata.get("stop_reason", metadata.get("finish_reason", metadata.get("done_reason")))
    status = metadata.get("status")
    if isinstance(status, str) and status in {"incomplete", "failed", "cancelled"}:
        details = metadata.get("incomplete_details")
        reason = (details.get("reason") if isinstance(details, dict) else None) or status
    if metadata.get("done") is False:
        reason = "incomplete"
    feedback = metadata.get("prompt_feedback", {})
    blocked = isinstance(feedback, dict) and feedback.get("block_reason")
    if blocked or reason in ("refusal", "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"):
        raise ERPError("llm_refusal", "模型拒绝生成结果。", 502)
    if reason in (None, "stop", "end_turn", "tool_use", "tool_calls", "STOP", "completed"):
        return
    known = {"length", "max_tokens", "max_output_tokens", "MAX_TOKENS", "stop_sequence",
             "pause_turn", "content_filter", "incomplete", "failed", "cancelled"}
    label = reason if isinstance(reason, str) and reason in known else "未知结束状态"
    truncated = label in {"length", "max_tokens", "max_output_tokens", "MAX_TOKENS"}
    message = (f"模型输出被截断（{label}）。" if truncated
               else f"模型未返回完整结果（{label}）。")
    message += f" 本次输出预算 {settings.llm_max_output_tokens} Token。"
    output = usage.get("output_tokens")
    details = usage.get("output_token_details")
    reasoning = details.get("reasoning") if isinstance(details, dict) else None
    if isinstance(output, int) and not isinstance(output, bool) and output >= 0:
        message += f" 接口报告输出 {output} Token。"
    if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
        message += f" 思考用量 {reasoning} Token。"
    message += ("可关闭思考或调高输出预算后重新读题；截断结果未保存。" if truncated
                else "请检查接口返回状态，当前不自动续轮。")
    raise ERPError("llm_incomplete", message, 502)
