from dataclasses import dataclass, replace
from importlib.resources import files

from erp_agent.application.contracts import (
    CATEGORIES,
    FACT_SCHEMA,
    ISSUE_SCHEMA,
    NULLABLE_TEXT,
    RULE_SCHEMA,
    TEXT,
    JSONObject,
    array,
    decode_reading,
    dumps,
    loads,
    obj,
    object_schema,
    string,
    to_json,
)
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl
from erp_agent.application.reading.batches import ReadingBatch, oversized_batch
from erp_agent.application.reading.numbers import recover_numbers
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Fact, ReadingResult, RuleSet


@dataclass(frozen=True)
class ReaderLimits:
    max_request_chars: int = 100_000
    max_batch_units: int = 16
    concurrency: int = 4

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
        if name not in {"facts", "rules", "reading"}:
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
        if name == "reading":
            self.schema = dumps(
                object_schema(
                    {
                        "title": TEXT,
                        "fact_rows": {
                            "type": "array",
                            "items": {"type": "array", "items": NULLABLE_TEXT},
                        },
                        "rule_rows": {
                            "type": "array",
                            "items": {"type": "array", "items": NULLABLE_TEXT},
                        },
                        "issues": {"type": "array", "items": ISSUE_SCHEMA},
                    }
                )
            )
            self.system += (
                "\n为减少重复键，facts 使用 fact_rows，每行依次为 "
                "[id,category,entity_id,entity_name,key,value,value_type,unit,state,evidence_ids,conversion,explanation]；"
                "rules 使用 rule_rows，每行依次为 [id,name,expression,state,evidence_ids,explanation]。"
                "evidence_ids 在行内是用 | 分隔的来源 ID 字符串，无来源用空字符串；"
                "仅 value 可为 null，不省略行或列。category 只能为 "
                + "/".join(CATEGORIES)
                + "；value_type 为 number/text/integer，state 为 known/assumed/conflicted/missing；"
                "conversion 为 identity/percent_to_ratio/wan_to_yuan。unit 为规范单位，未知用空字符串。"
            )

    def request(
        self,
        batch: ReadingBatch,
        max_chars: int,
        facts: tuple[Fact, ...] = (),
        memory: RoleMemory | None = None,
    ) -> LLMRequest:
        aliases = {original: alias for alias, original in batch.source_aliases.items()}
        payload: JSONObject = {
            "role": self.name,
            "documents": [batch.wire_payload()],
            "focus_evidence_ids": [aliases.get(eid, eid) for eid in batch.focus_ids],
        }
        if memory:
            payload["shared_alignment"] = memory.alignment()
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
        return LLMRequest(
            self.system,
            dumps(payload),
            "erp_reading",
            self.schema,
            max_output_tokens=min(64000, max(8000, len(batch.focus_ids) * 90))
            if self.name != "rules"
            else 4000,
            retries=0,
        )

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
        request = self.request(batch, max_chars, facts, memory)
        payload = obj(loads(request.user))
        system_size = len(self.system) + len(self.schema) + 16
        control.checkpoint(f"reading_{self.name}", completed)
        response = None
        try:
            response = client.generate(request)
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
                    max_output_tokens=request.max_output_tokens,
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
        elif self.name == "rules":
            other_ids = {f.id for f in result.problem.facts}
            result = replace(
                result,
                problem=replace(result.problem, facts=()),
                issues=tuple(i for i in result.issues if i.target not in other_ids),
            )
        return ReadingPart(self.name, batch, result)

    def _decode(self, content: str, model: str, batch: ReadingBatch) -> ReadingResult:
        data = obj(loads(normalize_model_json(content)))
        if self.name == "reading" and set(data) == {"title", "fact_rows", "rule_rows", "issues"}:
            normalized = {"title": data["title"], "issues": data["issues"]}
            columns = {
                "facts": (
                    "id",
                    "category",
                    "entity_id",
                    "entity_name",
                    "key",
                    "value",
                    "value_type",
                    "unit",
                    "state",
                    "evidence_ids",
                    "conversion",
                    "explanation",
                ),
                "rules": ("id", "name", "expression", "state", "evidence_ids", "explanation"),
            }
            for key, names in columns.items():
                rows = []
                for row in array(data["fact_rows" if key == "facts" else "rule_rows"]):
                    cells = array(row)
                    if len(cells) != len(names):
                        raise ERPError("invalid_contract", "紧凑读题条目列数不符合契约。")
                    item = dict(zip(names, cells, strict=True))
                    refs = string(item["evidence_ids"])
                    item["evidence_ids"] = refs.split("|") if refs else []
                    rows.append(item)
                normalized[key] = rows
            data = normalized
        expected = (
            {"title", "facts", "rules", "issues"}
            if self.name == "reading"
            else {"title", self.name, "issues"}
        )
        if set(data) not in (expected, {"title", "facts", "rules", "issues"}):
            raise ERPError("invalid_contract", "读题子角色输出字段不符合契约。")
        # Accept historic full-contract fixtures; production schema contains only this role.
        data = {
            "title": data.get("title", ""),
            "facts": data.get("facts", []) if self.name != "rules" else [],
            "rules": data.get("rules", []) if self.name != "facts" else [],
            "issues": data.get("issues", []),
        }
        aliases = batch.source_aliases
        for collection in ("facts", "rules", "issues"):
            for value in array(data[collection]):
                item = obj(value)
                item["evidence_ids"] = [
                    aliases.get(string(ref), string(ref)) for ref in array(item.get("evidence_ids"))
                ]
        recover_numbers(data)
        result = decode_reading(dumps(data), model)
        document = batch.payload()
        allowed = set(batch.focus_ids)
        context = document["context_evidence"]
        assert isinstance(context, list)
        allowed.update(str(row[0]) for row in context if isinstance(row, list))
        allowed.update(e.id for note in batch.notes for e in note.evidence)
        values = result.problem.facts + result.rules.rules
        for value in values:
            if any(eid not in allowed for eid in value.evidence_ids):
                raise ERPError("reading_batch_source", "提取条目引用了当前批次以外的原文来源。")
        for issue in result.issues:
            if any(eid not in allowed for eid in issue.evidence_ids):
                raise ERPError("reading_batch_source", "疑点引用了当前批次以外的原文来源。")
        return result
