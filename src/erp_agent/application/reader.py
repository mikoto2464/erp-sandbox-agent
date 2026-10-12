from dataclasses import replace
from importlib.resources import files
from typing import Callable

from erp_agent.application.contracts import READING_SCHEMA, JSONObject, decode_reading, dumps
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl
from erp_agent.domain.models import Issue, ParsedDocument, ReadingResult
from erp_agent.domain.errors import ERPError
from erp_agent.domain.validation import validate_reading


class StructuredReader:
    def __init__(self, llm: LLMClient,
                 client_factory: Callable[[], LLMClient] | None = None) -> None:
        self.llm = llm
        self.client_factory = client_factory

    def read(self, documents: tuple[ParsedDocument, ...], control: RunControl,
             client: LLMClient | None = None) -> ReadingResult:
        client = client or (self.client_factory() if self.client_factory else self.llm)
        control.checkpoint("reading")
        prompt = files("erp_agent").joinpath("prompts/reader.txt").read_text(encoding="utf-8")
        schema = dumps(READING_SCHEMA)
        documents_json = [self._compact(doc) for doc in documents]
        response = None
        try:
            response = client.generate(LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps({"documents": documents_json}), "erp_reading", schema,
            ))
            control.checkpoint("validating")
            reading = decode_reading(normalize_model_json(response.content), response.model)
        except ERPError as error:
            if response is None and error.code != "llm_structured_response":
                raise
            control.checkpoint("repairing_reading")
            response = client.generate(LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps({"documents": documents_json,
                       "previous_output": response.content if response else None,
                       "error": error.message,
                       "instruction": "修正结构错误，只返回符合 schema 的完整读题结果。"}),
                "erp_reading", schema,
            ))
            control.checkpoint("validating")
            reading = decode_reading(normalize_model_json(response.content), response.model)
        source_issues = tuple(
            Issue("reading.source_warning", "warning", doc.source.id, warning)
            for doc in documents for warning in doc.warnings
        )
        reading = replace(reading, prompt_version="reader-7", raw_response=response.content,
                          issues=reading.issues + source_issues)
        return validate_reading(reading, tuple(e for doc in documents for e in doc.evidence))

    @staticmethod
    def _compact(document: ParsedDocument) -> JSONObject:
        blocks: list[JSONObject] = []
        for block in document.blocks:
            if block.kind == "paragraph":
                blocks.append({"paragraph": [block.evidence_id, block.text]})
            else:
                blocks.append({"table": [[c.row, c.column, c.text, c.evidence_id,
                                           c.column_span, c.vertical_merge, c.number_format] for c in block.cells],
                               "block_index": block.index})
        return {"document_id": document.source.id, "filename": document.source.filename,
                "format": document.source.format,
                "cell_layout": ["row", "column", "text", "evidence_id", "column_span",
                                "vertical_merge", "number_format"],
                "blocks": blocks, "warnings": list(document.warnings),
                "table_contexts": {
                    e.id: {"heading_ids": list(e.table_context.heading_ids),
                           "column_header_ids": list(e.table_context.column_header_ids)}
                    for e in document.evidence
                    if e.table_context and e.table_context.column_header_ids
                }}
