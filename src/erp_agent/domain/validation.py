import re
from dataclasses import replace
from decimal import Decimal

from erp_agent.domain.models import Evidence, Fact, Issue, KnowledgeState, ReadingResult
from erp_agent.domain.text import normalized_text

NUMBER = re.compile(r"(?<![A-Za-z\d_.])-?\d+(?:,\d{3})*(?:\.\d+)?\s*(%|％|万)?")
CHINESE_DIGITS = "零〇一二两三四五六七八九十百千万亿"
CHINESE_COUNT = re.compile(
    rf"(?<![{CHINESE_DIGITS}\dA-Za-z.\-])([{CHINESE_DIGITS}]+)(?=年|季|个|期|月)",
)
UNIT_MARKERS = {"percent_to_ratio": ("%", "％"), "wan_to_yuan": ("万元", "万")}
UNSPECIFIED_UNITS = {"无", "无单位", "不清楚", "不明确", "未知", "未注明", "未提供", "unknown"}
DEFAULT_HORIZON = {"years": (4, "年"), "periods_per_year": (4, "季/年")}


def _default_horizon_facts(facts: tuple[Fact, ...]) -> tuple[Fact, ...]:
    result = list(facts)
    ids = {fact.id for fact in facts}
    for key, (value, unit) in DEFAULT_HORIZON.items():
        matches = [index for index, fact in enumerate(result)
                   if fact.category == "initial" and fact.key == key]
        explanation = f"系统默认：{value} {unit}；题目另有规定时可修改。"
        if not matches:
            identifier = f"default_{key}"
            while identifier in ids:
                identifier += "_"
            ids.add(identifier)
            result.append(Fact(identifier, "initial", "company", "公司", key, str(value),
                               "integer", unit, KnowledgeState.ASSUMED, (),
                               explanation=explanation))
        elif len(matches) == 1:
            index = matches[0]
            fact = result[index]
            if fact.value is None and fact.state != KnowledgeState.CONFLICTED:
                result[index] = replace(fact, value=str(value), value_type="integer", unit=unit,
                                        state=KnowledgeState.ASSUMED, evidence_ids=(),
                                        conversion="identity", explanation=explanation)
    return tuple(result)


def source_notice(document_id: str, message: str) -> Issue:
    routine = message.startswith(
        "XLS 二进制数值按 Excel 的 15 位有效数字规范化，消除存储浮点尾差；"
    )
    return Issue("reading.source_info" if routine else "reading.source_warning",
                 "info" if routine else "warning", document_id, message)


def _text_supported(value: str, source: str) -> bool:
    value, source = normalized_text(value), normalized_text(source)
    if not value:
        return False
    pattern = (r"(?<!\d)" if value[0].isdigit() else "") + re.escape(value)
    pattern += r"(?!\d)" if value[-1].isdigit() else ""
    return bool(re.search(pattern, source))


def _text_sources_supported(
    value: str, evidence: tuple[Evidence, ...], *, header_sequence: bool = False
) -> bool:
    if any(_text_supported(value, source.text) for source in evidence):
        return True
    # 历史读题可能逐行拼接单元格；必须逐格、同表同行、按真实列序证明。
    pieces = value.split("|")
    cells = tuple({source.id: source for source in evidence}.values())
    if len(pieces) < 2 or len(pieces) != len(cells) or any(
        source.table_context is None for source in cells
    ):
        return False
    rows = {(source.document_id, source.table_context.block_index, source.table_context.row)
            for source in cells if source.table_context}
    columns = [source.table_context.column for source in cells if source.table_context]
    if len(rows) != 1 or len(set(columns)) != len(columns):
        return False
    ordered = sorted(cells, key=lambda source: source.table_context.column)
    def cell_text(source: Evidence) -> str:
        text = source.text
        if header_sequence:
            text = re.sub(r"[（(](?:元|万元|件|个|年|季|月|天|%|％)[）)]$", "", text.strip())
        return normalized_text(text)

    return all(normalized_text(piece) == cell_text(source)
               or normalized_text(piece) == normalized_text(source.text)
               for piece, source in zip(pieces, ordered, strict=True))


def distinct_issues(issues: tuple[Issue, ...]) -> tuple[Issue, ...]:
    specific_targets = {issue.target for issue in issues if issue.severity != "info"
                        and issue.code not in {"validation.uncertain", "validation.value_missing"}}
    parse_targets = {issue.target for issue in issues
                     if issue.code == "reading.invalid_number" and issue.severity != "info"}
    return tuple(dict.fromkeys(issue for issue in issues
                               if not (issue.code == "validation.uncertain"
                                       and issue.target in specific_targets)
                               and not (issue.code == "validation.value_missing"
                                        and issue.target in parse_targets)))


