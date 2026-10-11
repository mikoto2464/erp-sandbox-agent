import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from erp_agent.domain.errors import ERPError

REMEMBER_SECONDS = 30 * 24 * 60 * 60
SESSION_SECONDS = 12 * 60 * 60


class SessionStore(Protocol):
    def save(self, digest: str, expires_at: float, key_digest: str) -> None: ...
    def valid(self, digest: str, now: float, key_digest: str) -> bool: ...
    def remove(self, digest: str) -> None: ...


@dataclass(frozen=True)
class LoginSession:
    token: str
    max_age: int | None


class AccessService:
    def __init__(self, key: str, store: SessionStore) -> None:
        self._key_digest = self._digest(key)
        self.configured = bool(key)
        self.store = store
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def login(self, key: str, remember: bool, address: str) -> LoginSession:
        if not self.configured:
            raise ERPError("access_not_configured", "请在服务器 .env 中设置 ERP_ACCESS_KEY。", 503)
        with self._lock:
            now = time.time()
            self._failures = {ip: item for ip, item in self._failures.items() if item[1] > now}
            attempts, until = self._failures.get(address, (0, now + 300))
            if attempts >= 10:
                raise ERPError("login_rate_limit", "尝试次数过多，请 5 分钟后重试。", 429)
            if not hmac.compare_digest(self._digest(key), self._key_digest):
                self._failures[address] = (attempts + 1, until)
                raise ERPError("invalid_access_key", "登录密钥不正确。", 401)
            self._failures.pop(address, None)
        duration = REMEMBER_SECONDS if remember else SESSION_SECONDS
        token = secrets.token_urlsafe(32)
        self.store.save(self._digest(token), now + duration, self._key_digest)
        return LoginSession(token, duration if remember else None)

    def authenticated(self, token: str) -> bool:
        return (self.configured and 20 <= len(token) <= 200
                and self.store.valid(self._digest(token), time.time(), self._key_digest))

    def logout(self, token: str) -> None:
        if token:
            self.store.remove(self._digest(token))
