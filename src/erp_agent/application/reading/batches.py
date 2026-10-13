import re
from dataclasses import dataclass, replace
from itertools import groupby

from langchain_text_splitters import RecursiveCharacterTextSplitter

from erp_agent.application.contracts import JSONObject, JSONValue, dumps, obj
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import DocumentBlock, ParsedDocument


@dataclass(frozen=True)
class ReadingBatch:
    document: ParsedDocument
    units: tuple[DocumentBlock, ...]
    notes: tuple[ParsedDocument, ...] = ()
    label: str = ""

    @property
    def focus_ids(self) -> tuple[str, ...]:
        return tuple(
            eid
            for block in self.units
            for eid in (
                (block.evidence_id,)
                if block.kind == "paragraph"
                else tuple(cell.evidence_id for cell in block.cells)
            )
        )

    def split(self) -> tuple["ReadingBatch", "ReadingBatch"]:
        middle = len(self.units) // 2
        return replace(self, units=self.units[:middle]), replace(self, units=self.units[middle:])

    @property
    def source_aliases(self) -> dict[str, str]:
        return {
            f"e{index}": evidence.id
            for index, evidence in enumerate(
                self.document.evidence + tuple(e for note in self.notes for e in note.evidence)
            )
            if len(evidence.id) > 32
        }

    def wire_payload(self) -> JSONObject:
        aliases = {original: alias for alias, original in self.source_aliases.items()}

        def compact(value: JSONValue) -> JSONValue:
            if isinstance(value, str):
                return aliases.get(value, value)
            if isinstance(value, list):
                return [compact(item) for item in value]
            if isinstance(value, dict):
                return {key: compact(item) for key, item in value.items()}
            return value

        return obj(compact(self.payload()))

    def payload(self) -> JSONObject:
        sources = {e.id: e for e in self.document.evidence}
        focused = set(self.focus_ids)
        context_ids: set[str] = set()
        contexts: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
        for eid in self.focus_ids:
            evidence = sources[eid]
            context = evidence.table_context
            if context:
                refs = context.heading_ids + context.column_header_ids
                context_ids.update(refs)
                contexts.setdefault((context.heading_ids, context.column_header_ids), []).append(
                    eid
                )
        # Keep the previous heading and vertically merged labels when a table is split.
        for unit in self.units:
            previous = [
                b
                for b in self.document.blocks
                if b.index < unit.index and b.kind == "paragraph" and b.text.strip()
            ]
            if previous:
                context_ids.add(previous[-1].evidence_id)
            if unit.kind != "table":
                continue
            original = next(b for b in self.document.blocks if b.index == unit.index)
            for cell in unit.cells:
                if cell.vertical_merge != "continue":
                    continue
                parents = [
                    c
                    for c in original.cells
                    if c.column == cell.column
                    and c.row < cell.row
                    and c.vertical_merge == "restart"
                ]
                if parents:
                    context_ids.add(parents[-1].evidence_id)
        blocks: list[JSONObject] = []
        for unit in self.units:
            if unit.kind == "paragraph":
                blocks.append(
                    {"paragraph": [unit.evidence_id, unit.text], "block_index": unit.index}
                )
            else:
                cells = [
                    [
                        c.row,
                        c.column,
                        c.text,
                        c.evidence_id,
                        c.column_span,
                        c.vertical_merge,
                        c.number_format,
                    ]
                    for c in unit.cells
                ]
                if blocks and "table" in blocks[-1] and blocks[-1]["block_index"] == unit.index:
                    table = blocks[-1]["table"]
                    assert isinstance(table, list)
                    table.extend(cells)
                else:
                    blocks.append({"table": cells, "block_index": unit.index})
        return {
            "document_id": self.document.source.id,
            "filename": self.document.source.filename,
            "format": self.document.source.format,
            "warnings": list(self.document.warnings),
            "cell_layout": [
                "row",
                "column",
                "text",
                "evidence_id",
                "column_span",
                "vertical_merge",
                "number_format",
            ],
            "blocks": blocks,
            "table_context_groups": [
                {
                    "evidence_ids": ids,
                    "heading_ids": list(headings),
                    "column_header_ids": list(headers),
                }
                for (headings, headers), ids in contexts.items()
            ],
            "context_evidence": [
                [e.id, e.text[:1500], e.location]
                for e in self.document.evidence
                if e.id in context_ids - focused
            ],
            "file_notes": [
                {"document_id": note.source.id, "evidence": [[e.id, e.text] for e in note.evidence]}
                for note in self.notes
            ],
        }


