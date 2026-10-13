import re
import unicodedata

from erp_agent.application.contracts import JSONObject, array, decimal, obj, string
from erp_agent.domain.errors import ERPError


def recover_numbers(data: JSONObject) -> None:
    """仅恢复模型数值字段；用户修改和求解契约继续严格校验。"""
    issues = array(data["issues"])
    for item in array(data["facts"]):
        fact = obj(item)
        kind = fact.get("value_type")
        if (
            not isinstance(kind, str)
            or kind not in {"number", "integer"}
            or fact.get("value") is None
        ):
            continue
        raw = fact["value"]
        try:
            text = unicodedata.normalize("NFKC", string(raw)).strip()
            if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?", text):
                text = text.replace(",", "")
            value = decimal(text)
            if fact["value_type"] == "integer" and value != value.to_integral_value():
                raise ERPError("invalid_contract", "整数条目包含小数。")
            fact["value"] = format(value, "f")
        except ERPError as error:
            message = (
                f"{fact.get('entity_name', '')} · {fact.get('key', '')}："
                f"模型提取值 {str(raw)[:200]!r} 无法作为有效数值使用（{error.message}）。"
                "请查看标出的原文，填写正确值；若实际是编号、日期或文字，请修改数据类型；"
                "无法确定时可标记不清楚。"
            )
            fact.update(value=None, state="missing")
            fact["explanation"] = "\n".join(
                filter(None, (str(fact.get("explanation", "")), message))
            )
            issues.append(
                {
                    "code": "reading.invalid_number",
                    "severity": "error",
                    "target": fact.get("id", ""),
                    "message": message,
                    "evidence_ids": fact.get("evidence_ids", []),
                }
            )
