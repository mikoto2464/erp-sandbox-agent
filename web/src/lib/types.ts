export type KnowledgeState = 'known' | 'assumed' | 'conflicted' | 'missing';
export type SolverMode = 'llm_only' | 'llm_monte_carlo';
export type PlanTone = 'conservative' | 'balanced' | 'aggressive' | 'custom';
export interface GenerationConfig {
  tone: PlanTone;
  custom_prompt: string;
  template_ids: string[];
  previous_solution_id: string | null;
  revision_prompt: string;
  requirements?: string[];
}
export interface BudgetTemplate {
  source: ParsedDocument['source'];
  sheets: {
    name: string;
    rows: number;
    columns: number;
    cells: {
      coordinate: string;
      text: string;
      number_format: string;
      formula: boolean;
      writable: boolean;
      horizontal?: string;
      vertical?: string;
      wrap?: boolean;
      display_text?: string;
    }[];
    merged_ranges: string[];
    hidden: boolean;
    protected: boolean;
    column_widths?: number[];
    row_heights?: number[];
  }[];
  warnings: string[];
}
export interface BudgetItem {
  template_id: string;
  sheet: string;
  cell: string;
  label: string;
  period: number | null;
  unit: string;
  value: string | null;
  action_indices: number[];
  action_ids?: string[];
  source_ids?: string[];
  conversion?: string;
  gap_id?: string;
  explanation: string;
}
export interface WorkbookEntry {
  sheet: string;
  cell: string;
  label: string;
  source_ids: string[];
  conversion: string;
  explanation: string;
}
export interface WorkbookMapping {
  id: string;
  solution_id: string;
  template_id: string;
  revision: number;
  confirmed: boolean;
  entries: WorkbookEntry[];
  issues: Issue[];
  values: (string | null)[];
  sources: {
    id: string;
    label: string;
    value: string | null;
    numeric: boolean;
    period: number | null;
    unit: string;
  }[];
}
export interface Artifact {
  id: string;
  solution_id: string;
  problem_version: number;
  kind: 'docx' | 'xlsx';
  filename: string;
  created_at: string;
  draft: boolean;
  formula_status: string;
  warnings: string[];
  template_id: string | null;
  mapping_id: string | null;
  mapping_revision: number | null;
}
export interface Project {
  id: string;
  name: string;
  created_at: string;
}
export interface TableContext {
  block_index: number;
  row: number;
  column: number;
  heading_ids: string[];
  column_header_ids: string[];
  row_ids: string[];
}
export interface Evidence {
  id: string;
  document_id: string;
  location: string;
  text: string;
  table_context?: TableContext | null;
  filename?: string;
}
export interface Cell {
  row: number;
  column: number;
  text: string;
  evidence_id: string;
  column_span: number;
  vertical_merge: string;
}
export interface DocumentBlock {
  index: number;
  kind: string;
  text: string;
  evidence_id: string;
  cells: Cell[];
}
export interface ParsedDocument {
  source: { id: string; filename: string; size: number; sha256: string; format: string };
  blocks: DocumentBlock[];
  evidence: Evidence[];
  warnings: string[];
}
export interface Fact {
  id: string;
  category: string;
  entity_id: string;
  entity_name: string;
  key: string;
  value: string | null;
  value_type: string;
  unit: string;
  state: KnowledgeState;
  evidence_ids: string[];
  conversion: string;
  explanation: string;
}
export interface Rule {
  id: string;
  name: string;
  expression: string;
  state: KnowledgeState;
  evidence_ids: string[];
  explanation: string;
}
export interface Issue {
  field_path?: string;
  action_id?: string;
  budget_id?: string;
  source_ids?: string[];
  impact?: string[];
  code: string;
  severity: string;
  target: string;
  message: string;
  evidence_ids: string[];
}
export interface EditableReading {
  title: string;
  facts: Fact[];
  rules: Rule[];
  issues: Issue[];
}
export interface ReadingInputs {
  revision: number;
  problem_text: string;
  supplement_text: string;
  annotations: { document_id: string; text: string }[];
}
export interface Snapshot {
  document_ids?: string[];
  template_readings?: TemplateReading[];
  version: number;
  inputs: ReadingInputs;
  additional_sources: ParsedDocument[];
  reading: {
    problem: {
      title: string;
      facts: Fact[];
      years: number | null;
      periods_per_year: number | null;
    };
    rules: { rules: Rule[] };
    issues: Issue[];
  };
}
export interface TemplateReading {
  template_id: string;
  sha256: string;
  reviewed: boolean;
  explanation: string;
  sheets: {
    name: string;
    purpose: string;
    periods: string;
    input_groups: {
      label: string;
      range: string;
      unit: string;
      description: string;
      depends_on_sheets?: string[];
    }[];
  }[];
  issues: Issue[];
  issue_reviews?: TemplateIssueReview[];
}
export interface TemplateIssueReview {
  issue: Issue;
  answer: string;
}
export interface TemplateLibraryItem {
  id: string;
  source: BudgetTemplate['source'];
  parsed: boolean;
  reading: TemplateReading | null;
  sheet_names?: string[];
}
export interface ModelLoad {
  id: string;
  name: string;
  priority: number;
  max_parallel: number;
  in_use: number;
  available: number;
}
export interface AgentTask {
  input_chars?: number;
  output_limit?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  elapsed_seconds?: number;
  recovery?: string;
  child_count?: number;
  input_group_count?: number;
  recommendation_count?: number;
  result_status?: string;
  unresolved_reason?: string;
  planning_limit?: string;
  task_kind?: string;
  planned_count?: number;
  batch_count?: number;
  difficulty?: string;
  difficulty_source?: string;
  candidate_checks?: {
    id: string;
    name: string;
    task_tier: string;
    eligible: boolean;
    reason: string;
  }[];
  dispatch_reason?: string;
  assignment_reason?: string;
  dispatch_fallback?: boolean;
  dispatch_reused?: boolean;
  dispatch_planned?: boolean;
  model_loads?: ModelLoad[];
  id: string;
  parent_id?: string;
  agent: string;
  role: string;
  object: string;
  status: string;
  stage?: string;
  model?: string;
  preset_name?: string;
  candidates?: string[];
  attempt?: number;
  evidence_count?: number;
  fact_count?: number;
  rule_count?: number;
  issue_count?: number;
  error?: string;
  started_at?: string;
  finished_at?: string;
}
export interface Job {
  intervention?: {
    id: string;
    task_id: string;
    title: string;
    message: string;
    help_text?: string;
    input_label?: string;
    input_placeholder?: string;
    technical_details?: string;
    code: string;
    details: {
      cash_unit?: string;
      cash_limit?: string;
      affected_actions?: { period_label: string; description: string; entity_name?: string }[];
      actions?: {
        action_id: string;
        period: number;
        description: string;
        amount: string;
        unit: string;
      }[];
    };
    options: { id: string; label: string; description: string }[];
  };
  project_id?: string;
  scope_name?: string;
  created_at?: string;
  tasks?: AgentTask[];
  archived_tasks?: number;
  completed?: number;
  id: string;
  kind: string;
  status: string;
  stage: string;
  metadata: {
    document_ids?: string[];
    template_ids?: string[];
    append?: boolean;
    document_id?: string;
    template_id?: string;
    filename?: string;
    mode?: SolverMode;
    version?: number;
    search?: Record<string, number>;
    generation?: GenerationConfig;
    solution_id?: string;
    kind?: 'docx' | 'xlsx';
    mapping_id?: string | null;
    upload_deleted?: boolean;
    model_preset?: { preset_name: string };
  };
  error?: { message: string };
  result?: { version: number };
}
export interface Action {
  action_id?: string;
  kind?: string;
  entity_id?: string;
  quantity_unit?: string;
  amount_unit?: string;
  amount_meaning?: string;
  related_entity_ids?: string[];
  depends_on?: string[];
  fact_ids?: string[];
  rule_ids?: string[];
  gap_ids?: string[];
  completion_period?: number | null;
  payment_period?: number | null;
  missing_reason?: string;
  condition?: {
    kind: string;
    reference_id: string | null;
    operator: string | null;
    value: string | null;
    explanation: string;
  };
  details?: Record<string, unknown>;
  period: number;
  description: string;
  amount: string | null;
  quantity: number | null;
}
export interface BudgetGap {
  id: string;
  code: string;
  message: string;
  targets: { template_id: string; sheet: string; cell: string }[];
  source_ids: string[];
}
export interface SolutionStage {
  status: string;
  error?: { code: string; message: string } | null;
  result?: {
    gaps?: BudgetGap[];
    completed_sheets?: [string, string][];
    failed_sheets?: [string, string, string][];
  } | null;
}
export interface Solution {
  plan_hash?: string;
  stages?: { actions: SolutionStage; budget: SolutionStage; report: SolutionStage };
  id: string;
  mode: SolverMode;
  problem_version: number;
  generation?: GenerationConfig;
  plan: {
    title: string;
    summary: string;
    actions: Action[];
    assumptions: string[];
    risks: string[];
    budget_items?: BudgetItem[];
  };
  evaluation: { verified: boolean; score: string | null; issues: Issue[] };
}
export interface Status {
  llm_configured: boolean;
  simulation_ready: boolean;
  default_solver: SolverMode;
  max_upload_mb: number;
}
export interface Provider {
  id: string;
  name: string;
  base_url: string;
  api_key_required: boolean;
}
export interface ModelPreset {
  id: string;
  revision: number;
  name: string;
  configured: boolean;
  priority: number;
  max_parallel: number;
  scheduling_enabled: boolean;
  capabilities: string;
  task_tier?: string;
  task_roles?: string[];
  provider: string;
  base_url: string;
  model: string;
  api_key_set: boolean;
  format: string;
  timeout: number;
  max_input_chars: number;
  max_output_tokens: number;
  token_parameter: string;
  thinking_mode: string;
  thinking_budget: number;
}
export interface ModelCatalog {
  active_id: string;
  presets: ModelPreset[];
  providers: Provider[];
}
export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}
