import type { DocumentBlock, Evidence, ParsedDocument } from './types';
export function evidenceIndex(documents: ParsedDocument[]) {
  return new Map(
    documents.flatMap((doc) =>
      doc.evidence.map((item) => [item.id, { ...item, filename: doc.source.filename }] as const),
    ),
  );
}
export function evidenceContext(item: Evidence, index: Map<string, Evidence>) {
  const ctx = item.table_context;
  if (!ctx) return '';
  return [ctx.heading_ids, ctx.column_header_ids, ctx.row_ids]
    .map((ids) =>
      ids
        .map((id) => index.get(id)?.text)
        .filter(Boolean)
        .join(' / '),
    )
    .filter(Boolean)
    .join(' · ');
}
export interface EvidenceTable {
  key: string;
  filename: string;
  heading: string;
  block: DocumentBlock;
  primary: string[];
  related: string[];
}
export function evidenceTables(documents: ParsedDocument[], ids: string[]): EvidenceTable[] {
  const index = evidenceIndex(documents),
    groups = new Map<string, EvidenceTable>(),
    primary = new Set<string>();
  const references = ids
    .map((id) => index.get(id))
    .filter((item): item is Evidence & { filename: string } => !!item);
  for (const item of references) {
    const ctx = item.table_context;
    if (!ctx) continue;
    const isHeader = references.some((other) =>
      other.table_context?.column_header_ids.includes(item.id),
    );
    const isObject = references.some(
      (other) =>
        other.document_id === item.document_id &&
        other.table_context?.block_index === ctx.block_index &&
        other.table_context.row_ids.includes(item.id) &&
        other.table_context.column > ctx.column,
    );
    if (!isHeader && !isObject) primary.add(item.id);
    const doc = [...documents].reverse().find((doc) => doc.source.id === item.document_id);
    if (!doc) continue;
    const blocks = new Set([
      ctx.block_index,
      ...ctx.column_header_ids
        .map((id) => index.get(id)?.table_context?.block_index)
        .filter((value): value is number => value !== undefined),
    ]);
    for (const number of blocks) {
      const block = doc.blocks.find((block) => block.index === number && block.kind === 'table');
      if (!block) continue;
      const key = `${item.document_id}:${number}`;
      groups.set(key, {
        key,
        filename: doc.source.filename,
        heading:
          ctx.heading_ids
            .map((id) => index.get(id)?.text)
            .filter(Boolean)
            .join(' / ') || '原文表格',
        block,
        primary: [],
        related: ids,
      });
    }
  }
  return [...groups.values()]
    .sort((a, b) => a.filename.localeCompare(b.filename) || a.block.index - b.block.index)
    .map((group) => ({ ...group, primary: [...primary] }));
}
