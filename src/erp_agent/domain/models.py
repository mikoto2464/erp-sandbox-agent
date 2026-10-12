from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class KnowledgeState(str, Enum):
    KNOWN = "known"
    ASSUMED = "assumed"
    CONFLICTED = "conflicted"
    MISSING = "missing"


class SolverMode(str, Enum):
    LLM_ONLY = "llm_only"
    LLM_MONTE_CARLO = "llm_monte_carlo"


class PlanTone(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TableEvidenceContext:
    block_index: int
    row: int
    column: int
    heading_ids: tuple[str, ...] = ()
    column_header_ids: tuple[str, ...] = ()
    row_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    id: str
    document_id: str
    location: str
    text: str
    table_context: TableEvidenceContext | None = None


@dataclass(frozen=True)
class SourceDocument:
    id: str
    filename: str
    sha256: str
    size: int
    format: str


@dataclass(frozen=True)
class Cell:
    row: int
    column: int
    text: str
    evidence_id: str
    column_span: int = 1
    vertical_merge: str = ""
    number_format: str = ""


@dataclass(frozen=True)
class DocumentBlock:
    index: int
    kind: str
    text: str = ""
    evidence_id: str = ""
    cells: tuple[Cell, ...] = ()


@dataclass(frozen=True)
class ParsedDocument:
    source: SourceDocument
    blocks: tuple[DocumentBlock, ...]
    evidence: tuple[Evidence, ...]
    warnings: tuple[str, ...] = ()
    parser_version: str = "ooxml-1"
    converted_sha256: str | None = None


@dataclass(frozen=True)
class Fact:
    id: str
    category: str
    entity_id: str
    entity_name: str
    key: str
    value: str | None
    value_type: str
    unit: str
    state: KnowledgeState
    evidence_ids: tuple[str, ...]
    conversion: str = "identity"
    explanation: str = ""


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    expression: str
    state: KnowledgeState
    evidence_ids: tuple[str, ...]
    explanation: str = ""


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    target: str
    message: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProblemSpec:
    title: str
    facts: tuple[Fact, ...]
    years: int | None = None
    periods_per_year: int | None = None
    schema_version: str = "1"


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    version: int = 1


@dataclass(frozen=True)
class ReadingResult:
    problem: ProblemSpec
    rules: RuleSet
    issues: tuple[Issue, ...]
    model: str
    prompt_version: str = "reader-1"
    raw_response: str = ""


@dataclass(frozen=True)
class DocumentAnnotation:
    document_id: str
    text: str


@dataclass(frozen=True)
class ReadingInputs:
    revision: int = 0
    problem_text: str = ""
    supplement_text: str = ""
    annotations: tuple[DocumentAnnotation, ...] = ()


@dataclass(frozen=True)
class ProblemSnapshot:
    project_id: str
    version: int
    created_at: str
    document_ids: tuple[str, ...]
    reading: ReadingResult
    revision_note: str = ""
    inputs: ReadingInputs = field(default_factory=ReadingInputs)
    additional_sources: tuple[ParsedDocument, ...] = ()


@dataclass(frozen=True)
class Action:
    period: int
    kind: str
    entity_id: str
    amount: Decimal | None
    quantity: int | None
    description: str


@dataclass(frozen=True)
class BudgetCell:
    coordinate: str
    text: str
    number_format: str = ""
    formula: bool = False
    writable: bool = True


@dataclass(frozen=True)
class BudgetFormulaReference:
    formula_cell: str
    sheet: str
    range: str


@dataclass(frozen=True)
class BudgetValidation:
    range: str
    kind: str
    formula1: str = ""
    formula2: str = ""


@dataclass(frozen=True)
class BudgetSheet:
    name: str
    rows: int
    columns: int
    cells: tuple[BudgetCell, ...]
    merged_ranges: tuple[str, ...] = ()
    hidden: bool = False
    protected: bool = False
    formula_references: tuple[BudgetFormulaReference, ...] = ()
    validations: tuple[BudgetValidation, ...] = ()


@dataclass(frozen=True)
class BudgetTemplate:
    source: SourceDocument
    sheets: tuple[BudgetSheet, ...]
    warnings: tuple[str, ...] = ()
    parser_version: str = "budget-2"


@dataclass(frozen=True)
class BudgetItem:
    template_id: str
    sheet: str
    cell: str
    label: str
    period: int | None
    unit: str
    value: str | None
    action_indices: tuple[int, ...]
    explanation: str


@dataclass(frozen=True)
class Plan:
    title: str
    summary: str
    actions: tuple[Action, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    budget_items: tuple[BudgetItem, ...] = ()


@dataclass(frozen=True)
class GenerationConfig:
    tone: PlanTone = PlanTone.BALANCED
    custom_prompt: str = ""
    template_ids: tuple[str, ...] = ()
    previous_solution_id: str | None = None
    revision_prompt: str = ""
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningContext:
    config: GenerationConfig = field(default_factory=GenerationConfig)
    templates: tuple[BudgetTemplate, ...] = ()
    previous_plan: Plan | None = None


@dataclass(frozen=True)
class ParameterRange:
    action_index: int
    field: str
    minimum: Decimal
    maximum: Decimal
    step: Decimal


@dataclass(frozen=True)
class StrategyProposal:
    baseline: Plan
    parameters: tuple[ParameterRange, ...]
    raw_response: str
    model: str


@dataclass(frozen=True)
class Evaluation:
    feasible: bool
    verified: bool
    score: Decimal | None
    issues: tuple[Issue, ...]
    scope: str


@dataclass(frozen=True)
class SearchConfig:
    seed: int = 42
    candidates: int = 100


@dataclass(frozen=True)
class SolveRequest:
    snapshot: ProblemSnapshot
    mode: SolverMode
    search: SearchConfig = field(default_factory=SearchConfig)
    context: PlanningContext = field(default_factory=PlanningContext)


@dataclass(frozen=True)
class SolutionBundle:
    id: str
    project_id: str
    problem_version: int
    mode: SolverMode
    plan: Plan
    evaluation: Evaluation
    baseline_score: Decimal | None
    evaluated_candidates: int
    feasible_candidates: int
    seed: int | None
    model: str
    raw_response: str
    candidate_plans: tuple[Plan, ...] = ()
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    prompt_version: str = "solver-4"


@dataclass(frozen=True)
class OutputSource:
    id: str
    label: str
    value: str | None
    numeric: bool
    period: int | None
    unit: str


@dataclass(frozen=True)
class WorkbookEntry:
    sheet: str
    cell: str
    label: str
    source_ids: tuple[str, ...]
    conversion: str = "identity"
    explanation: str = ""


@dataclass(frozen=True)
class WorkbookMapping:
    id: str
    project_id: str
    solution_id: str
    problem_version: int
    template_id: str
    template_sha256: str
    entries: tuple[WorkbookEntry, ...]
    issues: tuple[Issue, ...]
    revision: int = 1
    confirmed: bool = False
    model: str = ""
    prompt_version: str = "mapper-1"


@dataclass(frozen=True)
class ArtifactManifest:
    id: str
    project_id: str
    solution_id: str
    problem_version: int
    kind: str
    filename: str
    media_type: str
    sha256: str
    size: int
    created_at: str
    draft: bool
    formula_status: str
    warnings: tuple[str, ...]
    template_id: str | None = None
    mapping_id: str | None = None
    mapping_revision: int | None = None


@dataclass(frozen=True)
class ReportData:
    snapshot: ProblemSnapshot
    solution: SolutionBundle
    sources: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class RenderedArtifact:
    content: bytes
    formula_status: str = "not_applicable"
    warnings: tuple[str, ...] = ()