def _only_unspecified_unit(issue: Issue) -> bool:
    return (issue.code.split(".")[-1].lower() in {
        "ambiguous_unit", "missing_unit", "unit_missing", "unknown_unit", "unit_unknown",
    } and bool(re.search(r"未(?:明确|注明|提供|说明|列明)|不(?:清楚|明确)|未知|缺少",
                         issue.message))
        and not re.search(r"冲突|不一致|换算|倍率|百分|万元|周期", issue.message))


def validate_reading(reading: ReadingResult, evidence: tuple[Evidence, ...]) -> ReadingResult:
    sources = {e.id: e for e in evidence}
    failed_ids = {i.target for i in reading.issues if i.code == "validation.unsupported_value"}
    issues = [i for i in reading.issues if not i.code.startswith("validation.")]
    issues = [source_notice(i.target, i.message) if i.code == "reading.source_warning" else i
              for i in issues]
    unit_issues = [i for i in issues if _only_unspecified_unit(i)]
    unit_targets = {i.target for i in unit_issues}
    issues = [replace(i, code="review.resolved." + i.code, severity="info",
                      message=i.message + "；单位未注明，按无单位处理，保留原数值。")
              if i in unit_issues else i for i in issues]
    ids: set[str] = set()
    normalized: list[Fact] = []
    for fact in _default_horizon_facts(reading.problem.facts):
        current = _attach_unit_headers(fact, sources)
        unit = current.unit.strip()
        current = replace(current, unit="" if current.value_type == "text"
                          or unit.lower() in UNSPECIFIED_UNITS or (
                              current.id in unit_targets and current.conversion == "identity")
                          else unit)
        if not fact.id or fact.id in ids:
            issues.append(Issue("validation.duplicate_id", "error", fact.id,
                                "字段 ID 为空或重复。"))
        ids.add(fact.id)
        if not fact.key or not fact.entity_id:
            issues.append(Issue("validation.empty_key", "error", fact.id, "字段键或实体 ID 为空。"))
        invalid_refs = [eid for eid in fact.evidence_ids if eid not in sources]
        if invalid_refs:
            issues.append(Issue("validation.source_missing", "error", fact.id,
                                "字段引用了不存在的证据。", tuple(invalid_refs)))
            current = replace(current, state=KnowledgeState.CONFLICTED)
        if fact.value is None:
            current = replace(current, state=KnowledgeState.CONFLICTED
                              if fact.category == "initial" and fact.key in DEFAULT_HORIZON
                              and fact.state == KnowledgeState.CONFLICTED
                              else KnowledgeState.MISSING)
            acknowledged = fact.explanation.startswith("用户标注：")
            issues.append(Issue("validation.value_missing", "info" if acknowledged else "warning",
                                fact.id, fact.explanation if acknowledged else "字段值尚未提供。"))
        elif fact.state == KnowledgeState.KNOWN or (
                fact.state == KnowledgeState.CONFLICTED and fact.id in failed_ids):
            linked = tuple(sources[eid] for eid in current.evidence_ids if eid in sources)
            supported = bool(linked) and (
                _number_supported(current, linked) if fact.value_type in {"number", "integer"}
                else _text_sources_supported(
                    fact.value, linked,
                    header_sequence=fact.category == "other" and fact.key.endswith("_field_order"),
                )
            )
            if not supported:
                current = replace(current, state=KnowledgeState.CONFLICTED)
                issues.append(Issue("validation.unsupported_value", "error", fact.id,
                                    "原文证据无法支持这段文字，请核对内容与来源。"
                                    if fact.value_type == "text" else
                                    "原文证据无法支持该数值或所选转换，请核对数据格及适用表头。",
                                    current.evidence_ids))
        if current.state in {KnowledgeState.ASSUMED, KnowledgeState.CONFLICTED}:
            issues.append(Issue("validation.uncertain",
                                "info" if current.state == KnowledgeState.ASSUMED else "warning",
                                fact.id, "已采用暂定或默认值，随方案保留其口径。"
                                if current.state == KnowledgeState.ASSUMED else
                                "该字段存在尚未解决的冲突，需核对具体含义与来源。",
                                current.evidence_ids))
        normalized.append(current)
    keys: dict[tuple[str, str, str], Fact] = {}
    for fact in normalized:
        key = (fact.category, fact.entity_id, fact.key)
        if key in keys:
            issues.append(Issue("validation.duplicate_field", "error", fact.id,
                                "同一实体的字段重复，请合并来源或处理冲突。",
                                fact.evidence_ids))
        keys[key] = fact
    rule_ids: set[str] = set()
    for rule in reading.rules.rules:
        if not rule.id or rule.id in rule_ids:
            issues.append(Issue("validation.duplicate_rule", "error", rule.id,
                                "规则 ID 为空或重复。"))
        rule_ids.add(rule.id)
        if rule.state == KnowledgeState.KNOWN and (
            not rule.evidence_ids or any(eid not in sources for eid in rule.evidence_ids)
        ):
            issues.append(Issue("validation.rule_source", "error", rule.id,
                                "已知规则必须引用存在的原文证据。",
                                rule.evidence_ids))
        issues.append(Issue("validation.rule_unimplemented", "info", rule.id,
                            "规则已登记，尚未接入经营仿真；当前不能据此计算精确结果。"))
    if not any(not (fact.category == "initial" and fact.key in DEFAULT_HORIZON
                    and fact.explanation.startswith("系统默认："))
               for fact in reading.problem.facts):
        issues.append(Issue("validation.no_facts", "error", "problem",
                            "没有提取到任何规范字段。"))
    initial = [f for f in normalized if f.category == "initial"]
    if not any(f.key == "initial_cash" for f in initial):
        issues.append(Issue("validation.initial_cash", "warning", "initial_cash", "缺少初始现金。"))

    def period_value(key: str) -> int | None:
        matches = [f for f in initial if f.key == key
                   and f.state in {KnowledgeState.KNOWN, KnowledgeState.ASSUMED}
                   and f.value_type == "integer" and f.value is not None]
        if len(matches) != 1:
            return None
        value = int(Decimal(matches[0].value or "0"))
        if not 1 <= value <= 100:
            issues.append(Issue(
                "validation.period_range", "error", key, "经营周期超出允许范围。",
            ))
            return None
        return value

    years = period_value("years")
    periods = period_value("periods_per_year")
    if years is not None and periods is not None:
        period_ids = {f.id for f in initial if f.key in DEFAULT_HORIZON}
        issues = [replace(issue, severity="info", code="review.resolved." + issue.code,
                          message=issue.message + "；已按当前周期字段处理。")
                  if not issue.code.startswith(("validation.", "review.resolved."))
                  and (issue.target in period_ids or issue.target.split(".")[-1] in DEFAULT_HORIZON
                       or re.search(r"missing_periods|horizon|经营年限|经营周期|每年期数|总期数",
                                    issue.code + " " + issue.message, re.IGNORECASE))
                  and re.search(r"missing|缺|未提供|未给|需补", issue.code + " " + issue.message,
                                re.IGNORECASE)
                  and not re.search(r"conflict|冲突|不一致|矛盾", issue.code + " " + issue.message,
                                    re.IGNORECASE) else issue for issue in issues]
    if years is None or periods is None:
        issues.append(Issue("validation.horizon_missing", "warning", "horizon",
                            "经营周期存在无效值或尚未解决的冲突，请修正后生成方案。"))
    problem = replace(reading.problem, facts=tuple(normalized), years=years,
                      periods_per_year=periods)
    return replace(reading, problem=problem, issues=distinct_issues(tuple(issues)))


