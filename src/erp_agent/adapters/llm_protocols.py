from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit

from erp_agent.application.contracts import JSONObject, array, dumps, loads, obj, string
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.config import LLM_BASE_URLS, Settings
from erp_agent.domain.errors import ERPError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PROVIDER_NAMES = {
    "openai_chat": "OpenAI / 兼容 Chat Completions",
    "openai_responses": "OpenAI Responses",
    "anthropic": "Anthropic Claude Messages",
    "gemini": "Google Gemini",
    "ollama": "Ollama",
}


@dataclass(frozen=True)
class HTTPModelRequest:
    url: str
    payload: JSONObject
    headers: dict[str, str]


class ModelProtocol(Protocol):
    def build(self, request: LLMRequest, settings: Settings) -> HTTPModelRequest: ...
    def parse(self, response: JSONObject, settings: Settings,
              request: LLMRequest) -> LLMResponse: ...


def protocol_catalog() -> list[JSONObject]:
    return [{"id": key, "name": name, "base_url": LLM_BASE_URLS[key],
             "api_key_required": key != "ollama"}
            for key, name in PROVIDER_NAMES.items()]


def requires_api_key(settings: Settings) -> bool:
    return settings.llm_provider != "ollama" or urlsplit(settings.llm_base_url).hostname not in {
        "localhost", "127.0.0.1", "::1",
    }


