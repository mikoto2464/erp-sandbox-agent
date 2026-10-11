import re
from dataclasses import replace
from decimal import Decimal

from erp_agent.domain.models import Evidence, Fact, Issue, KnowledgeState, ReadingResult

NUMBER = re.compile(r"(?<![A-Za-z\d_.])-?\d+(?:,\d{3})*(?:\.\d+)?\s*(%|％|万)?")
CHINESE_DIGITS = "零〇一二两三四五六七八九十百千万亿"
CHINESE_COUNT = re.compile(
    rf"(?<![{CHINESE_DIGITS}\dA-Za-z.\-])([{CHINESE_DIGITS}]+)(?=年|季|个|期|月)",
)


def validate_reading(reading: ReadingResult, evidence: tuple[Evidence, ...]) -> ReadingResult:
    sources = {e.id: e for e in evidence}
    issues = [i for i in reading.issues if not i.code.startswith("validation.")]
    ids: set[str] = set()
    normalized: list[Fact] = []
    for fact in reading.problem.facts:
        current = fact
        if not fact.id or fact.id in ids:
            issues.append(Issue("validation.duplicate_id", "error", fact.id, "字段 ID 为空或重复。"))
        ids.add(fact.id)
        if not fact.key or not fact.entity_id:
            issues.append(Issue("validation.empty_key", "error", fact.id, "字段键或实体 ID 为空。"))
        invalid_refs = [eid for eid in fact.evidence_ids if eid not in sources]
        if invalid_refs:
            issues.append(Issue("validation.source_missing", "error", fact.id,
                                "字段引用了不存在的证据。", tuple(invalid_refs)))
            current = replace(current, state=KnowledgeState.CONFLICTED)
        if fact.value is None:
            current = replace(current, state=KnowledgeState.MISSING)
            issues.append(Issue("validation.value_missing", "warning", fact.id, "字段值尚未提供。"))
        elif fact.state == KnowledgeState.KNOWN:
            linked = tuple(sources[eid] for eid in fact.evidence_ids if eid in sources)
            supported = bool(linked) and (
                _number_supported(fact, linked) if fact.value_type in {"number", "integer"}
                else any(fact.value in e.text for e in linked)
            )
            if not supported:
                current = replace(current, state=KnowledgeState.CONFLICTED)
                issues.append(Issue("validation.unsupported_value", "error", fact.id,
                                    "原文证据无法支持该值及其单位转换。", fact.evidence_ids))
        if current.state in {KnowledgeState.ASSUMED, KnowledgeState.CONFLICTED}:
            issues.append(Issue("validation.uncertain", "warning", fact.id,
                                "该字段仍是显式假设或冲突，使用前需复核。", fact.evidence_ids))
        normalized.append(current)
    keys: dict[tuple[str, str, str], Fact] = {}
    for fact in normalized:
        key = (fact.category, fact.entity_id, fact.key)
        if key in keys:
            issues.append(Issue("validation.duplicate_field", "error", fact.id,
                                "同一实体的字段重复，请合并来源或处理冲突。", fact.evidence_ids))
        keys[key] = fact
    rule_ids: set[str] = set()
    for rule in reading.rules.rules:
        if not rule.id or rule.id in rule_ids:
            issues.append(Issue("validation.duplicate_rule", "error", rule.id, "规则 ID 为空或重复。"))
        rule_ids.add(rule.id)
        if rule.state == KnowledgeState.KNOWN and (
            not rule.evidence_ids or any(eid not in sources for eid in rule.evidence_ids)
        ):
            issues.append(Issue("validation.rule_source", "error", rule.id,
                                "已知规则必须引用存在的原文证据。", rule.evidence_ids))
        issues.append(Issue("validation.rule_unimplemented", "info", rule.id,
                            "规则已登记，尚未接入经营仿真；当前不能据此计算精确结果。"))
    if not normalized:
        issues.append(Issue("validation.no_facts", "error", "problem", "没有提取到任何规范字段。"))
    initial = [f for f in normalized if f.category == "initial"]
    if not any(f.key == "initial_cash" for f in initial):
        issues.append(Issue("validation.initial_cash", "warning", "initial_cash", "缺少初始现金。"))

    def period_value(key: str) -> int | None:
        matches = [f for f in initial if f.key == key and f.state == KnowledgeState.KNOWN
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
    if years is None or periods is None:
        issues.append(Issue("validation.horizon_missing", "warning", "horizon",
                            "经营年数或每年期数尚未确认；不能检查完整逐季计划。"))
    problem = replace(reading.problem, facts=tuple(normalized), years=years, periods_per_year=periods)
    return replace(reading, problem=problem, issues=tuple(issues))


def _number_supported(fact: Fact, evidence: tuple[Evidence, ...]) -> bool:
    target = Decimal(fact.value or "0")
    percentage_header = any("%" in e.text or "％" in e.text for e in evidence)
    for source in evidence:
        for match in NUMBER.finditer(source.text):
            raw = match.group().strip()
            suffix = match.group(1)
            number = Decimal(raw.rstrip("%％万 ").replace(",", "").strip())
            if fact.conversion == "percent_to_ratio":
                if suffix not in {"%", "％"} and not percentage_header:
                    continue
                number /= 100
            elif fact.conversion == "wan_to_yuan":
                if suffix != "万":
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
