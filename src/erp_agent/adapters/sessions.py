import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SQLiteSessionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS access_sessions (
                digest TEXT PRIMARY KEY, expires_at REAL NOT NULL, key_digest TEXT NOT NULL)""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, digest: str, expires_at: float, key_digest: str) -> None:
        with self._connect() as connection:
            connection.execute("INSERT INTO access_sessions VALUES(?,?,?)",
                               (digest, expires_at, key_digest))

    def valid(self, digest: str, now: float, key_digest: str) -> bool:
        with self._connect() as connection:
            connection.execute("DELETE FROM access_sessions WHERE expires_at<=? OR key_digest!=?",
                               (now, key_digest))
            return connection.execute(
                "SELECT 1 FROM access_sessions WHERE digest=? AND expires_at>? AND key_digest=?",
                (digest, now, key_digest),
            ).fetchone() is not None

    def remove(self, digest: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM access_sessions WHERE digest=?", (digest,))
