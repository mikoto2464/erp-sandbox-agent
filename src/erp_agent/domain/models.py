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


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Evidence:
    id: str
    document_id: str
    location: str
    text: str


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
class ProblemSnapshot:
    project_id: str
    version: int
    created_at: str
    document_ids: tuple[str, ...]
    reading: ReadingResult
    revision_note: str = ""


@dataclass(frozen=True)
class Action:
    period: int
    kind: str
    entity_id: str
    amount: Decimal | None
    quantity: int | None
    description: str


@dataclass(frozen=True)
class Plan:
    title: str
    summary: str
    actions: tuple[Action, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]


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
