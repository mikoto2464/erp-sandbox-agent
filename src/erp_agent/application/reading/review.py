from importlib.resources import files

from erp_agent.application.contracts import (
    ISSUE_SCHEMA, JSONObject, array, decode_reading, dumps, loads, obj, object_schema, to_json,
)
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl
from erp_agent.application.reading.merge import ROW_CATEGORIES, normalized_name
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Evidence, Fact, Issue, ReadingResult, Rule


class LinkReviewRole:
    def __init__(self) -> None:
        resources = files("erp_agent").joinpath("prompts")
        self.schema = dumps(object_schema({"issues": {"type": "array", "items": ISSUE_SCHEMA}}))
        self.system = (resources.joinpath("reader.txt").read_text(encoding="utf-8") + "\n" +
                       resources.joinpath("reader_review.txt").read_text(encoding="utf-8") +
                       "\nJSON schema:\n" + self.schema)

    def review(self, reading: ReadingResult, evidence: tuple[Evidence, ...], client: LLMClient,
               control: RunControl, max_chars: int, completed: int) -> tuple[Issue, ...]:
        sources = {e.id: e for e in evidence}
        parameters = tuple(f for f in reading.problem.facts if f.category not in ROW_CATEGORIES)
        units: list[tuple[Fact | Rule, ...]] = []
        for fact in parameters:
            related = tuple(r for r in reading.rules.rules if normalized_name(fact.entity_name)
                            and normalized_name(fact.entity_name) in
                            normalized_name(r.name + r.expression + r.explanation))
            units.append((fact,) + related[:4])
        for rule in reading.rules.rules:
            related = tuple(f for f in parameters if normalized_name(f.entity_name)
                            and normalized_name(f.entity_name) in
                            normalized_name(rule.name + rule.expression + rule.explanation))
            units.append((rule,) + related[:12])
        if not units:
            return ()
        issues: list[Issue] = []

        def payload(values: tuple[Fact | Rule, ...]) -> JSONObject:
            ids = {eid for item in values for eid in item.evidence_ids}
            return {"role": "review", "scope": "关联子集，不代表全题",
                    "facts": to_json(tuple(f for f in values if isinstance(f, Fact))),
                    "rules": to_json(tuple(r for r in values if isinstance(r, Rule))),
                    "evidence": [[eid, sources[eid].text, sources[eid].location]
                                 for eid in sorted(ids) if eid in sources]}

        def run(values: tuple[Fact | Rule, ...]) -> None:
            material = payload(values)
            allowed_ids = {v.id for v in values}
            allowed_refs = {eid for v in values for eid in v.evidence_ids}
            if len(self.system) + len(dumps(material)) + 1500 > max_chars:
                if len(values) > 1:
                    middle = len(values) // 2
                    run(values[:middle])
                    run(values[middle:])
                    return
                raise ERPError("reading_review_limit", "单条规则及其来源超过关联复核上限，请提高读题输入上限。")
            control.checkpoint("reading_review", completed)
            response = None
            for attempt in range(2):
                try:
                    response = client.generate(LLMRequest(self.system, dumps(material),
                                                          "erp_reading_review", self.schema))
                    control.checkpoint("reading_review", completed)
                    root = obj(loads(normalize_model_json(response.content)), {"issues"})
                    decoded = decode_reading(dumps({"title": "复核", "facts": [], "rules": [],
                                                    "issues": array(root["issues"])}), response.model)
                    if any(i.target not in allowed_ids or any(eid not in allowed_refs
                           for eid in i.evidence_ids) for i in decoded.issues):
                        raise ERPError("reading_review_source", "复核疑点未指向当前条目或引用了范围外来源。")
                    issues.extend(decoded.issues)
                    return
                except Cancelled:
                    raise
                except ERPError as error:
                    if error.code == "llm_incomplete" and len(values) > 1:
                        middle = len(values) // 2
                        run(values[:middle])
                        run(values[middle:])
                        return
                    if attempt or (response is None and error.code != "llm_structured_response"):
                        raise
                    control.checkpoint("repairing_reading", completed)
                    material["error"] = error.message[:1000]
                    material["instruction"] = "只返回 issues 数组，目标与证据必须属于当前关联子集。"
                    if len(self.system) + len(dumps(material)) > max_chars:
                        raise ERPError("reading_review_limit", "关联复核修复请求超过输入上限。") from error
        current: tuple[Fact | Rule, ...] = ()
        for unit in units:
            candidate = tuple(dict.fromkeys(current + unit))
            if current and (len(candidate) > 30 or len(self.system) + len(dumps(payload(candidate))) + 1500 > max_chars):
                run(current)
                current = ()
            current = tuple(dict.fromkeys(current + unit))
        if current:
            run(current)
        return tuple(dict.fromkeys(issues))
