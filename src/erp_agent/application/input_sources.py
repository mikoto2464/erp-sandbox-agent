import hashlib

from erp_agent.domain.models import (
    DocumentBlock, Evidence, ParsedDocument, ReadingInputs, SourceDocument,
)


def text_source(title: str, text: str, prefix: str) -> ParsedDocument:
    content = text.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    source = SourceDocument(prefix + digest[:24], title, digest, len(content), "text")
    blocks = []
    evidence = []
    for line in text.splitlines():
        if line.strip():
            index = len(blocks)
            eid = f"{source.id}:p{index}"
            blocks.append(DocumentBlock(index, "paragraph", line, eid))
            evidence.append(Evidence(eid, source.id, f"{title} / 第 {index + 1} 段", line))
    return ParsedDocument(source, tuple(blocks), tuple(evidence), parser_version="text-1")


def input_sources(inputs: ReadingInputs, documents: tuple[ParsedDocument, ...]) -> tuple[ParsedDocument, ...]:
    sources = []
    if inputs.problem_text:
        sources.append(text_source("手动题目", inputs.problem_text, "manual_"))
    if inputs.supplement_text:
        sources.append(text_source("题目补充与限制", inputs.supplement_text, "supplement_"))
    names = {d.source.id: d.source.filename for d in documents}
    for annotation in inputs.annotations:
        if annotation.text and annotation.document_id in names:
            text = f"关联文件：{names[annotation.document_id]}（{annotation.document_id}）\n{annotation.text}"
            sources.append(text_source("文件说明 · " + names[annotation.document_id], text, "annotation_"))
    return tuple(sources)
