from importlib.resources import files

from langchain_core.runnables import RunnableLambda

from erp_agent.application.contracts import (
    ISSUE_SCHEMA,
    JSONObject,
    array,
    decode_reading,
    dumps,
    loads,
    obj,
    object_schema,
    to_json,
)
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.reading.merge import ROW_CATEGORIES, normalized_name
from erp_agent.application.task_budget import task_budget
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Evidence, Fact, Issue, KnowledgeState, ReadingResult, Rule


class LinkReviewRole:
    def __init__(self) -> None:
        resources = files("erp_agent").joinpath("prompts")
        self.schema = dumps(object_schema({"issues": {"type": "array", "items": ISSUE_SCHEMA}}))
        self.system = (
            resources.joinpath("reader.txt").read_text(encoding="utf-8")
            + "\n"
            + resources.joinpath("reader_review.txt").read_text(encoding="utf-8")
            + "\nJSON schema:\n"
            + self.schema
        )

    def review(
        self,
        reading: ReadingResult,
        evidence: tuple[Evidence, ...],
        client: LLMClient,
        control: RunControl,
        max_chars: int,
        completed: int,
        review_groups: dict[str, str] | None = None,
        concurrency: int = 1,
    ) -> tuple[Issue, ...]:
        sources = {e.id: e for e in evidence}
        context_ids = {
            ref
            for e in evidence
            if e.table_context
            for ref in e.table_context.heading_ids + e.table_context.column_header_ids
        }
        parse_errors = {
            i.target
            for i in reading.issues
            if i.code == "reading.invalid_number" and i.severity == "error"
        }
        parameters = tuple(
            f
            for f in reading.problem.facts
            if f.category not in ROW_CATEGORIES and f.id not in parse_errors
        )
        units: list[tuple[Fact | Rule, ...]] = []
        disputed = {i.target for i in reading.issues if i.severity != "info"}

        def needs_review(values: tuple[Fact | Rule, ...]) -> bool:
            if review_groups is None:
                return True
            groups = {
                review_groups[eid]
                for value in values
                for eid in value.evidence_ids
                if eid in review_groups and eid not in context_ids
            }
            return len(groups) > 1 or any(
                value.id in disputed
                or value.state in {KnowledgeState.CONFLICTED, KnowledgeState.MISSING}
                for value in values
            )

        entities: dict[tuple[str, str], list[Fact]] = {}
        for fact in parameters:
            entities.setdefault((fact.category, fact.entity_id), []).append(fact)
        for entity_facts in entities.values():
            fact = entity_facts[0]
            related = tuple(
                r
                for r in reading.rules.rules
                if normalized_name(fact.entity_name)
                and normalized_name(fact.entity_name)
                in normalized_name(r.name + r.expression + r.explanation)
            )
            values = tuple(entity_facts) + related
            if needs_review(values):
                units.append(values)
        for rule in reading.rules.rules:
            related = tuple(
                f
                for f in parameters
                if normalized_name(f.entity_name)
                and normalized_name(f.entity_name)
                in normalized_name(rule.name + rule.expression + rule.explanation)
            )
            values = (rule,) + related
            if needs_review(values) and not any(set(values) <= set(unit) for unit in units):
                units.append(values)
        if not units:
            return ()

        def payload(values: tuple[Fact | Rule, ...]) -> JSONObject:
            ids = {eid for item in values for eid in item.evidence_ids}
            return {
                "role": "review",
                "scope": "关联子集，不代表全题",
                "facts": to_json(tuple(f for f in values if isinstance(f, Fact))),
                "rules": to_json(tuple(r for r in values if isinstance(r, Rule))),
                "existing_issues": to_json(
                    tuple(i for i in reading.issues if i.target in {v.id for v in values})
                ),
                "evidence": [
                    [eid, sources[eid].text, sources[eid].location]
                    for eid in sorted(ids)
                    if eid in sources
                ],
            }

        def run(
            values: tuple[Fact | Rule, ...], parent: TaskContext | None = None
        ) -> tuple[Issue, ...]:
            material = payload(values)
            allowed_ids = {v.id for v in values}
            allowed_refs = {eid for v in values for eid in v.evidence_ids}
            task = TaskContext(
                "review:" + ":".join(sorted(allowed_ids)),
                "Reader",
                "关联复核",
                "、".join(
                    dict.fromkeys(v.entity_name if isinstance(v, Fact) else v.name for v in values)
                )[:250],
                parent.id if parent else "reader",
            )
            task_budget(control, task)
            if len(self.system) + len(dumps(material)) + 1500 > max_chars:
                if len(values) > 1:
                    middle = len(values) // 2
                    return run(values[:middle], task) + run(values[middle:], task)
                raise ERPError(
                    "reading_review_limit", "单条规则及其来源超过关联复核上限，请提高读题输入上限。"
                )
            task_client = TaskLLMClient(client, control, task)
            control.checkpoint("reading_review", completed)
            response = None
            for attempt in range(2):
                try:
                    response = task_client.generate(
                        LLMRequest(
                            self.system,
                            dumps(material),
                            "erp_reading_review",
                            self.schema,
                            max_output_tokens=2000,
                            retries=0,
                        )
                    )
                    control.checkpoint("reading_review", completed)
                    root = obj(loads(normalize_model_json(response.content)), {"issues"})
                    decoded = decode_reading(
                        dumps(
                            {
                                "title": "复核",
                                "facts": [],
                                "rules": [],
                                "issues": array(root["issues"]),
                            }
                        ),
                        response.model,
                    )
                    if any(
                        i.target not in allowed_ids
                        or any(eid not in allowed_refs for eid in i.evidence_ids)
                        for i in decoded.issues
                    ):
                        raise ERPError(
                            "reading_review_source", "复核疑点未指向当前条目或引用了范围外来源。"
                        )
                    report_task(
                        control,
                        task,
                        "needs_input" if decoded.issues else "succeeded",
                        issue_count=len(decoded.issues),
                        model=response.model,
                    )
                    return decoded.issues
                except Cancelled:
                    raise
                except ERPError as error:
                    if (
                        error.code
                        in {
                            "llm_incomplete",
                            "llm_timeout",
                            "llm_gateway_timeout",
                            "llm_unavailable",
                        }
                        and len(values) > 1
                    ):
                        report_task(control, task, "split", error=error.message)
                        middle = len(values) // 2
                        return run(values[:middle], task) + run(values[middle:], task)
                    if attempt or (response is None and error.code != "llm_structured_response"):
                        if error.code in {
                            "invalid_contract",
                            "invalid_json",
                            "reading_review_source",
                            "llm_structured_response",
                        }:
                            message = (
                                f"关联复核未完成（{error.message}），"
                                "请核对这些条目的含义、适用对象与原文关联。"
                            )
                            finding = Issue(
                                "reading.review_unresolved",
                                "error",
                                values[0].id,
                                message,
                                tuple(sorted(allowed_refs)),
                            )
                            report_task(control, task, "needs_input", error=message)
                            return (finding,)
                        report_task(control, task, "failed", error=error.message)
                        raise
                    control.checkpoint("repairing_reading", completed)
                    material["error"] = error.message[:1000]
                    material["instruction"] = "只返回 issues 数组，目标与证据必须属于当前关联子集。"
                    if len(self.system) + len(dumps(material)) > max_chars:
                        raise ERPError(
                            "reading_review_limit", "关联复核修复请求超过输入上限。"
                        ) from error

            return ()

        batches: list[tuple[Fact | Rule, ...]] = []
        current: tuple[Fact | Rule, ...] = ()
        for unit in units:
            candidate = tuple(dict.fromkeys(current + unit))
            if current and (
                len(candidate) > 30
                or len(self.system) + len(dumps(payload(candidate))) + 1500 > max_chars
            ):
                batches.append(current)
                current = ()
            current = tuple(dict.fromkeys(current + unit))
        if current:
            batches.append(current)
        runner = RunnableLambda(run)
        results = runner.batch(
            batches, config={"max_concurrency": max(1, min(concurrency, 4))}, return_exceptions=True
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise next(
                (error for error in failures if not isinstance(error, Cancelled)), failures[0]
            )
        return tuple(
            dict.fromkeys(
                issue for result in results if not isinstance(result, Exception) for issue in result
            )
        )
