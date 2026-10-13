import hashlib
import re
import sqlite3
import threading
from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver
from rank_bm25 import BM25Okapi

from erp_agent.application.contracts import JSONObject, dumps, obj, to_json
from erp_agent.application.memory import MemoryContext
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import KnowledgeState, ProblemSnapshot


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
    ) -> None:
        self.service = service
        self.project_id, self.version = project_id, version
        self.fingerprint, self.document_ids = fingerprint, document_ids

    def recall(self, query: str, max_chars: int = 1500) -> MemoryContext:
        saved = self.service.read(self.project_id, f"knowledge:{self.version}")
        entries = saved.get("entries", []) if saved else []
        short = self.service.read(self.project_id, f"context:{self.fingerprint}")
        if isinstance(entries, list) and short and isinstance(short.get("entries"), list):
            entries = entries + short["entries"]
        if not isinstance(entries, list) or not entries:
            return MemoryContext(self.project_id, self.version)
        records = [obj(entry) for entry in entries]
        corpus = [_tokens(str(record.get("text", ""))) for record in records]
        scores = BM25Okapi(corpus).get_scores(_tokens(query))
        order = sorted(range(len(records)), key=lambda i: scores[i], reverse=True)
        selected: list[JSONObject] = []
        for index in order:
            if len(selected) >= 8:
                break
            if not set(corpus[index]).intersection(_tokens(query)):
                continue
            candidate = selected + [records[index]]
            if len(dumps(candidate)) <= max_chars:
                selected = candidate
        return MemoryContext(self.project_id, self.version, tuple(selected))

    def get(self, key: str) -> JSONObject | None:
        return self.service.read(self.project_id, f"run:{self.fingerprint}:{key}")

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
                    text = (f"实体对齐线索：{fact.get('category')} {fact.get('entity_name')} "
                            f"key={fact.get('key')}；提取未经汇总校验")
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

    def scope(
        self, project_id: str, version: int, fingerprint: str, document_ids: tuple[str, ...] = ()
    ) -> SQLiteRoleMemory:
        return SQLiteRoleMemory(self, project_id, version, fingerprint, document_ids)

    @staticmethod
    def _thread(project_id: str, key: str) -> str:
        return project_id + ":memory:" + hashlib.sha256(key.encode()).hexdigest()

    def _check(self, project_id: str, document_ids: tuple[str, ...] = ()) -> None:
        if self.connection.execute(
            "SELECT 1 FROM deleted_projects WHERE id=?", (project_id,)
        ).fetchone():
            raise ERPError("project_deleted", "项目已删除。", 404)
        for document_id in document_ids:
            if self.connection.execute(
                "SELECT 1 FROM deleted_uploads WHERE project_id=? AND kind='document' AND id=?",
                (project_id, document_id),
            ).fetchone():
                raise ERPError("upload_deleted", "文件已删除，停止保存批次记忆。", 409)

    def read(self, project_id: str, key: str) -> JSONObject | None:
        with self.lock:
            self._check(project_id)
            saved = self.saver.get_tuple(
                {"configurable": {"thread_id": self._thread(project_id, key)}}
            )
            return obj(to_json(saved.checkpoint["channel_values"]["value"])) if saved else None

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
                    "state": fact.state.value,
                    "text": (f"{fact.entity_name} {fact.key}={fact.value} {fact.unit}；"
                             + fact.explanation),
                    "evidence_ids": list(fact.evidence_ids),
                }
            )
        for rule in snapshot.reading.rules.rules:
            if rule.state not in {KnowledgeState.KNOWN, KnowledgeState.ASSUMED}:
                continue
            entries.append(
                {
                    "id": rule.id,
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
        self.write(
            snapshot.project_id,
            f"knowledge:{snapshot.version}",
            {"version": snapshot.version, "entries": entries},
            snapshot.document_ids,
        )

    def forget(self, project_id: str) -> None:
        with self.lock:
            rows = self.connection.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
                (project_id + ":memory:%",),
            ).fetchall()
            for (thread,) in rows:
                self.saver.delete_thread(thread)

    def close(self) -> None:
        with self.lock:
            self.connection.close()
