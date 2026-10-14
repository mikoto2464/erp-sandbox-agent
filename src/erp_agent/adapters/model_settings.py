import os
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit
from uuid import uuid4

from erp_agent.adapters.llm import LangChainLLMClient
from erp_agent.adapters.model_catalog import protocol_catalog, requires_api_key
from erp_agent.application.contracts import JSONObject, array, dumps, integer, loads, obj, string
from erp_agent.application.model_dispatch import DISPATCH_VERSION
from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse, ModelBinding
from erp_agent.config import LLM_BASE_URLS, Settings
from erp_agent.domain.errors import ERPError

MODEL_FIELDS = {
    "llm_base_url",
    "llm_model",
    "llm_api_key",
    "llm_format",
    "llm_timeout",
    "llm_max_input_chars",
    "llm_max_output_tokens",
    "llm_provider",
    "llm_token_parameter",
    "llm_thinking_mode",
    "llm_thinking_budget",
}
SCHEDULING_FIELDS = {"priority", "max_parallel", "scheduling_enabled", "capabilities"}
EDIT_FIELDS = {
    "base_url",
    "model",
    "api_key",
    "clear_api_key",
    "format",
    "timeout",
    "max_input_chars",
    "max_output_tokens",
    "provider",
    "token_parameter",
    "thinking_mode",
    "thinking_budget",
}


class RuntimeLLMClient:
    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.current().configured

    @property
    def max_input_chars(self) -> int:
        return getattr(self.current(), "max_input_chars", 100_000)

    def current(self) -> LLMClient:
        with self._lock:
            return self._client

    def replace(self, client: LLMClient) -> None:
        with self._lock:
            self._client = client

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self.current().generate(request)


@dataclass(frozen=True)
class ModelPreset:
    id: str
    name: str
    revision: int
    settings: Settings
    priority: int = 100
    max_parallel: int = 2
    scheduling_enabled: bool = False
    capabilities: str = ""


class SchedulingOptions(TypedDict):
    priority: int
    max_parallel: int
    scheduling_enabled: bool
    capabilities: str


def _configuration(settings: Settings) -> JSONObject:
    return {
        "provider": settings.llm_provider,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "api_key_set": bool(settings.llm_api_key),
        "api_key_required": requires_api_key(settings),
        "format": settings.llm_format,
        "token_parameter": settings.llm_token_parameter,
        "timeout": settings.llm_timeout,
        "max_input_chars": settings.llm_max_input_chars,
        "max_output_tokens": settings.llm_max_output_tokens,
        "thinking_mode": settings.llm_thinking_mode,
        "thinking_budget": settings.llm_thinking_budget,
        "configured": LangChainLLMClient(settings).configured,
    }


def _configured_settings(current: Settings, data: JSONObject) -> Settings:
    if set(data) - EDIT_FIELDS:
        raise ERPError("invalid_model_settings", "模型设置包含未知字段。")
    key = string(data.get("api_key", ""), maximum=2048).strip()
    clear = data.get("clear_api_key", False)
    if not isinstance(clear, bool):
        raise ERPError("invalid_model_settings", "清除密钥选项无效。")
    provider = string(data.get("provider", current.llm_provider))
    if provider not in LLM_BASE_URLS:
        raise ERPError("invalid_model_settings", "不支持的模型接口类型。")
    changed_provider = provider != current.llm_provider
    default_url = LLM_BASE_URLS[provider] if changed_provider else current.llm_base_url
    base_url = string(data.get("base_url", default_url)).strip() or LLM_BASE_URLS[provider]
    changed_host = urlsplit(base_url).netloc != urlsplit(current.llm_base_url).netloc
    retained_key = "" if changed_provider or changed_host else current.llm_api_key
    try:
        return replace(
            current,
            llm_provider=provider,
            llm_base_url=base_url,
            llm_model=string(
                data.get("model", "" if changed_provider else current.llm_model), maximum=200
            ).strip(),
            llm_api_key="" if clear else key or retained_key,
            llm_format=string(data.get("format", current.llm_format)),
            llm_token_parameter=string(data.get("token_parameter", current.llm_token_parameter)),
            llm_thinking_mode=string(data.get("thinking_mode", current.llm_thinking_mode)),
            llm_thinking_budget=integer(
                data.get("thinking_budget", current.llm_thinking_budget), 1024, 393215
            ),
            llm_timeout=integer(data.get("timeout", current.llm_timeout), 5, 1800),
            llm_max_input_chars=integer(
                data.get("max_input_chars", current.llm_max_input_chars), 10000, 8000000
            ),
            llm_max_output_tokens=integer(
                data.get("max_output_tokens", current.llm_max_output_tokens), 256, 393216
            ),
        )
    except ValueError as exc:
        raise ERPError("invalid_model_settings", str(exc)) from exc