class DocumentBatcher:
    def tasks(self, documents: tuple[ParsedDocument, ...]) -> tuple[ReadingBatch, ...]:
        tasks: list[ReadingBatch] = []
        for document in documents:
            notes = self._notes(document, documents)
            numbered_headings = [
                b.text.strip()
                for b in document.blocks
                if self._heading(b) and not b.text.strip().startswith("工作表：")
            ]
            major_style = (
                "chapter"
                if any(re.match(r"^第.+[章节]", text) for text in numbered_headings)
                else (
                    "chinese"
                    if any(
                        re.match(r"^[一二三四五六七八九十百]+[、．.]", text)
                        for text in numbered_headings
                    )
                    else "arabic"
                )
            )
            current: tuple[DocumentBlock, ...] = ()
            label = "题目正文"
            chapter = False
            for block in document.blocks:
                if block.kind == "paragraph" and not block.text.strip():
                    continue
                heading = self._heading(block) and (
                    block.text.strip().startswith("工作表：")
                    or major_style == "arabic"
                    or bool(re.match(r"^第.+[章节]", block.text.strip()))
                    or (
                        major_style == "chinese"
                        and bool(
                            re.match(
                                r"^[一二三四五六七八九十百]+[、．.]",
                                block.text.strip(),
                            )
                        )
                    )
                )
                standalone_table = (
                    not chapter
                    and block.kind == "table"
                    and any(unit.kind == "table" for unit in current)
                )
                if current and (heading or standalone_table):
                    tasks.append(ReadingBatch(document, current, notes, label))
                    current = ()
                if heading:
                    label = block.text.strip()
                    chapter = True
                elif standalone_table:
                    label = f"表格 {block.index + 1}"
                if block.kind == "table":
                    current += tuple(
                        replace(block, cells=tuple(row))
                        for _, row in groupby(
                            sorted(block.cells, key=lambda c: (c.row, c.column)),
                            key=lambda c: c.row,
                        )
                    )
                else:
                    current += (block,)
            if current:
                tasks.append(ReadingBatch(document, current, notes, label))
        return tuple(tasks)

    @staticmethod
    def _heading(block: DocumentBlock) -> bool:
        text = block.text.strip()
        return block.kind == "paragraph" and (
            text.startswith("工作表：")
            or (
                len(text) <= 80
                and not re.search(r"[。；！？]", text)
                and bool(
                    re.match(
                        r"^(?:[一二三四五六七八九十百]+[、．.]|\d+[、．.](?!\d)|第[一二三四五六七八九十百\d]+[章节])",
                        text,
                    )
                )
            )
        )

    @staticmethod
    def _notes(
        document: ParsedDocument, documents: tuple[ParsedDocument, ...]
    ) -> tuple[ParsedDocument, ...]:
        notes = tuple(
            d
            for d in documents
            if d.source.id.startswith(("annotation_", "supplement_"))
            and d.source.id != document.source.id
            and (
                d.source.id.startswith("supplement_")
                or any(document.source.id in e.text for e in d.evidence)
            )
            and len(dumps([[e.id, e.text] for e in d.evidence])) <= 2000
        )
        while notes and len(dumps([[e.id, e.text] for n in notes for e in n.evidence])) > 3000:
            notes = notes[:-1]
        return notes

    def batches(
        self, documents: tuple[ParsedDocument, ...], max_chars: int, max_units: int
    ) -> tuple[ReadingBatch, ...]:
        result: list[ReadingBatch] = []
        for document in documents:
            related = self._notes(document, documents)
            units: list[DocumentBlock] = []
            for block in document.blocks:
                if block.kind == "paragraph":
                    if block.text.strip():
                        limit = max(256, min(3000, max_chars // 2))
                        splitter = RecursiveCharacterTextSplitter(
                            chunk_size=limit,
                            chunk_overlap=0,
                            keep_separator=True,
                            strip_whitespace=False,
                            separators=["\n", "。", "；", "！", "？", " ", ""],
                        )
                        units.extend(
                            replace(block, text=part) for part in splitter.split_text(block.text)
                        )
                else:
                    for _, row in groupby(
                        sorted(block.cells, key=lambda c: (c.row, c.column)), key=lambda c: c.row
                    ):
                        units.append(replace(block, cells=tuple(row)))
            current: tuple[DocumentBlock, ...] = ()
            for unit in units:
                candidate = ReadingBatch(document, current + (unit,), related)
                new_table = (
                    current
                    and unit.kind == "table"
                    and any(b.kind == "table" and b.index != unit.index for b in current)
                )
                new_section = (
                    unit.kind == "paragraph"
                    and len(unit.text.strip()) <= 80
                    and not re.search(r"[。；！？]", unit.text)
                    and re.match(
                        r"^\s*(?:[一二三四五六七八九十百]+[、．.]|\d+[、．.](?!\d)|第[一二三四五六七八九十百\d]+[章节])",
                        unit.text,
                    )
                )
                if current and (
                    new_table
                    or new_section
                    or len(current) >= max_units
                    or len(dumps(candidate.payload())) > max_chars
                ):
                    result.append(ReadingBatch(document, current, related))
                    current = ()
                current += (unit,)
            if current:
                result.append(ReadingBatch(document, current, related))
        return tuple(result)


def oversized_batch(batch: ReadingBatch) -> ERPError:
    return ERPError(
        "reading_batch_limit",
        f"{batch.document.source.filename} 的正文块 {batch.units[0].index + 1} "
        "单段或单行及其上下文超过读题请求上限，请拆分该段内容或提高输入上限。",
    )
