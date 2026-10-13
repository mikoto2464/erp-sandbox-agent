import hashlib
from dataclasses import replace
from decimal import Decimal

from erp_agent.application.contracts import dumps, editable_reading
from erp_agent.application.reading.roles import ReadingPart
from erp_agent.domain.models import (
    Fact,
    Issue,
    KnowledgeState,
    ProblemSpec,
    ReadingResult,
    Rule,
    RuleSet,
)
from erp_agent.domain.text import normalized_text as normalized_name
from erp_agent.domain.validation import UNSPECIFIED_UNITS

ROW_CATEGORIES = {"bom", "demand_forecast", "order_pool", "order_contract"}


def identifier(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:20]


def _signature(fact: Fact) -> tuple[object, ...]:
    value = fact.value
    if value is not None:
        value = Decimal(value) if fact.value_type != "text" else normalized_name(value)
    return value, fact.value_type == "text", fact.unit


class ReadingMerger:
    def merge(self, parts: tuple[ReadingPart, ...]) -> ReadingResult:
        facts: dict[tuple[str, str, str], Fact] = {}
        rules: dict[tuple[str, str], Rule] = {}
        issues: list[Issue] = []
        for part in parts:
            remap: dict[str, str] = {}
            focused = set(part.batch.focus_ids)
            evidence = {e.id: e for e in part.batch.document.evidence}
            row_entities: dict[str, str] = {}
            fact_rows: dict[Fact, str] = {}
            for fact in part.result.problem.facts:
                if fact.category not in ROW_CATEGORIES:
                    continue
                headers = {header for eid in fact.evidence_ids if eid in evidence
                           and evidence[eid].table_context
                           for header in evidence[eid].table_context.column_header_ids}
                rows = sorted({(evidence[eid].table_context.block_index,
                                evidence[eid].table_context.row)
                               for eid in fact.evidence_ids if eid in focused - headers
                               and eid in evidence and evidence[eid].table_context})
                if rows:
                    name = (f"{part.batch.document.source.id}:{rows[0][0]}:{rows[0][1]}:"
                            + normalized_name(fact.entity_name))
                    fact_rows[fact] = name
                    row_entities.setdefault(fact.entity_id, name)
            for fact in part.result.problem.facts:
                if fact.category in ROW_CATEGORIES:
                    name = fact_rows.get(fact, row_entities.get(fact.entity_id,
                        part.batch.document.source.id + ":" + fact.entity_id))
                    entity = identifier(fact.category + "_", name)
                elif fact.category == "initial":
                    entity = "company"
                else:
                    entity = identifier(fact.category + "_", normalized_name(fact.entity_name)
                                        or part.batch.document.source.id + ":" + fact.entity_id)
                key = (fact.category, entity, fact.key)
                fid = identifier("fact_", ":".join(key))
                remap[fact.id] = fid
                unit = "" if fact.unit.strip().lower() in UNSPECIFIED_UNITS else fact.unit.strip()
                current = replace(fact, id=fid, entity_id=entity, unit=unit)
                previous = facts.get(key)
                if previous is None:
                    facts[key] = current
                    continue
                refs = tuple(dict.fromkeys(previous.evidence_ids + current.evidence_ids))
                explanations = "；".join(dict.fromkeys(filter(None, (
                    previous.explanation, current.explanation))))
                if (previous.state != KnowledgeState.CONFLICTED
                        and current.state != KnowledgeState.CONFLICTED):
                    if previous.value is None and current.value is not None:
                        facts[key] = replace(current, evidence_ids=refs, explanation=explanations)
                        continue
                    if previous.value is not None and current.value is None:
                        facts[key] = replace(previous, evidence_ids=refs, explanation=explanations)
                        continue
                if _signature(previous) == _signature(current):
                    state = (KnowledgeState.CONFLICTED if KnowledgeState.CONFLICTED in
                             {previous.state, current.state} else
                             KnowledgeState.KNOWN if KnowledgeState.KNOWN in
                             {previous.state, current.state} else previous.state)
                    facts[key] = replace(previous, evidence_ids=refs, state=state,
                                         explanation=explanations)
                else:
                    message = (f"{current.entity_name} · {current.key} 的不同来源给出"
                               f"“{previous.value} {previous.unit}”与"
                               f"“{current.value} {current.unit}”，"
                               "应采用哪一个值，是否有明确的补充覆盖依据？")
                    facts[key] = replace(previous, evidence_ids=refs,
                        state=KnowledgeState.CONFLICTED,
                        explanation="；".join(filter(None, (explanations, message))))
                    issues.append(Issue("reading.source_conflict", "warning", fid,
                                        message, refs))
            for rule in part.result.rules.rules:
                key = (normalized_name(rule.name), normalized_name(rule.expression))
                rid = identifier("rule_", ":".join(key))
                remap[rule.id] = rid
                previous = rules.get(key)
                rules[key] = replace(rule, id=rid, evidence_ids=tuple(dict.fromkeys(
                    (previous.evidence_ids if previous else ()) + rule.evidence_ids)))
            for issue in part.result.issues:
                issues.append(replace(issue, target=remap.get(issue.target, issue.target)))
        unique_issues = tuple(dict.fromkeys(issues))
        title = next((p.result.problem.title for p in parts if p.result.problem.title.strip()),
                     "ERP 赛题")
        model = ", ".join(dict.fromkeys(p.result.model for p in parts))
        result = ReadingResult(ProblemSpec(title, tuple(facts.values())),
                               RuleSet(tuple(rules.values())), unique_issues, model,
                               prompt_version="reader-16")
        return replace(result, raw_response=dumps(editable_reading(result)))
