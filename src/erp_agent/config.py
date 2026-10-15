import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from erp_agent.domain.models import SolverMode

LLM_BASE_URLS = {
    "openai_chat": "https://api.openai.com/v1",
    "openai_responses": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://localhost:11434",
}


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().startswith("ERP_"):
            raise ValueError(".env 只能包含 ERP_ 开头的 KEY=VALUE 配置。")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path(".erp-data")
    access_key: str = field(default="", repr=False)
    cookie_secure: bool = False
    llm_provider: str = "openai_chat"
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    llm_format: str = "json_schema"
    llm_timeout: int = 90
    llm_retries: int = 2
    llm_max_input_chars: int = 100_000
    llm_max_output_tokens: int = 12_000
    llm_token_parameter: str = "max_completion_tokens"
    llm_thinking_mode: str = "default"
    llm_thinking_budget: int = 8192
    reader_max_request_chars: int = 100_000
    reader_max_batch_units: int = 16
    reader_concurrency: int = 4
    default_solver: SolverMode = SolverMode.LLM_ONLY
    libreoffice: str = "libreoffice"
    max_upload_mb: int = 20
    model_dispatch_strategy: str = "direct"
    model_role_candidates: tuple[tuple[str, tuple[str, ...]], ...] = ()
    task_simple_max_chars: int = 32000
    task_moderate_max_chars: int = 64000
    solver_max_model_calls: int = 32
    task_max_model_calls: int = 8
    task_deadline_seconds: int = 900

    def __post_init__(self) -> None:
        if self.model_dispatch_strategy not in {"direct", "agent"}:
            raise ValueError("模型调度策略必须为 direct 或 agent。")
        if not 1 <= self.task_simple_max_chars < self.task_moderate_max_chars:
            raise ValueError("轻量输入阈值必须为正且小于复杂输入阈值。")
        roles = {"reader", "operations", "finance", "coordinator", "budget", "report"}
        if any(
            role not in roles or not ids or any(not isinstance(i, str) or not i for i in ids)
            for role, ids in self.model_role_candidates
        ):
            raise ValueError("职责候选须为合法职责到预设 ID 列表的映射。")
        if not 1 <= self.solver_max_model_calls <= 256:
            raise ValueError("求解总调用上限须为 1–256。")
        if not 1 <= self.task_max_model_calls <= 32 or not 30 <= self.task_deadline_seconds <= 7200:
            raise ValueError("任务调用上限须为 1–32，截止时间须为 30–7200 秒。")
        if self.llm_provider not in LLM_BASE_URLS:
            raise ValueError("不支持的模型接口类型。")
        if not 1 <= self.reader_concurrency <= 4:
            raise ValueError("读题并发必须在 1 到 4 之间。")
        if not self.llm_base_url:
            object.__setattr__(self, "llm_base_url", LLM_BASE_URLS[self.llm_provider])
        url = urlsplit(self.llm_base_url)
        if not url.hostname:
            raise ValueError("模型地址必须包含有效的主机名。")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("模型地址端口无效。")
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if url.scheme != "https" and not (url.scheme == "http" and local):
            raise ValueError("模型地址必须使用 HTTPS，本机接口可使用 HTTP。")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("模型地址不能包含凭据、查询参数或片段。")
        if self.llm_format not in {"json_schema", "json_object", "text"}:
            raise ValueError("ERP_LLM_FORMAT 必须为 json_schema、json_object 或 text。")
        if self.llm_token_parameter not in {"max_completion_tokens", "max_tokens"}:
            raise ValueError("Token 上限参数必须为 max_completion_tokens 或 max_tokens。")
        if self.llm_thinking_mode not in {"default", "enabled", "disabled"}:
            raise ValueError("思考模式必须为 default、enabled 或 disabled。")
        if not 1024 <= self.llm_thinking_budget <= 393215:
            raise ValueError("思考预算必须在 1024 到 393215 Token 之间。")
        if not 0 <= self.port <= 65535:
            raise ValueError("端口必须在 0 到 65535 之间，0 表示自动分配。")
        if not 0 <= self.llm_retries <= 5:
            raise ValueError("模型重试次数必须在 0 到 5 之间。")
        if (
            min(
                self.max_upload_mb,
                self.llm_timeout,
                self.llm_max_input_chars,
                self.llm_max_output_tokens,
                self.reader_max_request_chars,
                self.reader_max_batch_units,
            )
            <= 0
        ):
            raise ValueError("资源限制必须为正数。")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("ERP_HOST", "127.0.0.1"),
            port=int(os.getenv("ERP_PORT", "8000")),
            data_dir=Path(os.getenv("ERP_DATA_DIR", ".erp-data")),
            access_key=os.getenv("ERP_ACCESS_KEY", ""),
            cookie_secure=os.getenv("ERP_COOKIE_SECURE", "false").lower() == "true",
            llm_provider=os.getenv("ERP_LLM_PROVIDER", "openai_chat"),
            llm_base_url=os.getenv("ERP_LLM_BASE_URL", ""),
            llm_api_key=os.getenv("ERP_LLM_API_KEY", ""),
            llm_model=os.getenv("ERP_LLM_MODEL", ""),
            llm_format=os.getenv("ERP_LLM_FORMAT", "json_schema"),
            llm_timeout=int(os.getenv("ERP_LLM_TIMEOUT", "90")),
            llm_retries=int(os.getenv("ERP_LLM_RETRIES", "2")),
            llm_max_input_chars=int(os.getenv("ERP_LLM_MAX_INPUT_CHARS", "100000")),
            llm_max_output_tokens=int(os.getenv("ERP_LLM_MAX_OUTPUT_TOKENS", "12000")),
            llm_token_parameter=os.getenv("ERP_LLM_TOKEN_PARAMETER", "max_completion_tokens"),
            llm_thinking_mode=os.getenv("ERP_LLM_THINKING_MODE", "default"),
            llm_thinking_budget=int(os.getenv("ERP_LLM_THINKING_BUDGET", "8192")),
            reader_max_request_chars=int(os.getenv("ERP_READER_MAX_REQUEST_CHARS", "100000")),
            reader_max_batch_units=int(os.getenv("ERP_READER_MAX_BATCH_UNITS", "16")),
            reader_concurrency=int(os.getenv("ERP_READER_CONCURRENCY", "4")),
            default_solver=SolverMode.LLM_ONLY,
            libreoffice=os.getenv("ERP_LIBREOFFICE", "libreoffice"),
            max_upload_mb=int(os.getenv("ERP_MAX_UPLOAD_MB", "20")),
            model_dispatch_strategy=os.getenv("ERP_MODEL_DISPATCH_STRATEGY", "direct"),
            model_role_candidates=tuple(
                (role, tuple(ids))
                for role, ids in json.loads(os.getenv("ERP_MODEL_ROLE_CANDIDATES", "{}")).items()
            ),
            task_simple_max_chars=int(os.getenv("ERP_TASK_SIMPLE_MAX_CHARS", "32000")),
            task_moderate_max_chars=int(os.getenv("ERP_TASK_MODERATE_MAX_CHARS", "64000")),
            solver_max_model_calls=int(os.getenv("ERP_SOLVER_MAX_MODEL_CALLS", "32")),
            task_max_model_calls=int(os.getenv("ERP_TASK_MAX_MODEL_CALLS", "8")),
            task_deadline_seconds=int(os.getenv("ERP_TASK_DEADLINE_SECONDS", "900")),
        )
