import hashlib
import re
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver
from rank_bm25 import BM25Okapi

from erp_agent.application.contracts import JSONObject, dumps, loads, obj, to_json
from erp_agent.application.memory import MemoryContext
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import KnowledgeState, ProblemSnapshot, SolutionBundle


@dataclass
class MemoryIndex:
    records: list[JSONObject]
    corpus: list[list[str]]
    bm25: BM25Okapi
    by_id: dict[str, list[int]]
    by_entity: dict[str, list[int]]
    chars: int


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    return [
        token
        for word in words
        for token in (
            [word]
            if word.isascii() or len(word) < 2
            else [word[i : i + 2] for i in range(len(word) - 1)]
        )
    ] or [""]


class SQLiteRoleMemory:
    def __init__(
        self,
        service: "SQLiteMemoryService",
        project_id: str,
        version: int,
        fingerprint: str,
        document_ids: tuple[str, ...],
        solution_id: str | None = None,
        object_id: str | None = None,
    ) -> None:
        self.service = service
        self.project_id, self.version = project_id, version
        self.fingerprint, self.document_ids = fingerprint, document_ids
        self.solution_id, self.object_id = solution_id, object_id

    def alignment(self) -> JSONObject:
        saved = self.service.read(self.project_id, f"knowledge:{self.version}") or {}
        return obj(saved.get("alignment", {}))

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
    ) -> MemoryContext:
        saved = self.service.read(self.project_id, f"knowledge:{self.version}")
        entries = saved.get("entries", []) if saved else []
        short = self.service.read(self.project_id, f"context:{self.fingerprint}")
        if isinstance(entries, list) and short and isinstance(short.get("entries"), list):
            entries = entries + short["entries"]
        if not isinstance(entries, list) or not entries:
            return MemoryContext(self.project_id, self.version)
        current_solution = solution_id or self.solution_id
        current_object = object_id or self.object_id
        entries = [obj(entry) for entry in entries if isinstance(entry, dict)]
        if current_solution:
            solution = self.get("solution:" + current_solution)
            if solution:
                plan = obj(solution.get("plan", {}))
                for action in plan.get("actions", []):
                    if not isinstance(action, dict):
                        continue
                    entries.append(
                        {
                            "id": action.get("action_id", ""),
                            "entity_id": action.get("entity_id", ""),
                            "object_id": action.get("entity_id", ""),
                            "solution_id": current_solution,
                            "dependency_ids": action.get("depends_on", []),
                            "text": dumps(action),
                            "kind": "action",
                            "evidence_ids": [],
                        }
                    )
        entries = [
            entry
            for entry in entries
            if (not entry.get("solution_id") or entry["solution_id"] == current_solution)
            and (
                not current_object
                or not entry.get("object_id")
                or entry["object_id"] == current_object
                or entry.get("id") in ids
            )
        ]
        if not entries:
            return MemoryContext(
                self.project_id, self.version, (), current_solution, current_object
            )
        index = self.service.index(
            self.project_id,
            self.version,
            self.fingerprint,
            current_solution,
            current_object,
            entries,
        )
        records, corpus = index.records, index.corpus
        scores = index.bm25.get_scores(_tokens(query))
        exact = {i for identifier in ids for i in index.by_id.get(identifier, [])}
        exact.update(i for identifier in entity_ids for i in index.by_entity.get(identifier, []))
        for i in tuple(exact):
            for dependency in records[i].get("dependency_ids", []):
                exact.update(index.by_id.get(str(dependency), []))
        order = sorted(exact) + [
            i
            for i in sorted(range(len(records)), key=lambda i: scores[i], reverse=True)
            if i not in exact
        ]
        query_tokens = set(_tokens(query))
        selected: list[JSONObject] = []
        for index in order:
            if len(selected) >= 8:
                break
            if records[index].get("id") in exclude_ids:
                continue
            if index not in exact and not set(corpus[index]).intersection(query_tokens):
                continue
            candidate = selected + [records[index]]
            if len(dumps(candidate)) <= max_chars:
                selected = candidate
        return MemoryContext(
            self.project_id, self.version, tuple(selected), current_solution, current_object
        )

    def get(self, key: str) -> JSONObject | None:
        if key.startswith("solution:"):
            if self.solution_id and key != "solution:" + self.solution_id:
                return None
            return self.service.read(
                self.project_id, f"solution:{self.version}:" + key.split(":", 1)[1]
            )
        return self.service.read(self.project_id, f"run:{self.fingerprint}:{key}")

    def evidence(self, ids: tuple[str, ...]) -> tuple[JSONObject, ...]:
        """来源 ID 精确读取原文，常规上下文不重复携带原文件。"""
        saved = self.service.read(self.project_id, f"knowledge:{self.version}")
        if not saved:
            return ()
        lookup = obj(saved.get("evidence", {}))
        return tuple(obj(lookup[identifier]) for identifier in ids if identifier in lookup)

    def put(self, key: str, value: JSONObject) -> None:
        self.service.write(
            self.project_id, f"run:{self.fingerprint}:{key}", value, self.document_ids
        )
        reading = value.get("reading")
        if isinstance(reading, dict) and isinstance(reading.get("facts"), list):
            with self.service.lock:
                current = self.service.read(self.project_id, f"context:{self.fingerprint}") or {
                    "entries": []
                }
                entries = current["entries"]
                assert isinstance(entries, list)
                known = {str(entry.get("text", "")) for entry in entries if isinstance(entry, dict)}
                for fact in reading["facts"]:
                    if not isinstance(fact, dict):
                        continue
                    text = (
                        f"实体对齐线索：{fact.get('category')} {fact.get('entity_name')} "
                        f"key={fact.get('key')}；提取未经汇总校验"
                    )
                    if text not in known:
                        entries.append({"text": text, "state": "provisional", "evidence_ids": []})
                        known.add(text)
                self.service.write(
                    self.project_id,
                    f"context:{self.fingerprint}",
                    {"entries": entries[-200:]},
                    self.document_ids,
                )


