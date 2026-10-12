import type { EditableReading, Fact, Issue } from './types';
import { fieldNames } from './labels';
export function factInputError(fact: Fact): string {
  if (
    ['years', 'periods_per_year'].includes(fact.key) &&
    fact.value !== null &&
    (!Number.isSafeInteger(Number(fact.value)) || Number(fact.value) <= 0)
  )
    return '经营年数和每年期数须填写正整数。';
  return '';
}
export function issueFact(reading: EditableReading, issue: Issue) {
  return (
    reading.facts.find((fact) => fact.id === issue.target) ||
    reading.facts.find((fact) => fact.key === issue.target.split('.').pop())
  );
}
export function fieldConfirmation(fact: Fact) {
  return `用户已确认：${fieldNames[fact.key] || fact.key}=${fact.value ?? '未提供'} ${fact.unit}；说明：${fact.explanation || '默认'}`;
}
export function confirmFact(reading: EditableReading, fact: Fact) {
  reading.issues = reading.issues.filter(
    (issue) => !(issue.code === 'review.confirmed' && issue.target === fact.id),
  );
  reading.issues.push({
    code: 'review.confirmed',
    severity: 'info',
    target: fact.id,
    message: fieldConfirmation(fact),
    evidence_ids: fact.evidence_ids,
  });
}
export function resolveIssue(reading: EditableReading, issue: Issue, explanation: string) {
  const position = reading.issues.findIndex(
    (item) =>
      item.code === issue.code && item.target === issue.target && item.message === issue.message,
  );
  if (position >= 0)
    reading.issues[position] = {
      ...reading.issues[position],
      code: 'review.resolved.' + issue.code,
      severity: 'info',
      message: issue.message + '；用户标注：' + explanation,
    };
}
export function annotateIssue(
  reading: EditableReading,
  issue: Issue,
  choice: string,
  unitIssue: boolean,
  explanation: string,
) {
  const fact = issueFact(reading, issue),
    matches = fact
      ? [fact]
      : unitIssue
        ? reading.facts.filter((fact) => fact.key === issue.target.split('.').pop())
        : [];
  const note = `用户标注：${unitIssue ? '单位' + choice + '，按无单位保存' : choice}${explanation ? '；' + explanation : ''}`;
  if (unitIssue)
    for (const field of matches) {
      field.unit = '';
      field.explanation = note;
    }
  else if (fact)
    Object.assign(fact, { value: null, state: 'missing', evidence_ids: [], explanation: note });
  if (!issue.code.startsWith('validation.')) resolveIssue(reading, issue, note);
  else if (fact && !unitIssue)
    reading.issues.push({
      code: 'review.annotation',
      severity: 'info',
      target: fact.id,
      message: '用户标注：' + choice,
      evidence_ids: [],
    });
}
export function issueAdvice(issue: Issue, fact?: Fact) {
  if (issue.code === 'validation.unsupported_value')
    return fact?.value_type === 'text'
      ? '核对文字与对应原文。暂定解释可改为假设并说明。'
      : '核对数据格及表头。单位不明可留空，转换须有依据。';
  if (issue.code === 'validation.source_missing') return '原文依据无效或缺失，请重新选择来源。';
  if (issue.code === 'validation.uncertain')
    return '核对具体含义与来源；无法确定时保留假设或冲突并说明。';
  if (issue.code === 'validation.value_missing')
    return '可补充已查到的值，或标注无、不清楚；不要用零替代未知值。';
  if (/horizon/.test(issue.code)) return '默认 4 年、每年 4 季；周期异常时请修正并保存。';
  return '核对相关数据及依据，处理后保存校验。';
}
