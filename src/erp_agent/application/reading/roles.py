from dataclasses import dataclass, replace
from importlib.resources import files

from erp_agent.application.contracts import (
    FACT_SCHEMA,
    ISSUE_SCHEMA,
    RULE_SCHEMA,
    TEXT,
    JSONObject,
    decode_reading,
    dumps,
    loads,
    obj,
    object_schema,
    to_json,
)
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl
from erp_agent.application.reading.batches import ReadingBatch, oversized_batch
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Fact, ReadingResult, RuleSet


@dataclass(frozen=True)
class ReaderLimits:
    max_request_chars: int = 12_000
    max_batch_units: int = 4
    concurrency: int = 2

    def __post_init__(self) -> None:
        if (
            self.max_request_chars <= 0
            or self.max_batch_units <= 0
            or not 1 <= self.concurrency <= 4
        ):
            raise ValueError("读题请求上限和批次大小必须为正数。")


@dataclass(frozen=True)
class ReadingPart:
    role: str
    batch: ReadingBatch
    result: ReadingResult


class ExtractionRole:
    def __init__(self, name: str) -> None:
        if name not in {"facts", "rules"}:
            raise ValueError("未知读题提取子角色。")
        self.name = name
        resources = files("erp_agent").joinpath("prompts")
        shared = resources.joinpath("reader.txt").read_text(encoding="utf-8")
        role = resources.joinpath(f"reader_{name}.txt").read_text(encoding="utf-8")
        self.schema = dumps(
            object_schema(
                {
                    "title": TEXT,
                    name: {
                        "type": "array",
                        "items": FACT_SCHEMA if name == "facts" else RULE_SCHEMA,
                    },
                    "issues": {"type": "array", "items": ISSUE_SCHEMA},
                }
            )
        )
        self.system = shared + "\n" + role

    def extract(
        self,
        batch: ReadingBatch,
        client: LLMClient,
        control: RunControl,
        max_chars: int,
        completed: int,
        facts: tuple[Fact, ...] = (),
        memory: RoleMemory | None = None,
    ) -> ReadingPart:
        payload: JSONObject = {
            "role": self.name,
            "documents": [batch.payload()],
            "focus_evidence_ids": list(batch.focus_ids),
        }
        if memory:
            payload["memory"] = to_json(memory.recall(dumps(batch.payload())[:2000], 800))
        system_size = len(self.system) + len(self.schema) + 16
        if facts:
            payload["local_parameters"] = [
                {
                    "id": f.id,
                    "category": f.category,
                    "entity_id": f.entity_id,
                    "entity_name": f.entity_name,
                    "key": f.key,
                    "value": f.value,
                    "unit": f.unit,
                    "evidence_ids": list(f.evidence_ids),
                }
                for f in facts
            ]
        if system_size + len(dumps(payload)) > max_chars:
            # Parameters are an aid; the rule extractor still receives the original source.
            payload.pop("local_parameters", None)
        if system_size + len(dumps(payload)) > max_chars:
            raise oversized_batch(batch)
        control.checkpoint(f"reading_{self.name}", completed)
        response = None
        try:
            response = client.generate(
                LLMRequest(
                    self.system,
                    dumps(payload),
                    "erp_reading",
                    self.schema,
                    max_output_tokens=8000 if self.name == "facts" else 4000,
                    retries=0,
                )
            )
            control.checkpoint(f"reading_{self.name}", completed)
            result = self._decode(response.content, response.model, batch)
        except Cancelled:
            raise
        except ERPError as error:
            if response is None and error.code != "llm_structured_response":
                raise
            control.checkpoint("repairing_reading", completed)
            payload["error"] = error.message[:1200]
            payload["instruction"] = "仅修正当前批次的 JSON、契约或来源错误，保持当前子角色职责。"
            previous = response.content if response else None
            if previous is None:
                payload["previous_output"] = None
            elif system_size + len(dumps({**payload, "previous_output": previous})) <= max_chars:
                payload["previous_output"] = previous
            else:
                payload["instruction"] = "前次输出过大；仅重新提取当前批次，保持当前子角色职责。"
            if system_size + len(dumps(payload)) > max_chars:
                raise oversized_batch(batch) from error
            response = client.generate(
                LLMRequest(
                    self.system,
                    dumps(payload),
                    "erp_reading",
                    self.schema,
                    max_output_tokens=8000 if self.name == "facts" else 4000,
                    retries=0,
                )
            )
            control.checkpoint(f"reading_{self.name}", completed)
            result = self._decode(response.content, response.model, batch)
        if self.name == "facts":
            other_ids = {r.id for r in result.rules.rules}
            result = replace(
                result,
                rules=RuleSet(()),
                issues=tuple(i for i in result.issues if i.target not in other_ids),
            )
        else:
            other_ids = {f.id for f in result.problem.facts}
            result = replace(
                result,
                problem=replace(result.problem, facts=()),
                issues=tuple(i for i in result.issues if i.target not in other_ids),
            )
        return ReadingPart(self.name, batch, result)

    def _decode(self, content: str, model: str, batch: ReadingBatch) -> ReadingResult:
        data = obj(loads(normalize_model_json(content)))
        expected = {"title", self.name, "issues"}
        if set(data) not in (expected, {"title", "facts", "rules", "issues"}):
            raise ERPError("invalid_contract", "读题子角色输出字段不符合契约。")
        # Accept historic full-contract fixtures; production schema contains only this role.
        data = {
            "title": data.get("title", ""),
            "facts": data.get("facts", []) if self.name == "facts" else [],
            "rules": data.get("rules", []) if self.name == "rules" else [],
            "issues": data.get("issues", []),
        }
        result = decode_reading(dumps(data), model)
        document = batch.payload()
        allowed = set(batch.focus_ids)
        context = document["context_evidence"]
        assert isinstance(context, list)
        allowed.update(str(row[0]) for row in context if isinstance(row, list))
        allowed.update(e.id for note in batch.notes for e in note.evidence)
        values = result.problem.facts if self.name == "facts" else result.rules.rules
        for value in values:
            if any(eid not in allowed for eid in value.evidence_ids):
                raise ERPError("reading_batch_source", "提取条目引用了当前批次以外的原文来源。")
        for issue in result.issues:
            if any(eid not in allowed for eid in issue.evidence_ids):
                raise ERPError("reading_batch_source", "疑点引用了当前批次以外的原文来源。")
        return result
