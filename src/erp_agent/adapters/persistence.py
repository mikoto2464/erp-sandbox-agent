from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, cast

from erp_agent.application.contracts import JSONObject, JSONValue, array, dumps, integer, loads, obj
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
            connection.execute("""CREATE TABLE IF NOT EXISTS deleted_uploads (
                project_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                PRIMARY KEY(project_id, kind, id))""")
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
            if kind in {"document", "template", "original", "template_original"}:
                category = "document" if kind in {"document", "original"} else "template"
                self._check_upload(connection, project_id, category, record_id)
            if kind == "solution":
                data = obj(loads(dumps(payload)))
                for template_id in array(obj(data.get("generation", {})).get("template_ids", [])):
                    self._check_upload(connection, project_id, "template", str(template_id))
                snapshot = connection.execute("SELECT payload FROM snapshots WHERE project_id=? AND version=?",
                                              (project_id, data["problem_version"])).fetchone()
                if snapshot:
                    for document_id in array(obj(loads(snapshot[0])).get("document_ids", [])):
                        self._check_upload(connection, project_id, "document", str(document_id))
            if kind == "job":
                data = obj(loads(dumps(payload)))
                metadata = obj(data.get("metadata", {}))
                category = "document" if data.get("kind") == "parse" else "template"
                upload_id = metadata.get("document_id" if category == "document" else "template_id")
                if data.get("kind") in {"parse", "template"} and connection.execute(
                    "SELECT 1 FROM deleted_uploads WHERE project_id=? AND kind=? AND id=?",
                    (project_id, category, upload_id),
                ).fetchone():
                    data.update(status="cancelled", error=None)
                    metadata["upload_deleted"] = True
                    payload = data
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
                "SELECT payload FROM records r WHERE kind=? AND project_id=? "
                "AND NOT EXISTS (SELECT 1 FROM deleted_uploads d WHERE d.project_id=r.project_id "
                "AND d.kind=r.kind AND d.id=r.id) ORDER BY rowid",
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
            for document_id in array(payload.get("document_ids", [])):
                self._check_upload(connection, project_id, "document", str(document_id))
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
            connection.execute("DELETE FROM deleted_uploads WHERE project_id=?", (project_id,))

    @staticmethod
    def _check_upload(connection: sqlite3.Connection, project_id: str, kind: str, record_id: str) -> None:
        if connection.execute("SELECT 1 FROM deleted_uploads WHERE project_id=? AND kind=? AND id=?",
                              (project_id, kind, record_id)).fetchone():
            raise ERPError("upload_deleted", "文件已删除，请重新上传或选择其他文件。", 409)

    def upload_deleted(self, project_id: str, kind: str, record_id: str) -> bool:
        with self._connection() as connection:
            return bool(connection.execute(
                "SELECT 1 FROM deleted_uploads WHERE project_id=? AND kind=? AND id=?",
                (project_id, kind, record_id),
            ).fetchone())

    def delete_upload(self, project_id: str, kind: str, record_id: str) -> JSONObject:
        if kind not in {"document", "template"}:
            raise ERPError("invalid_upload_kind", "文件类型无效。")
        original = "original" if kind == "document" else "template_original"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_deleted(connection, project_id)
            self._check_upload(connection, project_id, kind, record_id)
            if not connection.execute("SELECT 1 FROM records WHERE kind=? AND project_id=? AND id=?",
                                      (original, project_id, record_id)).fetchone():
                raise ERPError("not_found", "文件不存在或不属于当前项目。", 404)
            connection.execute("INSERT INTO deleted_uploads VALUES(?,?,?)", (project_id, kind, record_id))
            if kind == "document":
                histories = connection.execute("SELECT payload FROM snapshots WHERE project_id=?", (project_id,)).fetchall()
                referenced = any(record_id in array(obj(loads(row[0])).get("document_ids", [])) for row in histories)
                row = connection.execute("SELECT payload FROM records WHERE kind='inputs' AND project_id=? AND id=?",
                                         (project_id, project_id)).fetchone()
                inputs: JSONObject | None = (obj(loads(row[0])) if row else
                                             {"revision": 0, "problem_text": "", "supplement_text": "", "annotations": []})
                inputs["revision"] = integer(inputs["revision"], 0) + 1
                inputs["annotations"] = [a for a in array(inputs["annotations"]) if obj(a)["document_id"] != record_id]
                connection.execute("INSERT OR REPLACE INTO records VALUES('inputs',?,?,?)",
                                   (project_id, project_id, dumps(inputs)))
            else:
                histories = connection.execute("SELECT payload FROM records WHERE kind='solution' AND project_id=?", (project_id,)).fetchall()
                referenced = any(record_id in array(obj(obj(loads(row[0])).get("generation", {})).get("template_ids", []))
                                 for row in histories)
                inputs = None
            if not referenced:
                connection.execute("DELETE FROM records WHERE project_id=? AND id=? AND kind IN (?,?)",
                                   (project_id, record_id, kind, original))
            for row in connection.execute("SELECT id,payload FROM records WHERE kind='job' AND project_id=?", (project_id,)).fetchall():
                job = obj(loads(row[1]))
                metadata = obj(job.get("metadata", {}))
                if (job.get("kind") == ("parse" if kind == "document" else "template")
                        and metadata.get("document_id" if kind == "document" else "template_id") == record_id):
                    metadata["upload_deleted"] = True
                    job.update(status="cancelled", error=None)
                    connection.execute("UPDATE records SET payload=? WHERE kind='job' AND project_id=? AND id=?",
                                       (dumps(job), project_id, row[0]))
        return {"deleted": True, "inputs": inputs}