def _attach_unit_headers(fact: Fact, sources: dict[str, Evidence]) -> Fact:
    markers = UNIT_MARKERS.get(fact.conversion, ())
    ids = list(fact.evidence_ids)
    for eid in fact.evidence_ids:
        source = sources.get(eid)
        if not source or not source.table_context:
            continue
        for header_id in source.table_context.column_header_ids:
            header = sources.get(header_id)
            if (header and header.document_id == source.document_id
                    and any(marker in header.text for marker in markers) and header_id not in ids):
                ids.append(header_id)
    return replace(fact, evidence_ids=tuple(ids))


def _header_has_unit(source: Evidence, evidence: tuple[Evidence, ...],
                     markers: tuple[str, ...]) -> bool:
    if source.table_context:
        header_ids = set(source.table_context.column_header_ids)
        candidates = [item for item in evidence if item.id in header_ids
                      and item.document_id == source.document_id]
    else:
        candidates = [item for item in evidence if item.document_id == source.document_id
                      and item.table_context is None]
    return any(any(marker in item.text for marker in markers) for item in candidates)


def _number_supported(fact: Fact, evidence: tuple[Evidence, ...]) -> bool:
    target = Decimal(fact.value or "0")
    for source in evidence:
        for match in NUMBER.finditer(source.text):
            raw = match.group().strip()
            suffix = match.group(1)
            number = Decimal(raw.rstrip("%％万 ").replace(",", "").strip())
            if fact.conversion == "percent_to_ratio":
                if suffix == "万" or (suffix not in {"%", "％"} and not _header_has_unit(
                        source, evidence, UNIT_MARKERS[fact.conversion])):
                    continue
                number /= 100
            elif fact.conversion == "wan_to_yuan":
                if suffix in {"%", "％"} or (suffix != "万" and not _header_has_unit(
                        source, evidence, UNIT_MARKERS[fact.conversion])):
                    continue
                number *= 10000
            elif suffix in {"%", "％", "万"}:
                continue
            if number == target:
                return True
        if fact.conversion == "identity" and fact.value_type == "integer":
            for match in CHINESE_COUNT.finditer(source.text):
                number = _chinese_count(match.group(1))
                if number is not None and Decimal(number) == target:
                    return True
    return False


def _chinese_count(text: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if len(text) == 1:
        return 10 if text == "十" else digits.get(text)
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", text):
        tens, _, ones = text.partition("十")
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None
