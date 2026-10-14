from dataclasses import dataclass
from typing import Protocol

from erp_agent.application.contracts import JSONObject
from erp_agent.domain.models import ProblemSnapshot, SolutionBundle


@dataclass(frozen=True)
class MemoryContext:
    project_id: str
    problem_version: int
    entries: tuple[JSONObject, ...] = ()
    solution_id: str | None = None
    object_id: str | None = None


class RoleMemory(Protocol):
    def alignment(self) -> JSONObject: ...
    def recall(
        self,
        query: str,
        max_chars: int = 1500,
        *,
        ids: tuple[str, ...] = (),
        entity_ids: tuple[str, ...] = (),
        exclude_ids: tuple[str, ...] = (),
        solution_id: str | None = None,
        object_id: str | None = None,
    ) -> MemoryContext: ...
    def get(self, key: str) -> JSONObject | None: ...
    def put(self, key: str, value: JSONObject) -> None: ...
    def evidence(self, ids: tuple[str, ...]) -> tuple[JSONObject, ...]: ...


class MemoryService(Protocol):
    def scope(
        self,
        project_id: str,
        version: int,
        fingerprint: str,
        document_ids: tuple[str, ...] = (),
        *,
        solution_id: str | None = None,
        object_id: str | None = None,
    ) -> RoleMemory: ...
    def remember(self, snapshot: ProblemSnapshot) -> None: ...
    def remember_solution(self, solution: SolutionBundle) -> None: ...
    def forget(self, project_id: str) -> None: ...
    def close(self) -> None: ...
