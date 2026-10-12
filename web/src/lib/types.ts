export type KnowledgeState = 'known' | 'assumed' | 'conflicted' | 'missing';
export type SolverMode = 'llm_only' | 'llm_monte_carlo';
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
export interface Job {
  id: string;
  kind: string;
  status: string;
  stage: string;
  metadata: {
    document_id?: string;
    mode?: SolverMode;
    version?: number;
    search?: Record<string, number>;
    model_preset?: { preset_name: string };
  };
  error?: { message: string };
  result?: { version: number };
}
export interface Action {
  period: number;
  description: string;
  amount: string | null;
  quantity: number | null;
}
export interface Solution {
  id: string;
  mode: SolverMode;
  problem_version: number;
  plan: {
    title: string;
    summary: string;
    actions: Action[];
    assumptions: string[];
    risks: string[];
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
