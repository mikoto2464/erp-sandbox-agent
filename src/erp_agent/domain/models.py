from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, StrEnum
from uuid import uuid4

from erp_agent.domain.reporting import ReportOutline


class KnowledgeState(str, Enum):
    KNOWN = "known"
    ASSUMED = "assumed"
    CONFLICTED = "conflicted"
    MISSING = "missing"


class SolverMode(str, Enum):
    LLM_ONLY = "llm_only"
    # 仅解码历史方案；新求解请求拒绝该值。
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
    field_path: str = ""
    action_id: str = ""
    budget_id: str = ""
    source_ids: tuple[str, ...] = ()
    impact: tuple[str, ...] = ()


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
class TemplateInputGroup:
    label: str
    range: str
    unit: str
    description: str
    depends_on_sheets: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemplateSheetReading:
    name: str
    purpose: str
    periods: str
    input_groups: tuple[TemplateInputGroup, ...] = ()


@dataclass(frozen=True)
class TemplateIssueReview:
    issue: Issue
    answer: str


@dataclass(frozen=True)
class TemplateReading:
    template_id: str
    sha256: str
    sheets: tuple[TemplateSheetReading, ...]
    issues: tuple[Issue, ...] = ()
    reviewed: bool = False
    explanation: str = ""
    prompt_version: str = "template-reader-1"
    issue_reviews: tuple[TemplateIssueReview, ...] = ()


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
    template_readings: tuple[TemplateReading, ...] = ()


class ActionKind(StrEnum):
    PURCHASE = "purchase"
    ARRIVAL = "arrival"
    START_PRODUCTION = "start_production"
    COMPLETE_PRODUCTION = "complete_production"
    BID = "bid"
    CONTRACT = "contract"
    DELIVERY = "delivery"
    RECEIPT = "receipt"
    BORROW = "borrow"
    REPAY = "repay"
    PAYMENT = "payment"
    RESEARCH = "research"
    CERTIFY = "certify"
    MARKET_OPEN = "market_open"
    INSTALL = "install"
    RECRUIT = "recruit"
    TRAIN = "train"
    DISMISS = "dismiss"
    REVIEW = "review"
    EXTENSION = "extension"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionCondition:
    kind: str = "always"
    reference_id: str | None = None
    operator: str | None = None
    value: str | None = None
    explanation: str = ""


@dataclass(frozen=True)
class ResourceDetails:
    resource_entity_id: str
    counterparty_entity_id: str | None = None


@dataclass(frozen=True)
class ProductionDetails:
    product_entity_id: str
    line_entity_id: str | None
    worker_entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderDetails:
    order_entity_id: str
    product_entity_id: str | None = None


@dataclass(frozen=True)
class FinanceDetails:
    finance_entity_id: str
    obligation_action_id: str | None = None


@dataclass(frozen=True)
class WorkforceDetails:
    worker_entity_id: str
    program_entity_id: str | None = None


@dataclass(frozen=True)
class ProgressDetails:
    target_entity_id: str


@dataclass(frozen=True)
class ExtensionDetails:
    type_name: str
    reason: str


ActionDetails = (
    ResourceDetails
    | ProductionDetails
    | OrderDetails
    | FinanceDetails
    | WorkforceDetails
    | ProgressDetails
    | ExtensionDetails
)


@dataclass(frozen=True)
class Action:
    period: int
    kind: str
    entity_id: str
    amount: Decimal | None
    quantity: int | None
    description: str
    action_id: str = field(default_factory=lambda: uuid4().hex)
    quantity_unit: str = ""
    amount_unit: str = ""
    amount_meaning: str = "unknown"
    related_entity_ids: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()
    completion_period: int | None = None
    payment_period: int | None = None
    missing_reason: str = ""
    condition: ActionCondition = field(default_factory=ActionCondition)
    details: ActionDetails | None = None
    schema_version: str = "1"


@dataclass(frozen=True)
class BudgetCell:
    coordinate: str
    text: str
    number_format: str = ""
    formula: bool = False
    writable: bool = True
    horizontal: str = ""
    vertical: str = ""
    wrap: bool = False
    display_text: str = ""


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
    column_widths: tuple[int, ...] = ()
    row_heights: tuple[int, ...] = ()


@dataclass(frozen=True)
class BudgetTemplate:
    source: SourceDocument
    sheets: tuple[BudgetSheet, ...]
    warnings: tuple[str, ...] = ()
    parser_version: str = "budget-3"


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
    action_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    conversion: str = "identity"
    gap_id: str | None = None


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
class SolveRequest:
    snapshot: ProblemSnapshot
    mode: SolverMode
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
    prompt_version: str = "solver-14"


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
    plan_hash: str = ""
    budget_hash: str = ""


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
    plan_hash: str = ""
    budget_hash: str = ""


@dataclass(frozen=True)
class ReportData:
    snapshot: ProblemSnapshot
    solution: SolutionBundle
    sources: tuple[Evidence, ...] = ()
    outline: ReportOutline | None = None


@dataclass(frozen=True)
class RenderedArtifact:
    content: bytes
    formula_status: str = "not_applicable"
    warnings: tuple[str, ...] = ()
