import hashlib
from dataclasses import dataclass
from importlib.resources import files

from erp_agent.application.contracts import (
    TEXT,
    JSONObject,
    array,
    dumps,
    integer,
    loads,
    obj,
    object_schema,
    string,
)
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest
from erp_agent.domain.errors import ERPError

DISPATCH_VERSION = "dispatcher-2"
DISPATCH_SCHEMA = object_schema(
    {
        "difficulty": {"type": "string", "enum": ["simple", "moderate", "complex"]},
        "assignments": {
            "type": "array",
            "items": object_schema(
                {
                    "preset_id": TEXT,
                    "reason": TEXT,
                }
            ),
        },
        "summary": TEXT,
        "reuse_scope": {"type": "string", "enum": ["none", "same_table"]},
    }
)


@dataclass(frozen=True)
class ModelAssignment:
    preset_id: str
    reason: str


@dataclass(frozen=True)
class DispatchDecision:
    difficulty: str
    assignments: tuple[ModelAssignment, ...]
    summary: str
    fallback: bool = False
    reuse_scope: str = "none"


def table_dispatch_key(request: LLMRequest) -> str | None:
    if not request.task or request.task.agent != "Reader" or request.schema_name != "erp_reading":
        return None
    try:
        data = obj(loads(request.user))
        if any(k in data for k in ("error", "errors", "previous_output")):
            return None
        documents = array(data.get("documents"))
        if len(documents) != 1:
            return None
        document = obj(documents[0])
        blocks = [obj(value) for value in array(document.get("blocks"))]
        if not blocks or any("table" not in block for block in blocks):
            return None
        cells = [array(cell) for block in blocks for cell in array(block["table"])]
        if any(cell[5] or cell[4] != 1 for cell in cells):
            return None
        # 表头、结构与输入量一致才复用；规则、合并格或修复重新评估。
        if any(
            any(
                marker in str(cell[2])
                for marker in ("公式", "必须", "不得", "扣分", "限制", "违约")
            )
            for cell in cells
        ):
            return None
        shape = sorted({(str(cell[1]), str(cell[4]), str(cell[6])) for cell in cells})
        headers = sorted(
            {
                str(ref)
                for group in array(document.get("table_context_groups", []))
                for ref in array(obj(group).get("column_header_ids", []))
            }
        )
        if not headers:
            return None
        descriptor = {
            "source": document["document_id"],
            "blocks": [b["block_index"] for b in blocks],
            "shape": shape,
            "headers": headers,
            "notes": document.get("file_notes"),
            "size_group": len(request.user) // 3000,
            "system": request.system,
            "schema": request.schema_json,
        }
        return hashlib.sha256(dumps(descriptor).encode()).hexdigest()
    except (ERPError, IndexError, KeyError):
        return None


def _samples(value: object, limit: int) -> list[str]:
    if isinstance(value, dict):
        parts = list(value.items())
        if len(parts) > 12:
            parts = parts[:8] + parts[-4:]
        return [f"{key}: {part}" for key, item in parts for part in _samples(item, limit)]
    if isinstance(value, list):
        parts = value if len(value) <= 8 else value[:4] + value[-4:]
        return [part for item in parts for part in _samples(item, limit)]
    return [str(value)[:limit]] if value is not None else []


def task_material(request: LLMRequest) -> JSONObject:
    try:
        data = obj(loads(request.user))
    except ERPError:
        data = {"text": request.user}
    task = request.task
    material = {
        key: value
        for key, value in data.items()
        if key not in {"previous_output", "shared_memory", "task_memory", "focus_evidence_ids"}
    }
    documents = material.get("documents")
    if isinstance(documents, list):
        material["documents"] = [
            {
                key: value
                for key, value in document.items()
                if key in {"filename", "blocks", "context_evidence", "file_notes"}
            }
            for document in documents
            if isinstance(document, dict)
        ]
    sample = "\n".join(_samples(material, 300))
    if len(sample) > 2400:
        middle = len(sample) // 2
        sample = (
            sample[:1000] + "\n…\n" + sample[middle - 300 : middle + 300] + "\n…\n" + sample[-700:]
        )
    return {
        "agent": task.agent if task else "Agent",
        "role": task.role if task else request.schema_name,
        "object": task.object[:300] if task else request.schema_name,
        "input_chars": len(request.system) + len(request.user) + len(request.schema_json),
        "requested_output_tokens": request.max_output_tokens,
        "has_repair": any(key in data for key in ("error", "errors", "previous_output")),
        "can_reuse_same_table": table_dispatch_key(request) is not None,
        "keys": list(data),
        "instructions": request.system[:1000],
        "content_sample": sample,
        "sampling_note": "仅为有界任务样本，完整输入由执行角色读取；样本不能证明无跨段关联。",
    }


