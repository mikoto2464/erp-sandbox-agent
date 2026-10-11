from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from erp_agent.application.contracts import JSONObject, JSONValue

from erp_agent.domain.models import (
    Evaluation,
    ParsedDocument,
    Plan,
    ProblemSnapshot,
    SourceDocument,
    StrategyProposal,
)


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    schema_name: str
    schema_json: str


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str


class LLMClient(Protocol):
    @property
    def configured(self) -> bool: ...

    def generate(self, request: LLMRequest) -> LLMResponse: ...


class ModelSettings(Protocol):
    def public(self) -> JSONObject: ...
    def update(self, data: JSONObject) -> JSONObject: ...


class DocumentConverter(Protocol):
    def convert(self, content: bytes) -> bytes: ...


class DocumentParser(Protocol):
    def parse(self, source: SourceDocument, content: bytes) -> ParsedDocument: ...


class StrategyGenerator(Protocol):
    def propose(self, snapshot: ProblemSnapshot) -> StrategyProposal: ...


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
    def save_snapshot(self, project_id: str, payload: JSONObject,
                      expected_version: int | None = None) -> JSONObject: ...
    def snapshot(self, project_id: str, version: int | None = None) -> JSONObject: ...
    def versions(self, project_id: str) -> list[int]: ...
    def delete_project(self, project_id: str) -> None: ...


class JobRunner(Protocol):
    def submit(self, project_id: str, kind: str,
               operation: Callable[[RunControl], JSONValue],
               metadata: JSONObject | None = None) -> JSONObject: ...
    def cancel(self, project_id: str, job_id: str) -> JSONObject: ...
    def cancel_project(self, project_id: str) -> None: ...
    def close(self) -> None: ...
