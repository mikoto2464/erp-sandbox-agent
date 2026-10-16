"""职责结论的定位校验与确定性引用归位，不丢弃无效建议。"""

from erp_agent.application.contracts import JSONObject, array, obj, string, strings
from erp_agent.domain.calculations import CalculationRecord
from erp_agent.domain.errors import ERPError


def validate_analysis(
    data: JSONObject,
    context_hash: str,
    allowed: set[str],
    loaded: set[str],
    calculations: dict[str, CalculationRecord],
) -> JSONObject:
    if data.get("context_hash") != context_hash:
        code = "planning.context" if "context_hash" in data else "planning.structure"
        raise ERPError(code, "context_hash：职责分析与冻结题目绑定不一致。")
    errors: list[tuple[str, str]] = []

    def issue(code: str, path: str, message: str) -> None:
        errors.append((code, f"{path}：{message}"))

    def text(entry: JSONObject, key: str, path: str, maximum: int, nonempty: bool) -> None:
        try:
            value = string(entry[key], maximum)
            if nonempty and not value.strip():
                raise ERPError("planning.analysis", "说明不能为空。")
        except KeyError:
            issue("planning.structure", path, "缺少字段。")
        except ERPError as error:
            issue(error.code, path, error.message)

    def refs(entry: JSONObject, key: str, path: str, optional: bool = False) -> tuple[str, ...]:
        try:
            return strings(entry.get(key, []) if optional else entry[key])
        except KeyError:
            issue("planning.structure", path, "缺少字段。")
        except ERPError as error:
            issue(error.code, path, error.message)
        return ()

    def names(ids: set[str]) -> str:
        return ", ".join(v[:100] for v in sorted(ids)[:8])

    text(data, "summary", "summary", 1600, True)
    try:
        entries = array(data["findings"])
    except (ERPError, KeyError) as error:
        raise ERPError("planning.structure", "findings：必须提供职责结论数组。") from error
    if len(entries) > 12:
        raise ERPError("planning.analysis", "findings：最多返回12项职责结论。")
    result = []
    for index, raw in enumerate(entries):
        path = f"findings[{index}]"
        try:
            entry = obj(raw)
        except ERPError as error:
            issue(error.code, path, error.message)
            continue
        text(entry, "statement", path + ".statement", 1200, True)
        text(entry, "implication", path + ".implication", 1200, False)
        calc_ids = list(refs(entry, "calculation_ids", path + ".calculation_ids", True))
        unknown = set(calc_ids) - calculations.keys()
        if unknown:
            issue(
                "planning.reference",
                path + ".calculation_ids",
                "不存在的计算 ID：" + names(unknown),
            )
        references = []
        unknown_refs = set()
        for ref in refs(entry, "references", path + ".references"):
            if ref in allowed:
                references.append(ref)
            elif ref in calculations:
                # 工具返回的真实计算 ID 归入专用字段，保留其来源链。
                calc_ids.append(ref)
            else:
                unknown_refs.add(ref)
        if unknown_refs:
            issue(
                "planning.reference",
                path + ".references",
                "当前题目不存在的引用：" + names(unknown_refs),
            )
        for calc_id in dict.fromkeys(calc_ids):
            if calc_id not in calculations:
                continue
            record = calculations[calc_id]
            if record.context_hash != context_hash:
                raise ERPError(
                    "planning.context", path + ".calculation_ids：计算记录属于其它题目或版本。"
                )
            references.extend(record.fact_ids)
            if record.rule_id:
                references.append(record.rule_id)
        unknown = set(references) - allowed
        if unknown:
            issue(
                "planning.reference",
                path + ".references",
                "计算来源不属于当前题目：" + names(unknown),
            )
        unread = set(references) & allowed - loaded
        if unread:
            issue(
                "planning.not_loaded",
                path + ".references",
                "尚未读取的引用，请按 ID 查询：" + names(unread),
            )
        result.append(
            {
                **entry,
                "references": list(dict.fromkeys(references)),
                "calculation_ids": list(dict.fromkeys(calc_ids)),
            }
        )
    if errors:
        raise ERPError(errors[0][0], "；".join(message for _, message in errors[:24]))
    return {**data, "findings": result}
