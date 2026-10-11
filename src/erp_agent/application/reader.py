from dataclasses import replace
from importlib.resources import files
from typing import Callable

from erp_agent.application.contracts import READING_SCHEMA, JSONObject, decode_reading, dumps
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl
from erp_agent.domain.models import Issue, ParsedDocument, ReadingResult
from erp_agent.domain.errors import ERPError
from erp_agent.domain.validation import validate_reading


class StructuredReader:
    def __init__(self, llm: LLMClient,
                 client_factory: Callable[[], LLMClient] | None = None) -> None:
        self.llm = llm
        self.client_factory = client_factory

    def read(self, documents: tuple[ParsedDocument, ...], control: RunControl) -> ReadingResult:
        client = self.client_factory() if self.client_factory else self.llm
        control.checkpoint("reading")
        prompt = files("erp_agent").joinpath("prompts/reader.txt").read_text(encoding="utf-8")
        schema = dumps(READING_SCHEMA)
        response = client.generate(LLMRequest(
            prompt + "\nJSON schema:\n" + schema,
            dumps({"documents": [self._compact(doc) for doc in documents]}), "erp_reading", schema,
        ))
        control.checkpoint("validating")
        try:
            reading = decode_reading(response.content, response.model)
        except ERPError as error:
            control.checkpoint("repairing_reading")
            response = client.generate(LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps({"documents": [self._compact(doc) for doc in documents],
                       "previous_output": response.content, "error": error.message,
                       "instruction": "修正结构错误，只返回符合 schema 的完整读题结果。"}),
                "erp_reading", schema,
            ))
            control.checkpoint("validating")
            reading = decode_reading(response.content, response.model)
        source_issues = tuple(
            Issue("reading.source_warning", "warning", doc.source.id, warning)
            for doc in documents for warning in doc.warnings
        )
        reading = replace(reading, prompt_version="reader-2", issues=reading.issues + source_issues)
        return validate_reading(reading, tuple(e for doc in documents for e in doc.evidence))

    @staticmethod
    def _compact(document: ParsedDocument) -> JSONObject:
        blocks: list[JSONObject] = []
        for block in document.blocks:
            if block.kind == "paragraph":
                blocks.append({"paragraph": [block.evidence_id, block.text]})
            else:
                blocks.append({"table": [[c.row, c.column, c.text, c.evidence_id,
                                           c.column_span, c.vertical_merge] for c in block.cells]})
        return {"document_id": document.source.id, "filename": document.source.filename,
                "cell_layout": ["row", "column", "text", "evidence_id", "column_span",
                                "vertical_merge"],
                "blocks": blocks, "warnings": list(document.warnings)}
