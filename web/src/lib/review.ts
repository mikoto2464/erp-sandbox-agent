import type { EditableReading, Fact, Issue } from './types';
import { fieldNames } from './labels';
export function distinctIssues(issues: Issue[]): Issue[] {
  const generic = new Set(['validation.uncertain', 'validation.value_missing']);
  const specificTargets = new Set(
    issues
      .filter((issue) => issue.severity !== 'info' && !generic.has(issue.code))
      .map((issue) => issue.target),
  );
  const parseTargets = new Set(
    issues
      .filter((issue) => issue.code === 'reading.invalid_number' && issue.severity !== 'info')
      .map((issue) => issue.target),
  );
  const seen = new Set<string>();
  return issues.filter((issue) => {
    if (issue.code === 'validation.uncertain' && specificTargets.has(issue.target)) return false;
    if (issue.code === 'validation.value_missing' && parseTargets.has(issue.target)) return false;
    const key = JSON.stringify([issue.code, issue.target, issue.message]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
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
  const direct = reading.facts.find((fact) => fact.id === issue.target);
  if (direct) return direct;
  const key = issue.target.split('.').pop();
  const matches = reading.facts.filter((fact) => fact.key === key);
  return matches.length === 1 ? matches[0] : undefined;
}
const noteCode = (issue: Issue) => 'review.note.' + issue.code;
const answerPrefix = (issue: Issue) => `针对问题：${issue.message}\n待确认答复：`;
export const TERM_TOTAL_ANSWER =
  '采用用户默认口径：该利率 / 贴息覆盖对应完整期限，不是年利率或每季利率，不按期数重复相乘。';
export const UNKNOWN_RATE_ANSWER = '暂不清楚计息口径；保留为未确定条件，不据此计算确定的利息。';
export function issueAnswer(reading: EditableReading, issue: Issue): string {
  const note = reading.issues.find(
    (item) =>
      item.code === noteCode(issue) &&
      item.target === issue.target &&
      item.message.startsWith(answerPrefix(issue)),
  );
  return note?.message.slice(answerPrefix(issue).length) || '';
}
export function setIssueAnswer(reading: EditableReading, issue: Issue, answer: string) {
  reading.issues = reading.issues.filter(
    (item) =>
      !(
        item.code === noteCode(issue) &&
        item.target === issue.target &&
        item.message.startsWith(answerPrefix(issue))
      ),
  );
  if (answer.trim())
    reading.issues.push({
      code: noteCode(issue),
      severity: 'info',
      target: issue.target,
      message: answerPrefix(issue) + answer,
      evidence_ids: issue.evidence_ids,
    });
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
  if (position >= 0) {
    const answer = explanation.trim() || '默认';
    if (answer !== '默认') {
      setIssueAnswer(reading, issue, '');
      const target =
        issueFact(reading, issue) || reading.rules.find((rule) => rule.id === issue.target);
      const clarification = `用户答复（${issue.message}）：${answer}`;
      if (target && !target.explanation.includes(clarification))
        target.explanation = [target.explanation, clarification].filter(Boolean).join('\n');
      const fact = issueFact(reading, issue);
      if (fact && ratePeriodIssue(issue, fact) && ['finance', 'discount'].includes(fact.category)) {
        const key = fact.category === 'finance' ? 'interest_rate_basis' : 'discount_rate_basis';
        const basis = reading.facts.find(
          (item) =>
            item.category === fact.category &&
            item.entity_id === fact.entity_id &&
            item.key === key,
        );
        const values = {
          value: answer === TERM_TOTAL_ANSWER ? 'term_total' : null,
          value_type: 'text',
          unit: '',
          state: answer === TERM_TOTAL_ANSWER ? ('assumed' as const) : ('missing' as const),
          evidence_ids: [],
          conversion: 'identity',
          explanation:
            answer === TERM_TOTAL_ANSWER
              ? '用户确认：对应完整期限的总利率。'
              : `用户标注：计息口径${answer === UNKNOWN_RATE_ANSWER ? '不清楚' : '以用户答复为准，尚未结构化'}；${answer}`,
        };
        if (basis) Object.assign(basis, values);
        else {
          let id = fact.id + '_basis';
          while (reading.facts.some((item) => item.id === id)) id += '_';
          reading.facts.push({
            ...values,
            id,
            key,
            category: fact.category,
            entity_id: fact.entity_id,
            entity_name: fact.entity_name,
          });
        }
      }
    }
    const currentPosition = reading.issues.findIndex(
      (item) =>
        item.code === issue.code && item.target === issue.target && item.message === issue.message,
    );
    reading.issues[currentPosition] = {
      ...reading.issues[currentPosition],
      code: 'review.resolved.' + issue.code,
      severity: 'info',
      message: issue.message + '；用户答复：' + answer,
    };
  }
}
export function annotateIssue(
  reading: EditableReading,
  issue: Issue,
  choice: string,
  unitIssue: boolean,
  explanation: string,
) {
  const fact = issueFact(reading, issue),
    matches = fact ? [fact] : [];
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
  if (issue.code === 'reading.invalid_number')
    return '查看原文后修改字段的值与数据类型，再确认处理；无法确定可标记不清楚。';
  if (issue.code === 'reading.extraction_unresolved' || issue.code === 'reading.review_unresolved')
    return '查看标出的原文，在问题答复中说明参数、规则与适用对象，确认后保存。';
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
  if (ratePeriodIssue(issue, fact))
    return '默认按对应完整期限的总利率处理；题目另有规定时，在下方填写计息口径。';
  return '核对相关数据及依据，处理后保存校验。';
}
export function ratePeriodIssue(issue: Issue, fact?: Fact): boolean {
  return (
    (fact?.category === 'finance' ||
      fact?.category === 'discount' ||
      /利率|贴息|贴现/.test(issue.message)) &&
    /利率|贴息|贴现|interest|discount/i.test((fact?.key || '') + issue.message) &&
    /周期|计息|年利率|季利率|总利率|期限|按季|按年|利率口径/.test(issue.message)
  );
}
