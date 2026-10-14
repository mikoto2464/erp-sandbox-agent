import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from erp_agent.domain.models import Plan, ProblemSpec, RuleSet

NUMERALS = "零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟"
NUMERIC_CLAIM = re.compile(
    rf"[0-9０-９]|(?:百分之|千分之|万分之)[{NUMERALS}]+|"
    rf"[{NUMERALS}]+\s*(?:元|件|人|条|季|年|月|天|日|小时|台|套|吨|公斤|个|成|倍|%|％|点)|"
    rf"第[{NUMERALS}]+(?:期|季|年|月)|[{NUMERALS}]+分之[{NUMERALS}]+"
)


@dataclass(frozen=True)
class ReportNote:
    title: str
    explanation: str
    action_indices: tuple[int, ...] = ()
    fact_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    budget_indices: tuple[int, ...] = ()
    period: int | None = None
    condition: str = ""
    response: str = ""


@dataclass(frozen=True)
class ReportOutline:
    strategy: tuple[ReportNote, ...] = ()
    milestones: tuple[ReportNote, ...] = ()
    risks: tuple[ReportNote, ...] = ()


def validate_report_outline(
    outline: ReportOutline,
    plan: "Plan",
    problem: "ProblemSpec",
    rules: "RuleSet",
) -> tuple[str, ...]:
    errors: list[str] = []
    fact_ids = {fact.id for fact in problem.facts}
    rule_ids = {rule.id for rule in rules.rules}
    horizon = (problem.years or 4) * (problem.periods_per_year or 4)
    for section in ("strategy", "milestones", "risks"):
        for index, note in enumerate(getattr(outline, section)):
            target = f"{section}[{index}]"
            if not note.title.strip() or not note.explanation.strip():
                errors.append(f"{target} 缺少标题或说明。")
            if not (note.action_indices or note.fact_ids or note.rule_ids or note.budget_indices):
                errors.append(f"{target} 缺少方案或题目引用。")
            if any(
                type(i) is not int or not 0 <= i < len(plan.actions) for i in note.action_indices
            ):
                errors.append(f"{target} 引用了当前方案以外的行动。")
            if any(
                type(i) is not int or not 0 <= i < len(plan.budget_items)
                for i in note.budget_indices
            ):
                errors.append(f"{target} 引用了当前方案以外的预算。")
            if any(i not in fact_ids for i in note.fact_ids):
                errors.append(f"{target} 引用了不存在的题目参数。")
            if any(i not in rule_ids for i in note.rule_ids):
                errors.append(f"{target} 引用了不存在的题目规则。")
            if note.period is not None and (
                type(note.period) is not int or not 1 <= note.period <= horizon
            ):
                errors.append(f"{target} 的期间超出经营周期。")
            entities = {
                plan.actions[i].entity_id
                for i in note.action_indices
                if type(i) is int and 0 <= i < len(plan.actions)
            }
            names = {
                fact.entity_name
                for fact in problem.facts
                if fact.id in note.fact_ids or fact.entity_id in entities
            }
            prose = " ".join((note.title, note.explanation, note.condition, note.response))
            for name in sorted(names, key=len, reverse=True):
                if name and not re.fullmatch(rf"[0-9０-９{NUMERALS}]+", name):
                    prose = prose.replace(name, "")
            if NUMERIC_CLAIM.search(prose):
                errors.append(f"{target} 说明中另写了数值或期间，请用引用及 period 展示。")
    return tuple(errors)
