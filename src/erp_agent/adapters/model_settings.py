import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from erp_agent.adapters.llm import CompatibleLLMClient
from erp_agent.application.contracts import JSONObject, dumps, integer, loads, obj, string
from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError

MODEL_FIELDS = {"llm_base_url", "llm_model", "llm_api_key", "llm_format", "llm_timeout",
                "llm_max_input_chars", "llm_max_output_tokens"}


class RuntimeLLMClient:
    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.current().configured

    def current(self) -> LLMClient:
        with self._lock:
            return self._client

    def replace(self, client: LLMClient) -> None:
        with self._lock:
            self._client = client

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self.current().generate(request)


class ModelSettingsService:
    def __init__(self, settings: Settings, injected: LLMClient | None = None) -> None:
        self.path: Path = settings.data_dir / "model-settings.json"
        if self.path.is_file():
            saved = obj(loads(self.path.read_text(encoding="utf-8")))
            if set(saved) - MODEL_FIELDS:
                raise ValueError("模型设置文件包含未知字段。")
            settings = replace(settings, **saved)
        self.settings = settings
        self.client = RuntimeLLMClient(injected or CompatibleLLMClient(settings))
        self._lock = threading.Lock()

    def public(self) -> JSONObject:
        settings = self.settings
        return {"base_url": settings.llm_base_url, "model": settings.llm_model,
                "api_key_set": bool(settings.llm_api_key), "format": settings.llm_format,
                "timeout": settings.llm_timeout, "max_input_chars": settings.llm_max_input_chars,
                "max_output_tokens": settings.llm_max_output_tokens,
                "configured": self.client.configured}

    def update(self, data: JSONObject) -> JSONObject:
        if set(data) - {"base_url", "model", "api_key", "clear_api_key", "format", "timeout",
                        "max_input_chars", "max_output_tokens"}:
            raise ERPError("invalid_model_settings", "模型设置包含未知字段。")
        with self._lock:
            current = self.settings
            key = string(data.get("api_key", ""), maximum=2048).strip()
            clear = data.get("clear_api_key", False)
            if not isinstance(clear, bool):
                raise ERPError("invalid_model_settings", "清除密钥选项无效。")
            changes = {
                "llm_base_url": string(data.get("base_url", current.llm_base_url)).strip(),
                "llm_model": string(data.get("model", current.llm_model), maximum=200).strip(),
                "llm_api_key": "" if clear else key or current.llm_api_key,
                "llm_format": string(data.get("format", current.llm_format)),
                "llm_timeout": integer(data.get("timeout", current.llm_timeout), 5, 300),
                "llm_max_input_chars": integer(data.get("max_input_chars",
                                                        current.llm_max_input_chars), 10000, 1000000),
                "llm_max_output_tokens": integer(data.get("max_output_tokens",
                                                          current.llm_max_output_tokens), 256, 64000),
            }
            try:
                updated = replace(current, **changes)
            except ValueError as exc:
                raise ERPError("invalid_model_settings", str(exc)) from exc
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="model-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(dumps(changes))
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.settings = updated
            self.client.replace(CompatibleLLMClient(updated))
        return self.public()