class ModelDispatcher:
    """主模型评估具体任务，只返回经过验证的执行模型候选。"""

    version = DISPATCH_VERSION

    def __init__(self) -> None:
        self.prompt = (
            files("erp_agent").joinpath("prompts/dispatcher.txt").read_text(encoding="utf-8")
        )
        self.schema = dumps(DISPATCH_SCHEMA)

    def decide(
        self, request: LLMRequest, models: list[JSONObject], client: LLMClient, max_chars: int
    ) -> DispatchDecision:
        payload: JSONObject = {
            "task": task_material(request),
            "models": models,
            "model_columns": [
                "id",
                "name",
                "model",
                "priority",
                "max_parallel",
                "in_use",
                "available",
                "available_after_dispatch",
                "capabilities",
                "eligible",
            ],
            "model_rows": [],
        }
        # 小上下文主模型也获得完整目录，先缩短描述，再缩短任务样本。
        if len(self.prompt) + len(self.schema) + len(dumps(payload)) > max_chars:
            payload["models"] = []
            payload["model_rows"] = [
                [
                    m["id"],
                    str(m["name"])[:20],
                    str(m["model"])[:35],
                    m["priority"],
                    m["max_parallel"],
                    m["in_use"],
                    m["available"],
                    m.get("available_after_dispatch", m["available"]),
                    str(m["capabilities"])[:80],
                    m.get("eligible", True),
                ]
                for m in models
            ]
        task = obj(payload["task"])
        while len(self.prompt) + len(self.schema) + len(dumps(payload)) > max_chars:
            sample = string(task["content_sample"])
            if len(sample) <= 200:
                raise ERPError("dispatch_input_limit", "主 Agent 调度目录超出模型输入上限。")
            task["content_sample"] = sample[: len(sample) // 2]
        response = client.generate(
            LLMRequest(
                self.prompt,
                dumps(payload),
                "erp_model_dispatch",
                self.schema,
                max_output_tokens=1600,
                retries=0,
            )
        )
        root = obj(
            loads(normalize_model_json(response.content)),
            {"difficulty", "assignments", "summary", "reuse_scope"},
        )
        difficulty = string(root["difficulty"])
        if difficulty not in {"simple", "moderate", "complex"}:
            raise ERPError("dispatch_contract", "主 Agent 返回了无效任务难度。")
        allowed = {string(m["id"]) for m in models if m.get("eligible", True)}
        choices = array(root["assignments"])
        integer(len(choices), 1, min(5, len(allowed)))
        assignments = []
        for value in choices:
            item = obj(value, {"preset_id", "reason"})
            preset_id, reason = string(item["preset_id"], 100), string(item["reason"], 500).strip()
            if preset_id not in allowed or not reason:
                raise ERPError("dispatch_contract", "主 Agent 分配了目录外模型或未说明依据。")
            assignments.append(ModelAssignment(preset_id, reason))
        if len({a.preset_id for a in assignments}) != len(assignments):
            raise ERPError("dispatch_contract", "主 Agent 返回了重复执行模型。")
        summary = string(root["summary"], 800).strip()
        if not summary:
            raise ERPError("dispatch_contract", "主 Agent 未说明任务分配依据。")
        reuse_scope = string(root["reuse_scope"])
        if reuse_scope not in {"none", "same_table"}:
            raise ERPError("dispatch_contract", "调度复用范围无效。")
        if difficulty != "simple":
            reuse_scope = "none"
        return DispatchDecision(difficulty, tuple(assignments), summary, reuse_scope=reuse_scope)
