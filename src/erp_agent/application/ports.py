from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from erp_agent.application.contracts import JSONObject, JSONValue
from erp_agent.domain.models import (
    BudgetTemplate,
    Evaluation,
    ParsedDocument,
    Plan,
    PlanningContext,
    ProblemSnapshot,
    RenderedArtifact,
    ReportData,
    SolutionBundle,
    SourceDocument,
    StrategyProposal,
    WorkbookMapping,
)
from erp_agent.domain.reporting import ReportOutline

DEFAULT_PLANNING_CONTEXT = PlanningContext()


@dataclass(frozen=True)
class TaskContext:
    id: str
    agent: str
    role: str
    object: str
    parent_id: str = ""


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    schema_name: str
    schema_json: str
    max_output_tokens: int | None = None
    retries: int | None = None
    task: TaskContext | None = None
    reasoning_reserve_tokens: int = 0
    stream: bool = False


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    output_limit: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    elapsed_seconds: float | None = None


class LLMClient(Protocol):
    @property
    def configured(self) -> bool: ...

    def generate(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class ModelBinding:
    preset_id: str
    preset_name: str
    preset_revision: int
    configuration: JSONObject
    client: LLMClient = field(repr=False)

    def metadata(self) -> JSONObject:
        return {
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "preset_revision": self.preset_revision,
            **self.configuration,
        }


class ModelSettings(Protocol):
    def public(self) -> JSONObject: ...
    def update(self, data: JSONObject) -> JSONObject: ...
    def catalog(self) -> JSONObject: ...
    def create_preset(self, data: JSONObject) -> JSONObject: ...
    def update_preset(self, preset_id: str, data: JSONObject) -> JSONObject: ...
    def activate(self, preset_id: str, data: JSONObject) -> JSONObject: ...
    def delete_preset(self, preset_id: str, revision: int) -> None: ...
    def capture(self) -> ModelBinding: ...
    def capture_pool(self) -> ModelBinding: ...
    def test_preset(self, preset_id: str, data: JSONObject) -> JSONObject: ...


class DocumentConverter(Protocol):
    def convert(self, content: bytes) -> bytes: ...


class DocumentParser(Protocol):
    def parse(self, source: SourceDocument, content: bytes) -> ParsedDocument: ...


class StrategyGenerator(Protocol):
    def propose(
        self, snapshot: ProblemSnapshot, context: PlanningContext = DEFAULT_PLANNING_CONTEXT
    ) -> StrategyProposal: ...


class BudgetTemplateParser(Protocol):
    def parse(self, source: SourceDocument, content: bytes) -> BudgetTemplate: ...


class ReportRenderer(Protocol):
    def render(self, report: ReportData) -> RenderedArtifact: ...


class ReportComposer(Protocol):
    def compose(self, report: ReportData, llm: LLMClient, control: RunControl) -> ReportOutline: ...


class WorkbookMappingAnalyzer(Protocol):
    def analyze(
        self,
        solution: SolutionBundle,
        template: BudgetTemplate,
        llm: LLMClient,
        control: RunControl,
        snapshot: ProblemSnapshot | None = None,
    ) -> WorkbookMapping: ...


class WorkbookRenderer(Protocol):
    def render(
        self,
        report: ReportData,
        template: BudgetTemplate | None,
        content: bytes | None,
        mapping: WorkbookMapping | None,
    ) -> RenderedArtifact: ...


class SpreadsheetConverter(Protocol):
    def convert(self, content: bytes) -> bytes: ...


class FormulaRecalculator(Protocol):
    def recalculate(self, content: bytes) -> RenderedArtifact: ...


class PlanEvaluator(Protocol):
    @property
    def supports_scoring(self) -> bool: ...

    def evaluate(self, snapshot: ProblemSnapshot, plan: Plan) -> Evaluation: ...


class RunControl(Protocol):
    def checkpoint(self, stage: str, completed: int = 0) -> None: ...


class Repository(Protocol):
    def put(self, kind: str, project_id: str, record_id: str, payload: object) -> None: ...
    def get(self, kind: str, project_id: str, record_id: str) -> JSONObject: ...
    def list(self, kind: str, project_id: str) -> list[JSONValue]: ...
    def list_all(self, kind: str) -> list[JSONObject]: ...
    def save_snapshot(
        self, project_id: str, payload: JSONObject, expected_version: int | None = None
    ) -> JSONObject: ...
    def snapshot(self, project_id: str, version: int | None = None) -> JSONObject: ...
    def versions(self, project_id: str) -> list[int]: ...
    def delete_project(self, project_id: str) -> None: ...
    def delete_upload(self, project_id: str, kind: str, record_id: str) -> JSONObject: ...
    def upload_deleted(self, project_id: str, kind: str, record_id: str) -> bool: ...


class JobRunner(Protocol):
    def submit(
        self,
        project_id: str,
        kind: str,
        operation: Callable[[RunControl], JSONValue],
        metadata: JSONObject | None = None,
    ) -> JSONObject: ...
    def cancel(self, project_id: str, job_id: str) -> JSONObject: ...
    def cancel_project(self, project_id: str) -> None: ...
    def close(self) -> None: ...
