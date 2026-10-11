from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, cast

from erp_agent.application.contracts import JSONObject, JSONValue, dumps, loads, obj
from erp_agent.domain.errors import ERPError


class SQLiteRepository:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, project_id TEXT NOT NULL, id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(kind, project_id, id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                project_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(project_id, version))""")
            connection.execute("CREATE TABLE IF NOT EXISTS deleted_projects (id TEXT PRIMARY KEY)")
            for record in self.list_all("job"):
                if record.get("status") in {"running", "queued"}:
                    record["status"] = "failed"
                    record["error"] = {"code": "server_restarted", "message": "服务重启，请重试任务。"}
                    connection.execute(
                        "UPDATE records SET payload=? WHERE kind='job' AND project_id=? AND id=?",
                        (dumps(record), str(record["project_id"]), str(record["id"])),
                    )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put(self, kind: str, project_id: str, record_id: str, payload: object) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_deleted(connection, project_id)
            connection.execute(
                "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,project_id,id) "
                "DO UPDATE SET payload=excluded.payload",
                (kind, project_id, record_id, dumps(payload)),
            )

    def get(self, kind: str, project_id: str, record_id: str) -> JSONObject:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM records WHERE kind=? AND project_id=? AND id=?",
                (kind, project_id, record_id),
            ).fetchone()
        if row is None:
            raise ERPError("not_found", "资源不存在或不属于当前项目。", 404)
        return obj(loads(row[0]))

    def list(self, kind: str, project_id: str) -> list[JSONValue]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM records WHERE kind=? AND project_id=? ORDER BY rowid",
                (kind, project_id),
            ).fetchall()
        return [loads(row[0]) for row in rows]

    def list_all(self, kind: str) -> list[JSONObject]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM records WHERE kind=?", (kind,),
            ).fetchall()
        return [obj(loads(row[0])) for row in rows]

    def save_snapshot(self, project_id: str, payload: JSONObject,
                      expected_version: int | None = None) -> JSONObject:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_deleted(connection, project_id)
            row = connection.execute(
                "SELECT MAX(version) FROM snapshots WHERE project_id=?", (project_id,),
            ).fetchone()
            latest = int(row[0] or 0)
            if expected_version is not None and latest != expected_version:
                raise ERPError("version_conflict", "题目已有新版本，请重新加载后修改。", 409)
            saved = dict(payload)
            saved["version"] = latest + 1
            connection.execute("INSERT INTO snapshots VALUES(?,?,?)",
                               (project_id, latest + 1, dumps(saved)))
        return saved

    def snapshot(self, project_id: str, version: int | None = None) -> JSONObject:
        with self._connection() as connection:
            if version is None:
                row = connection.execute(
                    "SELECT payload FROM snapshots WHERE project_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT payload FROM snapshots WHERE project_id=? AND version=?",
                    (project_id, version),
                ).fetchone()
        if row is None:
            raise ERPError("snapshot_not_found", "当前项目还没有读题结果。", 404)
        return obj(loads(row[0]))

    def versions(self, project_id: str) -> list[int]:
        with self._connection() as connection:
            rows = connection.execute("SELECT version FROM snapshots WHERE project_id=? "
                                      "ORDER BY version DESC", (project_id,)).fetchall()
        return [cast(int, row[0]) for row in rows]

    @staticmethod
    def _check_deleted(connection: sqlite3.Connection, project_id: str) -> None:
        if connection.execute("SELECT 1 FROM deleted_projects WHERE id=?", (project_id,)).fetchone():
            raise ERPError("project_deleted", "项目已删除。", 404)

    def delete_project(self, project_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM records WHERE kind='project' AND project_id=?", (project_id,),
            ).fetchone():
                raise ERPError("not_found", "项目不存在。", 404)
            connection.execute("INSERT OR IGNORE INTO deleted_projects VALUES(?)", (project_id,))
            connection.execute("DELETE FROM snapshots WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM records WHERE project_id=?", (project_id,))
