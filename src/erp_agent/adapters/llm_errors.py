import json
import re
from urllib.parse import quote

from erp_agent.adapters.llm_diagnostics import validate_completion
from erp_agent.application.ports import LLMRequest
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError

MAX_ERROR_BYTES = 32 * 1024


def _redact(text: str, settings: Settings, request: LLMRequest) -> str:
    key = settings.llm_api_key
    if key:
        for secret in {key, quote(key, safe=""), json.dumps(key)[1:-1]}:
            text = text.replace(secret, "[已隐藏]")
    for content in (request.system, request.user, request.schema_json):
        if content:
            text = text.replace(content, "[请求内容已隐藏]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [已隐藏]", text)
    text = re.sub(r"(?i)(api[-_ ]?key|token|secret|authorization)\s*[:=]\s*[^\s,;]+",
                  r"\1=[已隐藏]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏]", text)
    return " ".join(text.split())[:800]


def _diagnostic(error: Exception, settings: Settings, request: LLMRequest) -> str:
    root = getattr(error, "body", None)
    response = getattr(error, "response", None)
    if root is None and response is not None:
        try:
            if len(response.content) <= MAX_ERROR_BYTES:
                root = response.json()
        except (ValueError, TypeError, AttributeError):
            return ""
    if not isinstance(root, dict):
        return ""
    detail = root.get("error", root)
    if isinstance(detail, dict):
        parts = [detail.get("message"), detail.get("param"), detail.get("code")]
    elif isinstance(detail, str):
        parts = [detail]
    else:
        parts = []
    text = "；".join(p for p in parts if isinstance(p, str))
    if len(text) > MAX_ERROR_BYTES:
        return ""
    return _redact(text, settings, request)


def model_error(error: Exception, settings: Settings, request: LLMRequest) -> ERPError:
    name = type(error).__name__
    if isinstance(error, ImportError) and "socksio" in str(error).lower():
        return ERPError(
            "llm_proxy_dependency",
            "服务器配置了 SOCKS 代理，但缺少 socksio 依赖。请在项目虚拟环境中重新安装项目依赖，"
            "或将 ALL_PROXY/all_proxy 改为可用的 HTTP 代理后重启服务。", 503,
        )
    if name == "LengthFinishReasonError":
        completion = getattr(error, "completion", None)
        usage = getattr(completion, "usage", None)
        tokens = getattr(usage, "completion_tokens", None)
        details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None)
        validate_completion({"finish_reason": "length"}, {
            "output_tokens": tokens, "output_token_details": {"reasoning": reasoning},
        }, settings)
    if name == "ContentFilterFinishReasonError":
        return ERPError("llm_refusal", "模型拒绝生成结果。", 502)
    if isinstance(error, TimeoutError) or "Timeout" in name:
        return ERPError("llm_timeout", "模型请求超时，请检查连接或调整预设超时。", 504)
    if "Connection" in name or name == "ConnectError":
        return ERPError("llm_connection", "无法连接模型接口，请检查服务地址及服务器代理。", 502)
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = getattr(response, "status_code", None)
    if not isinstance(status, int) or not 100 <= status <= 599:
        return ERPError("llm_adapter_error", "LangChain 模型调用失败，请检查接口类型与模型参数。", 502)
    if status in {502, 503, 504}:
        return ERPError("llm_gateway_timeout" if status == 504 else "llm_unavailable",
                        f"模型接口返回 HTTP {status}，上游暂时无法完成请求。", status)
    message = f"模型接口返回 HTTP {status}。"
    diagnostic = _diagnostic(error, settings, request) if status in {400, 422} else ""
    if diagnostic:
        message += f" 接口说明：{diagnostic}。"
        lower = diagnostic.lower()
        if "tool_choice" in lower or "forced" in lower:
            message += " 可在此预设中将输出格式改为“JSON 对象”或“提示词约束 JSON”后测试。"
        elif "max_completion_tokens" in lower:
            message += " 可在此预设高级参数中将 Token 上限参数改为 max_tokens。"
        elif "json_schema" in lower or "response_format" in lower:
            message += " 请检查此模型支持的输出格式，可改为“JSON 对象”后测试。"
    return ERPError("llm_http_error", message, 502)
