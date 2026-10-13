from importlib import import_module
from dataclasses import replace
from functools import lru_cache
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlsplit

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.prompts import ChatPromptTemplate

from erp_agent.adapters.llm_diagnostics import MAX_RESPONSE_CHARS, validate_completion
from erp_agent.adapters.llm_errors import model_error
from erp_agent.adapters.model_catalog import requires_api_key
from erp_agent.application.contracts import dumps, loads, obj
from erp_agent.application.llm_budget import output_token_limit
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError


MODEL_CLASSES = {
    "openai_chat": ("langchain_openai", "ChatOpenAI"),
    "openai_responses": ("langchain_openai", "ChatOpenAI"),
    "anthropic": ("langchain_anthropic", "ChatAnthropic"),
    "gemini": ("langchain_google_genai", "ChatGoogleGenerativeAI"),
    "ollama": ("langchain_ollama", "ChatOllama"),
}


def _base_url(settings: Settings) -> str:
    base = settings.llm_base_url.rstrip("/")
    if settings.llm_provider == "gemini":
        if "/models/" in base:
            base = base.split("/models/", 1)[0]
        if base.endswith(("/v1", "/v1beta", "/v1alpha")):
            return base.rsplit("/", 1)[0]
    suffixes = {
        "openai_chat": ("/chat/completions",), "openai_responses": ("/responses",),
        "anthropic": ("/v1/messages", "/messages", "/v1"), "ollama": ("/api/chat",),
    }
    for suffix in suffixes.get(settings.llm_provider, ()):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def model_options(settings: Settings) -> dict[str, object]:
    provider = settings.llm_provider
    common: dict[str, object] = {"model": settings.llm_model, "max_retries": settings.llm_retries}
    if provider in {"openai_chat", "openai_responses"}:
        common.update(api_key=settings.llm_api_key, base_url=_base_url(settings),
                      timeout=settings.llm_timeout)
        extra: dict[str, object] = {}
        if provider == "openai_chat" and settings.llm_token_parameter == "max_tokens":
            # ChatOpenAI 会重命名 max_tokens；旧接口通过 extra_body 保留原参数。
            extra["max_tokens"] = settings.llm_max_output_tokens
        else:
            common["max_tokens"] = settings.llm_max_output_tokens
        if settings.llm_thinking_mode != "default":
            if provider == "openai_responses":
                raise ERPError("llm_thinking_unsupported", "Responses 请使用接口默认思考模式。")
            extra["thinking"] = {"type": settings.llm_thinking_mode}
        if extra:
            common["extra_body"] = extra
        if provider == "openai_responses":
            common.update(use_responses_api=True, store=False)
    elif provider == "anthropic":
        common.update(api_key=settings.llm_api_key, base_url=_base_url(settings),
                      timeout=settings.llm_timeout, max_tokens=settings.llm_max_output_tokens)
        if settings.llm_thinking_mode != "default":
            thinking: dict[str, object] = {"type": settings.llm_thinking_mode}
            if settings.llm_thinking_mode == "enabled":
                if settings.llm_thinking_budget >= settings.llm_max_output_tokens:
                    raise ERPError("llm_thinking_budget", "思考预算必须小于输出 Token 上限。")
                if settings.llm_format == "json_schema":
                    raise ERPError("llm_thinking_tools", "开启思考时请使用 JSON 对象或提示词约束 JSON。")
                thinking["budget_tokens"] = settings.llm_thinking_budget
            common["thinking"] = thinking
    elif provider == "gemini":
        if settings.llm_thinking_mode != "default":
            raise ERPError("llm_thinking_unsupported", "Gemini 请使用接口默认思考模式。")
        common.update(google_api_key=settings.llm_api_key,
                      max_output_tokens=settings.llm_max_output_tokens,
                      timeout=settings.llm_timeout, vertexai=False,
                      client_options={"api_endpoint": _base_url(settings)})
        version = urlsplit(settings.llm_base_url).path.split("/models/", 1)[0].rsplit("/", 1)[-1]
        common["api_version"] = version if version in {"v1", "v1beta", "v1alpha"} else "v1beta"
        # Google SDK 使用总尝试次数，其他接口配置额外重试次数。
        common["max_retries"] = settings.llm_retries + 1
    elif provider == "ollama":
        common.pop("max_retries")
        common.update(base_url=_base_url(settings), num_predict=settings.llm_max_output_tokens,
                      client_kwargs={"timeout": settings.llm_timeout})
        if settings.llm_api_key:
            common["client_kwargs"] = {"timeout": settings.llm_timeout,
                                       "headers": {"Authorization": f"Bearer {settings.llm_api_key}"}}
        if settings.llm_thinking_mode != "default":
            common["reasoning"] = settings.llm_thinking_mode == "enabled"
    return common