def _endpoint(base: str, suffix: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith(suffix) else base + suffix


def _bearer(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}


def _content(value: object) -> str:
    return string(value, maximum=MAX_RESPONSE_BYTES)


def _incomplete() -> ERPError:
    return ERPError("llm_incomplete", "模型输出未正常完成，请调整输出上限。", 502)


def _refusal() -> ERPError:
    return ERPError("llm_refusal", "模型拒绝生成结果。", 502)


class OpenAIChatProtocol:
    def build(self, request: LLMRequest, settings: Settings) -> HTTPModelRequest:
        payload: JSONObject = {
            "model": settings.llm_model,
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": request.user}],
            settings.llm_token_parameter: settings.llm_max_output_tokens,
        }
        if settings.llm_format != "text":
            response_format: JSONObject = {"type": settings.llm_format}
            if settings.llm_format == "json_schema":
                response_format["json_schema"] = {
                    "name": request.schema_name, "strict": True,
                    "schema": loads(request.schema_json),
                }
            payload["response_format"] = response_format
        return HTTPModelRequest(_endpoint(settings.llm_base_url, "/chat/completions"),
                                payload, _bearer(settings))

    def parse(self, response: JSONObject, settings: Settings, request: LLMRequest) -> LLMResponse:
        choices = array(response.get("choices"))
        if not choices:
            raise ERPError("llm_empty_response", "模型没有返回候选结果。", 502)
        choice = obj(choices[0])
        message = obj(choice.get("message"))
        if message.get("refusal"):
            raise _refusal()
        if choice.get("finish_reason") != "stop":
            raise _incomplete()
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(_content(obj(part).get("text")) for part in content
                              if obj(part).get("type") == "text")
        return LLMResponse(_content(content), string(response.get("model", settings.llm_model)))


class OpenAIResponsesProtocol:
    def build(self, request: LLMRequest, settings: Settings) -> HTTPModelRequest:
        payload: JSONObject = {"model": settings.llm_model, "instructions": request.system,
                               "input": request.user, "store": False,
                               "max_output_tokens": settings.llm_max_output_tokens}
        if settings.llm_format != "text":
            output_format: JSONObject = {"type": settings.llm_format}
            if settings.llm_format == "json_schema":
                output_format.update(name=request.schema_name, strict=True,
                                     schema=loads(request.schema_json))
            payload["text"] = {"format": output_format}
        return HTTPModelRequest(_endpoint(settings.llm_base_url, "/responses"),
                                payload, _bearer(settings))

    def parse(self, response: JSONObject, settings: Settings, request: LLMRequest) -> LLMResponse:
        if response.get("status") != "completed":
            raise _incomplete()
        parts: list[str] = []
        for value in array(response.get("output")):
            item = obj(value)
            if item.get("type") != "message":
                continue
            for block in array(item.get("content")):
                content = obj(block)
                if content.get("type") == "refusal":
                    raise _refusal()
                if content.get("type") == "output_text":
                    parts.append(_content(content.get("text")))
        return LLMResponse(_content("".join(parts)),
                           string(response.get("model", settings.llm_model)))


class AnthropicProtocol:
    def build(self, request: LLMRequest, settings: Settings) -> HTTPModelRequest:
        payload: JSONObject = {"model": settings.llm_model, "system": request.system,
                               "messages": [{"role": "user", "content": request.user}],
                               "max_tokens": settings.llm_max_output_tokens}
        if settings.llm_format == "json_schema":
            payload["tools"] = [{"name": request.schema_name,
                                 "description": "返回符合结构的完整结果",
                                 "input_schema": loads(request.schema_json)}]
            payload["tool_choice"] = {"type": "tool", "name": request.schema_name,
                                      "disable_parallel_tool_use": True}
        return HTTPModelRequest(_endpoint(settings.llm_base_url, "/messages"), payload,
                                {"x-api-key": settings.llm_api_key,
                                 "anthropic-version": "2023-06-01"})

    def parse(self, response: JSONObject, settings: Settings, request: LLMRequest) -> LLMResponse:
        reason = response.get("stop_reason")
        if reason == "refusal":
            raise _refusal()
        if reason not in {"end_turn", "tool_use"}:
            raise _incomplete()
        blocks = [obj(value) for value in array(response.get("content"))]
        if settings.llm_format == "json_schema":
            tools = [block for block in blocks if block.get("type") == "tool_use"]
            if (len(tools) != 1 or reason != "tool_use"
                    or tools[0].get("name") != request.schema_name):
                raise ERPError("llm_structured_response", "模型未返回唯一的结构化结果。", 502)
            content = dumps(obj(tools[0].get("input")))
        else:
            content = "".join(_content(block.get("text")) for block in blocks
                              if block.get("type") == "text")
        return LLMResponse(_content(content), string(response.get("model", settings.llm_model)))


class GeminiProtocol:
    def build(self, request: LLMRequest, settings: Settings) -> HTTPModelRequest:
        generation: JSONObject = {"maxOutputTokens": settings.llm_max_output_tokens}
        if settings.llm_format != "text":
            generation["responseMimeType"] = "application/json"
            if settings.llm_format == "json_schema":
                generation["responseJsonSchema"] = loads(request.schema_json)
        payload: JSONObject = {"systemInstruction": {"parts": [{"text": request.system}]},
                               "contents": [{"role": "user", "parts": [{"text": request.user}]}],
                               "generationConfig": generation}
        base = settings.llm_base_url.rstrip("/")
        model = settings.llm_model.removeprefix("models/")
        url = base if base.endswith(":generateContent") else (
            base + "/models/" + quote(model, safe="") + ":generateContent"
        )
        return HTTPModelRequest(url, payload, {"x-goog-api-key": settings.llm_api_key})

    def parse(self, response: JSONObject, settings: Settings, request: LLMRequest) -> LLMResponse:
        feedback = response.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise _refusal()
        candidates = array(response.get("candidates"))
        if not candidates:
            raise ERPError("llm_empty_response", "模型没有返回候选结果。", 502)
        candidate = obj(candidates[0])
        if candidate.get("finishReason") in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT"}:
            raise _refusal()
        if candidate.get("finishReason") != "STOP":
            raise _incomplete()
        parts = [obj(value) for value in array(obj(candidate.get("content")).get("parts"))]
        content = "".join(_content(part["text"]) for part in parts
                          if "text" in part and not part.get("thought"))
        return LLMResponse(_content(content),
                           string(response.get("modelVersion", settings.llm_model)))


class OllamaProtocol:
    def build(self, request: LLMRequest, settings: Settings) -> HTTPModelRequest:
        payload: JSONObject = {"model": settings.llm_model, "stream": False,
                               "messages": [{"role": "system", "content": request.system},
                                            {"role": "user", "content": request.user}],
                               "options": {"num_predict": settings.llm_max_output_tokens}}
        if settings.llm_format != "text":
            payload["format"] = (
                loads(request.schema_json) if settings.llm_format == "json_schema" else "json"
            )
        return HTTPModelRequest(_endpoint(settings.llm_base_url, "/api/chat"),
                                payload, _bearer(settings))

    def parse(self, response: JSONObject, settings: Settings, request: LLMRequest) -> LLMResponse:
        if response.get("done") is not True or response.get("done_reason") != "stop":
            raise _incomplete()
        return LLMResponse(_content(obj(response.get("message")).get("content")),
                           string(response.get("model", settings.llm_model)))


def create_protocol(provider: str) -> ModelProtocol:
    protocols: dict[str, type[ModelProtocol]] = {
        "openai_chat": OpenAIChatProtocol,
        "openai_responses": OpenAIResponsesProtocol,
        "anthropic": AnthropicProtocol,
        "gemini": GeminiProtocol,
        "ollama": OllamaProtocol,
    }
    return protocols[provider]()
