import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from erp_agent.application.contracts import array, loads, obj, string
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

    @property
    def configured(self) -> bool:
        return bool(self.settings.llm_model and self.settings.llm_api_key)

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.configured:
            raise ERPError("llm_not_configured", "请在服务端配置模型名称和 API 密钥。", 503)
        if len(request.system) + len(request.user) > self.settings.llm_max_input_chars:
            raise ERPError("llm_input_limit", "材料超过模型输入限制，请拆分文件或调高配置。")
        response_format: dict[str, object] = {"type": self.settings.llm_format}
        if self.settings.llm_format == "json_schema":
            response_format["json_schema"] = {
                "name": request.schema_name, "strict": True, "schema": loads(request.schema_json),
            }
        payload = json.dumps({
            "model": self.settings.llm_model,
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": request.user}],
            "response_format": response_format,
            "max_completion_tokens": self.settings.llm_max_output_tokens,
        }, ensure_ascii=False).encode()
        opener = build_opener(NoRedirect())
        for attempt in range(self.settings.llm_retries + 1):
            req = Request(self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                          data=payload, headers={
                              "Authorization": f"Bearer {self.settings.llm_api_key}",
                              "Content-Type": "application/json",
                          }, method="POST")
            try:
                with opener.open(req, timeout=self.settings.llm_timeout) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ERPError("llm_output_limit", "模型响应超过大小限制。", 502)
                root = obj(loads(raw.decode("utf-8")))
                choices = array(root.get("choices"))
                if not choices:
                    raise ERPError("llm_empty_response", "模型没有返回候选结果。", 502)
                choice = obj(choices[0])
                message = obj(choice.get("message"))
                if message.get("refusal"):
                    raise ERPError("llm_refusal", "模型拒绝生成结果。", 502)
                if choice.get("finish_reason") != "stop":
                    raise ERPError("llm_incomplete", "模型输出未正常完成，请调整输出上限。", 502)
                return LLMResponse(string(message.get("content"), maximum=4 * 1024 * 1024),
                                   string(root.get("model", self.settings.llm_model)))
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