def create_chat_model(settings: Settings) -> BaseChatModel:
    module_name, class_name = MODEL_CLASSES[settings.llm_provider]
    try:
        model_class = getattr(import_module(module_name), class_name)
    except ModuleNotFoundError as error:
        raise ERPError("llm_dependency_missing",
                       "LangChain 模型依赖未安装，请运行 uv pip install --python .venv/bin/python -e .。",
                       503) from error
    model = model_class(**model_options(settings))
    if not isinstance(model, BaseChatModel):
        raise ERPError("llm_invalid_adapter", "模型适配器必须实现 LangChain BaseChatModel。", 503)
    return model


def _message_text(message: object) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content
                 if isinstance(block, dict) and block.get("type") in {"text", "output_text"}]
        return "".join(part for part in parts if isinstance(part, str))
    raise ERPError("llm_invalid_response", "LangChain 未返回有效消息内容。", 502)


class LangChainLLMClient:
    def __init__(self, settings: Settings,
                 model_factory: Callable[[Settings], BaseChatModel] = create_chat_model) -> None:
        self.settings = settings
        self.model_factory = model_factory

    @property
    def max_input_chars(self) -> int:
        return self.settings.llm_max_input_chars

    @property
    def configured(self) -> bool:
        return bool(self.settings.llm_model and (
            self.settings.llm_api_key or not requires_api_key(self.settings)
        ))

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.configured:
            raise ERPError("llm_not_configured", "请配置模型名称和所需 API 密钥。", 503)
        system = request.system
        if self.settings.llm_format == "json_schema":
            system = system.replace("\nJSON schema:\n" + request.schema_json, "")
        elif request.schema_json not in system:
            system += "\nJSON schema:\n" + request.schema_json
        if len(system) + len(request.user) > self.settings.llm_max_input_chars:
            raise ERPError("llm_input_limit", "材料超过输入字符限制，请拆分文件或调整预设。")
        settings = replace(self.settings,
                           llm_max_output_tokens=output_token_limit(
                               request, self.settings.llm_max_output_tokens,
                               self.settings.llm_thinking_mode, self.settings.llm_provider,
                               self.settings.llm_thinking_budget),
                           llm_retries=self.settings.llm_retries if request.retries is None else request.retries)
        started = monotonic()
        try:
            model = self._model(settings)
            if request.stream and "streaming" in type(model).model_fields:
                model = model.model_copy(update={"streaming": True})
            runnable: Runnable[Any, Any] = model
            schema = obj(loads(request.schema_json))
            structured = settings.llm_format == "json_schema"
            if structured:
                named_schema = {**schema, "title": request.schema_name}
                options: dict[str, object] = {"include_raw": True}
                if settings.llm_provider != "anthropic":
                    options["method"] = "json_schema"
                if settings.llm_provider in {"openai_chat", "openai_responses"}:
                    options["strict"] = True
                runnable = model.with_structured_output(named_schema, **options)
            elif settings.llm_format == "json_object":
                if settings.llm_provider in {"openai_chat", "openai_responses"}:
                    runnable = model.bind(response_format={"type": "json_object"})
                elif settings.llm_provider == "ollama":
                    runnable = model.bind(format="json")
                elif settings.llm_provider == "gemini":
                    runnable = model.bind(response_mime_type="application/json")
            messages = ChatPromptTemplate.from_messages([
                ("system", "{instructions}"), ("human", "{material}")
            ]).invoke({"instructions": system, "material": request.user}).to_messages()
            options = {"stream": True} if request.stream else {}
            output = runnable.invoke(messages, **options)
        except ERPError:
            raise
        except Exception as error:
            raise model_error(error, settings, request) from error
        if structured:
            if not isinstance(output, dict) or output.get("raw") is None:
                raise ERPError("llm_invalid_response", "LangChain 结构化输出缺少原始消息。", 502)
            message = output["raw"]
        else:
            message = output
        metadata = getattr(message, "response_metadata", {}) or {}
        usage = getattr(message, "usage_metadata", {}) or {}
        if not isinstance(metadata, dict) or not isinstance(usage, dict):
            raise ERPError("llm_invalid_response", "LangChain 消息元数据无效。", 502)
        if request.stream and not any(metadata.get(key) is not None for key in (
            "finish_reason", "stop_reason", "done_reason", "status",
        )) and metadata.get("done") is not True:
            raise ERPError("llm_incomplete", "模型流式响应缺少完成标记，当前结果未采用。", 502)
        validate_completion(metadata, usage, settings, self.settings.llm_max_output_tokens)
        additional = getattr(message, "additional_kwargs", {}) or {}
        if isinstance(additional, dict) and additional.get("refusal"):
            raise ERPError("llm_refusal", "模型拒绝生成结果。", 502)
        blocks = getattr(message, "content", [])
        if isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "refusal" for block in blocks
        ):
            raise ERPError("llm_refusal", "模型拒绝生成结果。", 502)
        if structured:
            if settings.llm_provider == "anthropic":
                calls = getattr(message, "tool_calls", [])
                if (len(calls) != 1 or calls[0].get("name") != request.schema_name
                        or metadata.get("stop_reason") != "tool_use"):
                    raise ERPError("llm_structured_response", "模型未返回唯一的结构化结果。", 502)
            if output.get("parsing_error") or not isinstance(output.get("parsed"), dict):
                raise ERPError("llm_structured_response", "模型未返回符合 Schema 的完整结构化结果。", 502)
            if settings.llm_provider != "anthropic":
                try:
                    raw_content = loads(_message_text(message))
                except ERPError as error:
                    raise ERPError("llm_structured_response", "模型原始输出不是完整的 JSON。", 502) from error
                if raw_content != output["parsed"]:
                    raise ERPError("llm_structured_response", "结构化结果与原始输出不一致。", 502)
            try:
                content = dumps(output["parsed"])
            except (TypeError, ValueError, RecursionError) as error:
                raise ERPError("llm_structured_response", "模型返回了无效的 JSON 数据。", 502) from error
        else:
            content = _message_text(message)
        if not content.strip():
            raise ERPError("llm_empty_response", "模型没有返回可用内容。", 502)
        if len(content) > MAX_RESPONSE_CHARS:
            raise ERPError("llm_response_limit", "模型返回内容超过安全大小限制。", 502)
        returned_model = metadata.get("model_name", metadata.get("model", settings.llm_model))
        if not isinstance(returned_model, str) or not returned_model:
            returned_model = settings.llm_model
        output = usage.get("output_tokens")
        details = usage.get("output_token_details")
        reasoning = details.get("reasoning") if isinstance(details, dict) else None
        return LLMResponse(
            content, returned_model, settings.llm_max_output_tokens,
            output if isinstance(output, int) and not isinstance(output, bool) else None,
            reasoning if isinstance(reasoning, int) and not isinstance(reasoning, bool) else None,
            round(monotonic() - started, 3),
        )

    @lru_cache(maxsize=8)
    def _model(self, settings: Settings) -> BaseChatModel:
        return self.model_factory(settings)
