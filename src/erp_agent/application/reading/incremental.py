from dataclasses import replace

from erp_agent.application.contracts import dumps, editable_reading
from erp_agent.application.reading.merge import ReadingMerger
from erp_agent.domain.models import Issue, KnowledgeState, ParsedDocument, ReadingResult
from erp_agent.domain.validation import validate_reading


def merge_incremental(
    previous: ReadingResult, added: ReadingResult, documents: tuple[ParsedDocument, ...]
) -> ReadingResult:
    facts = tuple(
        f
        for f in previous.problem.facts
        if not (
            f.category == "initial"
            and f.key in {"years", "periods_per_year"}
            and f.explanation.startswith("系统默认：")
        )
    )
    old = replace(previous, problem=replace(previous.problem, facts=facts))
    added = replace(
        added,
        problem=replace(
            added.problem,
            facts=tuple(
                f for f in added.problem.facts if not f.explanation.startswith("系统默认：")
            ),
        ),
    )
    # 合并器只为新增实体生成 ID；已保存数据保持 ID 以保留用户确认及方案引用。
    existing = {(f.category, f.entity_id, f.key): f for f in old.problem.facts}
    issues = [i for i in previous.issues if not i.code.startswith("validation.")]
    remap: dict[str, str] = {}
    for fact in added.problem.facts:
        key = (fact.category, fact.entity_id, fact.key)
        original = existing.get(key)
        if not original:
            existing[key] = fact
            continue
        remap[fact.id] = original.id
        existing[key], issue = ReadingMerger.merge_fact(original, fact)
        if issue:
            issues.append(issue)
    issues.extend(replace(i, target=remap.get(i.target, i.target))
                  for i in added.issues if not i.code.startswith("validation."))
    rules = {r.id: r for r in old.rules.rules}
    for rule in added.rules.rules:
        existing_rule = rules.get(rule.id)
        if not existing_rule:
            rules[rule.id] = rule
            continue
        refs = tuple(dict.fromkeys(existing_rule.evidence_ids + rule.evidence_ids))
        conflict = existing_rule.expression != rule.expression
        rules[rule.id] = replace(
            existing_rule, evidence_ids=refs,
            state=KnowledgeState.CONFLICTED if conflict else existing_rule.state,
        )
        if conflict:
            issues.append(Issue("reading.source_conflict", "warning", rule.id,
                f"{rule.name} 的规则有不同表达：{existing_rule.expression}；{rule.expression}。"
                "请说明适用口径或覆盖依据。", refs))
    result = replace(
        old,
        problem=replace(old.problem, facts=tuple(existing.values())),
        rules=replace(old.rules, rules=tuple(rules.values())),
        issues=tuple(dict.fromkeys(issues)),
        model=", ".join(dict.fromkeys((old.model, added.model))),
        prompt_version=added.prompt_version,
    )
    checked = validate_reading(result, tuple(e for d in documents for e in d.evidence))
    return replace(checked, raw_response=dumps(editable_reading(checked)))