class SQLiteMemoryService:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.saver = SqliteSaver(self.connection)
        self.saver.setup()
        self.lock = threading.RLock()
        self._indexes: OrderedDict[tuple, MemoryIndex] = OrderedDict()
        self._index_chars = 0
        self.index_max_entries, self.index_max_chars = 24, 8_000_000

    def scope(
        self,
        project_id: str,
        version: int,
        fingerprint: str,
        document_ids: tuple[str, ...] = (),
        *,
        solution_id: str | None = None,
        object_id: str | None = None,
    ) -> SQLiteRoleMemory:
        return SQLiteRoleMemory(
            self, project_id, version, fingerprint, document_ids, solution_id, object_id
        )

    def invalidate(self, project_id: str) -> None:
        with self.lock:
            for key in tuple(self._indexes):
                if key[0] == project_id:
                    self._index_chars -= self._indexes.pop(key).chars

    def index(
        self,
        project_id: str,
        version: int,
        fingerprint: str,
        solution_id: str | None,
        object_id: str | None,
        entries: list[JSONObject],
    ) -> MemoryIndex:
        serialized = dumps(entries)
        key = (
            project_id,
            version,
            fingerprint,
            solution_id,
            object_id,
            hashlib.sha256(serialized.encode()).hexdigest(),
        )
        with self.lock:
            found = self._indexes.get(key)
            if found:
                self._indexes.move_to_end(key)
                return found
            corpus = [_tokens(str(record.get("text", ""))) for record in entries]
            by_id: dict[str, list[int]] = {}
            by_entity: dict[str, list[int]] = {}
            for i, record in enumerate(entries):
                for identifier in (record.get("id", ""), *record.get("evidence_ids", [])):
                    if identifier:
                        by_id.setdefault(str(identifier), []).append(i)
                entity_id = record.get("entity_id")
                if entity_id:
                    by_entity.setdefault(str(entity_id), []).append(i)
            result = MemoryIndex(
                entries, corpus, BM25Okapi(corpus), by_id, by_entity, len(serialized)
            )
            if result.chars <= self.index_max_chars:
                self._indexes[key] = result
                self._index_chars += result.chars
                while (
                    len(self._indexes) > self.index_max_entries
                    or self._index_chars > self.index_max_chars
                ):
                    self._index_chars -= self._indexes.popitem(last=False)[1].chars
            return result

    @staticmethod
    def _thread(project_id: str, key: str) -> str:
        return project_id + ":memory:" + hashlib.sha256(key.encode()).hexdigest()

    def _check(self, project_id: str, document_ids: tuple[str, ...] = ()) -> None:
        if self.connection.execute(
            "SELECT 1 FROM deleted_projects WHERE id=?", (project_id,)
        ).fetchone():
            self.invalidate(project_id)
            raise ERPError("project_deleted", "项目已删除。", 404)
        for document_id in document_ids:
            if self.connection.execute(
                "SELECT 1 FROM deleted_uploads WHERE project_id=? AND kind='document' AND id=?",
                (project_id, document_id),
            ).fetchone():
                self.invalidate(project_id)
                raise ERPError("upload_deleted", "文件已删除，停止保存批次记忆。", 409)

    def read(self, project_id: str, key: str) -> JSONObject | None:
        with self.lock:
            self._check(project_id)
            saved = self.saver.get_tuple(
                {"configurable": {"thread_id": self._thread(project_id, key)}}
            )
            value = obj(to_json(saved.checkpoint["channel_values"]["value"])) if saved else None
            if value and key.startswith("knowledge:"):
                alignment = obj(value.get("alignment", {}))
                documents = alignment.get("document_ids", [])
                if isinstance(documents, list):
                    self._check(project_id, tuple(str(item) for item in documents))
            return value

    def write(
        self, project_id: str, key: str, value: JSONObject, document_ids: tuple[str, ...] = ()
    ) -> None:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._check(project_id, document_ids)
                thread = self._thread(project_id, key)
                checkpoint = empty_checkpoint()
                checkpoint["id"] = "current"
                checkpoint["channel_values"] = {"value": value}
                checkpoint["channel_versions"] = {"value": 1}
                self.saver.put(
                    {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                    checkpoint,
                    {"source": "update", "step": 0, "parents": {}, "project_id": project_id},
                    {"value": 1},
                )
                self.invalidate(project_id)
            except Exception:
                self.connection.rollback()
                raise

    def remember(self, snapshot: ProblemSnapshot) -> None:
        entries: list[JSONObject] = []
        for fact in snapshot.reading.problem.facts:
            if fact.state not in {KnowledgeState.KNOWN, KnowledgeState.ASSUMED}:
                continue
            entries.append(
                {
                    "id": fact.id,
                    "entity_id": fact.entity_id,
                    "kind": "fact",
                    "state": fact.state.value,
                    "text": (
                        f"{fact.entity_name} {fact.key}={fact.value} {fact.unit}；"
                        + fact.explanation
                    ),
                    "evidence_ids": list(fact.evidence_ids),
                }
            )
        for rule in snapshot.reading.rules.rules:
            if rule.state not in {KnowledgeState.KNOWN, KnowledgeState.ASSUMED}:
                continue
            entries.append(
                {
                    "id": rule.id,
                    "kind": "rule",
                    "state": rule.state.value,
                    "text": f"{rule.name}：{rule.expression}；{rule.explanation}",
                    "evidence_ids": list(rule.evidence_ids),
                }
            )
        for issue in snapshot.reading.issues:
            if issue.severity == "info" and issue.code.startswith("review.resolved."):
                entries.append(
                    {
                        "id": issue.target,
                        "state": "resolved",
                        "text": issue.message,
                        "evidence_ids": list(issue.evidence_ids),
                    }
                )
        for template in snapshot.template_readings:
            for sheet in template.sheets:
                entries.append(
                    {
                        "id": template.template_id + ":" + sheet.name,
                        "state": "reviewed" if template.reviewed else "unreviewed",
                        "text": f"空表 {sheet.name}：{sheet.purpose}；期间 {sheet.periods}；"
                        + dumps(sheet.input_groups)
                        + "；"
                        + template.explanation
                        + "；逐条核对："
                        + dumps(template.issue_reviews),
                        "evidence_ids": [],
                        "template_id": template.template_id,
                        "sha256": template.sha256,
                    }
                )
        evidence: JSONObject = {
            ref.id: {
                "id": ref.id,
                "document_id": ref.document_id,
                "location": ref.location,
                "text": ref.text,
            }
            for document in snapshot.additional_sources
            for ref in document.evidence
        }
        with self.lock:
            self._check(snapshot.project_id, snapshot.document_ids)
            for document_id in snapshot.document_ids:
                row = self.connection.execute(
                    "SELECT payload FROM records WHERE kind='document' AND project_id=? AND id=?",
                    (snapshot.project_id, document_id),
                ).fetchone()
                if row:
                    document = obj(loads(row[0]))
                    for ref in document.get("evidence", []):
                        if isinstance(ref, dict) and ref.get("id"):
                            evidence[str(ref["id"])] = {
                                key: ref[key]
                                for key in ("id", "document_id", "location", "text")
                                if key in ref
                            }
        self.write(
            snapshot.project_id,
            f"knowledge:{snapshot.version}",
            {
                "version": snapshot.version,
                "entries": entries,
                "evidence": evidence,
                "alignment": {
                    "project_id": snapshot.project_id,
                    "problem_version": snapshot.version,
                    "rule_version": snapshot.reading.rules.version,
                    "document_ids": list(snapshot.document_ids),
                    "years": snapshot.reading.problem.years,
                    "periods_per_year": snapshot.reading.problem.periods_per_year,
                    "templates": [
                        {"id": t.template_id, "sha256": t.sha256, "reviewed": t.reviewed}
                        for t in snapshot.template_readings
                    ],
                },
            },
            snapshot.document_ids,
        )

    def forget(self, project_id: str) -> None:
        with self.lock:
            self.invalidate(project_id)
            rows = self.connection.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
                (project_id + ":memory:%",),
            ).fetchall()
            for (thread,) in rows:
                self.saver.delete_thread(thread)

    def close(self) -> None:
        with self.lock:
            self._indexes.clear()
            self.connection.close()

    def remember_solution(self, solution: SolutionBundle) -> None:
        self.write(
            solution.project_id,
            f"solution:{solution.problem_version}:{solution.id}",
            {
                "solution_id": solution.id,
                "problem_version": solution.problem_version,
                "generation": to_json(solution.generation),
                "plan": to_json(solution.plan),
                "evaluation": to_json(solution.evaluation),
                "prompt_version": solution.prompt_version,
            },
        )