class ModelSettingsService:
    def __init__(self, settings: Settings, injected: LLMClient | None = None) -> None:
        self.path: Path = settings.data_dir / "model-settings.json"
        self._base = settings
        self._lock = threading.RLock()
        self._presets: dict[str, ModelPreset] = {}
        self._capacity = ModelCapacity()
        self._active_id = "default"
        migrate = False
        if self.path.is_file():
            saved = obj(loads(self.path.read_text(encoding="utf-8")))
            if "schema_version" in saved:
                obj(saved, {"schema_version", "active_id", "presets"})
                version = integer(saved["schema_version"], 1, 3)
                migrate = version < 3
                self._active_id = string(saved["active_id"])
                for item in array(saved["presets"]):
                    entry = obj(
                        item,
                        {"id", "name", "revision", "settings"}
                        | (
                            (
                                SCHEDULING_FIELDS
                                if version == 3
                                else SCHEDULING_FIELDS - {"capabilities"}
                            )
                            if version >= 2
                            else set()
                        ),
                    )
                    preset_id = string(entry["id"], maximum=100)
                    if not preset_id or preset_id in self._presets:
                        raise ValueError("模型预设 ID 为空或重复。")
                    config = self._load_settings(obj(entry["settings"]))
                    name = self._name(entry["name"])
                    self._presets[preset_id] = ModelPreset(
                        preset_id,
                        name,
                        integer(entry["revision"]),
                        config,
                        **self._scheduling(entry),
                    )
                if not 1 <= len(self._presets) <= 50 or self._active_id not in self._presets:
                    raise ValueError("模型预设数量或当前预设无效。")
                if len({p.name.casefold() for p in self._presets.values()}) != len(self._presets):
                    raise ValueError("模型预设名称重复。")
            else:
                settings = self._load_settings(saved)
                migrate = True
        if not self._presets:
            self._presets["default"] = ModelPreset("default", "默认配置", 1, settings)
        active = self._presets[self._active_id]
        self.settings = active.settings
        self.client = RuntimeLLMClient(injected or LangChainLLMClient(self.settings))
        self._sync_capacity()
        if migrate:
            self._persist(self._presets, self._active_id)

    @staticmethod
    def _scheduling(data: JSONObject, current: ModelPreset | None = None) -> SchedulingOptions:
        enabled = data.get("scheduling_enabled", current.scheduling_enabled if current else False)
        if not isinstance(enabled, bool):
            raise ERPError("invalid_model_settings", "参与调度选项必须为布尔值。")
        return {
            "priority": integer(
                data.get("priority", current.priority if current else 100), 0, 1000
            ),
            "max_parallel": integer(
                data.get("max_parallel", current.max_parallel if current else 2), 1, 16
            ),
            "scheduling_enabled": enabled,
            "capabilities": string(
                data.get("capabilities", current.capabilities if current else ""), 500
            ).strip(),
        }

    def _sync_capacity(self) -> None:
        self._capacity.configure({p.id: p.max_parallel for p in self._presets.values()})
        self._capacity.register(
            tuple(
                ModelBinding(
                    p.id,
                    p.name,
                    p.revision,
                    {**_configuration(p.settings), "max_parallel": p.max_parallel},
                    LangChainLLMClient(p.settings),
                )
                for p in self._presets.values()
            )
        )

    def _load_settings(self, saved: JSONObject) -> Settings:
        if set(saved) - MODEL_FIELDS:
            raise ValueError("模型设置文件包含未知字段。")
        public = {key.removeprefix("llm_"): value for key, value in saved.items()}
        public["api_key"] = public.get("api_key", "")
        return _configured_settings(replace(self._base, llm_api_key=""), public)

    @staticmethod
    def _name(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
            raise ERPError("invalid_preset_name", "预设名称必须在 1 到 80 字之间。")
        return value.strip()

    def _get(self, preset_id: str, revision: int | None = None) -> ModelPreset:
        preset = self._presets.get(preset_id)
        if preset is None:
            raise ERPError("preset_not_found", "模型预设不存在。", 404)
        if revision is not None and revision != preset.revision:
            raise ERPError("preset_conflict", "预设已更新，请重新加载后操作。", 409)
        return preset

    def _check_name(self, name: str, preset_id: str = "") -> None:
        if any(
            p.name.casefold() == name.casefold() and p.id != preset_id
            for p in self._presets.values()
        ):
            raise ERPError("preset_name_exists", "该预设名称已存在。", 409)

    @staticmethod
    def _public_preset(preset: ModelPreset) -> JSONObject:
        return {
            "id": preset.id,
            "name": preset.name,
            "revision": preset.revision,
            "priority": preset.priority,
            "max_parallel": preset.max_parallel,
            "scheduling_enabled": preset.scheduling_enabled,
            "capabilities": preset.capabilities,
            **_configuration(preset.settings),
        }

    def catalog(self) -> JSONObject:
        with self._lock:
            return {
                "active_id": self._active_id,
                "providers": protocol_catalog(),
                "presets": [self._public_preset(p) for p in self._presets.values()],
            }

    def public(self) -> JSONObject:
        with self._lock:
            return {
                **self._public_preset(self._get(self._active_id)),
                "active_id": self._active_id,
                "providers": protocol_catalog(),
                "configured": self.client.configured,
            }

    def capture(self) -> ModelBinding:
        with self._lock:
            preset = self._get(self._active_id)
            configuration = _configuration(preset.settings)
            configuration.pop("api_key_set")
            configuration.pop("api_key_required")
            return ModelBinding(
                preset.id, preset.name, preset.revision, configuration, self.client.current()
            )

    def capture_pool(self) -> ModelBinding:
        with self._lock:
            models = []
            for preset in sorted(self._presets.values(), key=lambda p: (p.id != self._active_id,)):
                client = (
                    self.client.current()
                    if preset.id == self._active_id
                    else LangChainLLMClient(preset.settings)
                )
                if not client.configured or not (
                    preset.id == self._active_id or preset.scheduling_enabled
                ):
                    continue
                configuration = _configuration(preset.settings)
                configuration.pop("api_key_set")
                configuration.pop("api_key_required")
                configuration.update(
                    priority=preset.priority,
                    max_parallel=preset.max_parallel,
                    capabilities=preset.capabilities,
                )
                models.append(
                    ModelBinding(preset.id, preset.name, preset.revision, configuration, client)
                )
            if not models:
                return self.capture()
            models.sort(key=lambda m: -int(m.configuration["priority"]))
            primary = models[0]
            return replace(
                primary,
                configuration={
                    **primary.configuration,
                    "model_pool": [m.metadata() for m in models],
                    "dispatcher_version": DISPATCH_VERSION,
                },
                client=ScheduledLLMClient(
                    tuple(models),
                    self._capacity,
                    strategy=self._base.model_dispatch_strategy,
                    role_candidates=dict(self._base.model_role_candidates),
                    task_max_calls=self._base.task_max_model_calls,
                    task_deadline_seconds=self._base.task_deadline_seconds,
                ),
            )

    def _persist(self, presets: dict[str, ModelPreset], active_id: str) -> None:
        payload = {
            "schema_version": 3,
            "active_id": active_id,
            "presets": [
                {
                    "id": p.id,
                    "name": p.name,
                    "revision": p.revision,
                    "priority": p.priority,
                    "max_parallel": p.max_parallel,
                    "scheduling_enabled": p.scheduling_enabled,
                    "capabilities": p.capabilities,
                    "settings": {
                        field: getattr(p.settings, field) for field in sorted(MODEL_FIELDS)
                    },
                }
                for p in presets.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="model-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(dumps(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _commit(
        self, presets: dict[str, ModelPreset], active_id: str, replace_client: bool = False
    ) -> None:
        self._persist(presets, active_id)
        self._presets, self._active_id = presets, active_id
        self._sync_capacity()
        self.settings = presets[active_id].settings
        if replace_client:
            self.client.replace(LangChainLLMClient(self.settings))

    def create_preset(self, data: JSONObject) -> JSONObject:
        with self._lock:
            if set(data) - EDIT_FIELDS - SCHEDULING_FIELDS - {"name"}:
                raise ERPError("invalid_model_settings", "模型预设包含未知字段。")
            if len(self._presets) >= 50:
                raise ERPError("preset_limit", "最多保存 50 个模型预设。", 409)
            name = self._name(data.get("name"))
            self._check_name(name)
            settings = _configured_settings(
                replace(self._base, llm_api_key="", llm_model=""),
                {k: v for k, v in data.items() if k != "name" and k not in SCHEDULING_FIELDS},
            )
            preset = ModelPreset(uuid4().hex, name, 1, settings, **self._scheduling(data))
            self._commit({**self._presets, preset.id: preset}, self._active_id)
            return self._public_preset(preset)

    def _edit(self, preset_id: str, data: JSONObject, expected: int | None) -> JSONObject:
        current = self._get(preset_id, expected)
        if set(data) - EDIT_FIELDS - SCHEDULING_FIELDS - {"name"}:
            raise ERPError("invalid_model_settings", "模型预设包含未知字段。")
        name = self._name(data.get("name", current.name))
        self._check_name(name, preset_id)
        settings = _configured_settings(
            current.settings,
            {k: v for k, v in data.items() if k != "name" and k not in SCHEDULING_FIELDS},
        )
        updated = replace(
            current,
            name=name,
            settings=settings,
            revision=current.revision + 1,
            **self._scheduling(data, current),
        )
        self._commit(
            {**self._presets, preset_id: updated},
            self._active_id,
            replace_client=preset_id == self._active_id,
        )
        return self._public_preset(updated)

    def update_preset(self, preset_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            expected = integer(data.get("expected_revision"))
            return self._edit(
                preset_id, {k: v for k, v in data.items() if k != "expected_revision"}, expected
            )

    def update(self, data: JSONObject) -> JSONObject:
        with self._lock:
            self._edit(self._active_id, data, None)
            return self.public()

    def activate(self, preset_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            obj(data, {"expected_revision"})
            preset = self._get(preset_id, integer(data["expected_revision"]))
            if not LangChainLLMClient(preset.settings).configured:
                raise ERPError("preset_not_configured", "请先补全此预设的模型名称与密钥。", 409)
            if preset_id != self._active_id:
                self._commit(dict(self._presets), preset_id, replace_client=True)
            return self.public()

    def delete_preset(self, preset_id: str, revision: int) -> None:
        with self._lock:
            self._get(preset_id, revision)
            if preset_id == self._active_id:
                raise ERPError("preset_in_use", "请先切换到其他预设，再删除当前预设。", 409)
            self._commit(
                {k: p for k, p in self._presets.items() if k != preset_id}, self._active_id
            )

    def test_preset(self, preset_id: str, data: JSONObject) -> JSONObject:
        with self._lock:
            obj(data, {"expected_revision"})
            preset = self._get(preset_id, integer(data["expected_revision"]))
            settings = replace(preset.settings, llm_retries=0)
        client = LangChainLLMClient(settings)
        schema = {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        }
        request = LLMRequest(
            "请只返回符合 JSON schema 的对象，不附加说明。\nJSON schema:\n" + dumps(schema),
            '返回 {"status":"ok"}。',
            "erp_connection_test",
            dumps(schema),
        )
        started = time.monotonic()
        response = client.generate(request)
        if loads(response.content) != {"status": "ok"}:
            raise ERPError("llm_test_contract", "接口可连接，但未返回符合要求的 JSON。", 502)
        return {
            "preset_id": preset.id,
            "preset_revision": preset.revision,
            "model": response.model,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "succeeded": True,
        }
