import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from erp_agent.adapters.llm_protocols import MAX_RESPONSE_BYTES, create_protocol, requires_api_key
from erp_agent.application.contracts import dumps, loads, obj
from erp_agent.application.ports import LLMRequest, LLMResponse
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        return None


class CompatibleLLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.protocol = create_protocol(settings.llm_provider)

    @property
    def configured(self) -> bool:
        return bool(self.settings.llm_model and (
            self.settings.llm_api_key or not requires_api_key(self.settings)
        ))

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.configured:
            raise ERPError("llm_not_configured", "请在服务端配置模型名称和 API 密钥。", 503)
        if len(request.system) + len(request.user) > self.settings.llm_max_input_chars:
            raise ERPError("llm_input_limit", "材料超过模型输入限制，请拆分文件或调高配置。")
        outgoing = self.protocol.build(request, self.settings)
        payload = dumps(outgoing.payload).encode("utf-8")
        opener = build_opener(NoRedirect())
        for attempt in range(self.settings.llm_retries + 1):
            req = Request(outgoing.url, data=payload,
                          headers={**outgoing.headers, "Content-Type": "application/json"},
                          method="POST")
            try:
                with opener.open(req, timeout=self.settings.llm_timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ERPError("llm_output_limit", "模型响应超过大小限制。", 502)
                try:
                    root = obj(loads(raw.decode("utf-8")))
                    result = self.protocol.parse(root, self.settings, request)
                except (ERPError, UnicodeError) as exc:
                    if isinstance(exc, ERPError) and exc.code.startswith("llm_"):
                        raise
                    raise ERPError("llm_invalid_response", "模型接口响应结构无效。", 502) from exc
                if not result.content.strip():
                    raise ERPError("llm_empty_response", "模型没有返回可用内容。", 502)
                return result
            except HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.settings.llm_retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise ERPError("llm_http_error", f"模型接口返回 HTTP {exc.code}。", 502) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < self.settings.llm_retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise ERPError("llm_connection", "无法连接模型接口或请求超时。", 502) from exc
        raise ERPError("llm_failed", "模型请求失败。", 502)
