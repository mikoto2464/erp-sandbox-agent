import re
from collections import defaultdict

from erp_agent.domain.models import Issue, ParsedDocument, ReadingResult


def missing_table_rows(reading: ReadingResult, documents: tuple[ParsedDocument, ...]) -> tuple[Issue, ...]:
    cited = {eid for item in reading.problem.facts + reading.rules.rules
             for eid in item.evidence_ids}
    issues: list[Issue] = []
    for document in documents:
        sources = {e.id: e for e in document.evidence}
        for block in document.blocks:
            if block.kind != "table":
                continue
            rows: dict[int, list[str]] = defaultdict(list)
            for cell in block.cells:
                rows[cell.row].append(cell.evidence_id)
            missing: list[int] = []
            for row, ids in rows.items():
                if any(eid in cited for eid in ids):
                    continue
                has_data = any(re.fullmatch(r"[-+]?\d[\d,.]*\s*(?:%|％|万|元|万元|年|月|季|件|个)?",
                                           sources[eid].text.strip()) for eid in ids)
                if not has_data:
                    continue
                missing.append(row + 1)
            for start in range(0, len(missing), 20):
                selected = missing[start:start + 20]
                selected_refs = tuple(eid for row in selected for eid in rows[row - 1])
                issues.append(Issue("reading.uncovered_rows", "warning", document.source.id,
                    f"{document.source.filename} · 表格所在正文块 {block.index + 1} 的第 "
                    + "、".join(map(str, selected))
                    + " 行含数据，但提取结果没有引用这些行。请核对是否遗漏有效条目；不适用的行可说明。",
                    selected_refs))
    return tuple(issues)
