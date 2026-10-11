import json
from contextlib import contextmanager, ExitStack
from importlib import import_module
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_core.language_models import BaseChatModel

from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.adapters.llm import LangChainLLMClient, create_chat_model
from erp_agent.adapters.llm_diagnostics import validate_completion
from erp_agent.adapters.llm_errors import model_error
from erp_agent.adapters.model_catalog import protocol_catalog
from erp_agent.adapters.model_settings import ModelSettingsService
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMRequest
from erp_agent.application.reader import StructuredReader
from erp_agent.config import LLM_BASE_URLS, Settings
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import KnowledgeState, SourceDocument
from tests.helpers import Control, docx_bytes

SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}},
          "required": ["value"], "additionalProperties": False}
REQUEST = LLMRequest("Return JSON.", "Input material.", "structured_result", dumps(SCHEMA))


def provider_response(provider, content, structured=True):
    text = dumps(content)
    if provider == "openai_chat":
        return {"id": "chat-test", "object": "chat.completion", "created": 1,
                "model": "returned-model", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}
    if provider == "openai_responses":
        return {"id": "resp-test", "object": "response", "created_at": 1,
                "status": "completed", "model": "returned-model", "output": [
                {"id": "reasoning-test", "type": "reasoning", "summary": []},
                {"id": "message-test", "type": "message", "role": "assistant",
                 "status": "completed", "content": [
                 {"type": "output_text", "text": text, "annotations": []}]}],
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}
    if provider == "anthropic":
        blocks = [{"type": "tool_use", "id": "tool-test", "name": "structured_result",
                   "input": content}] if structured else [
                   {"type": "thinking", "thinking": "internal", "signature": "test"},
                   {"type": "text", "text": text}]
        return {"id": "msg-test", "type": "message", "role": "assistant",
                "model": "returned-model", "content": blocks,
                "stop_reason": "tool_use" if structured else "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 4}}
    if provider == "gemini":
        return {"modelVersion": "returned-model", "candidates": [{"finishReason": "STOP",
                "content": {"role": "model", "parts": [
                {"thought": True, "text": "internal"}, {"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4,
                                  "totalTokenCount": 14}}
    return {"model": "returned-model", "created_at": "2026-10-11T00:00:00Z", "done": True,
            "done_reason": "stop", "message": {"role": "assistant", "content": text,
                                                 "thinking": "internal"},
            "prompt_eval_count": 10, "eval_count": 4}


@contextmanager
def mock_transport(response, seen, status=200):
    modules = [httpx]
    try:
        modules.append(import_module("httpx2"))
    except ModuleNotFoundError:
        pass
    with ExitStack() as stack:
        for module in modules:
            def send(client, request, _module=module, **kwargs):
                req = request
                seen.append(req)
                result = _module.Response(status, json=response, request=req)
                result.read()
                return result
            stack.enter_context(patch.object(module.Client, "send", send))
        yield


class LangChainTests(unittest.TestCase):
    def invoke(self, provider, response=None, status=200, request=REQUEST, **options):
        settings = Settings(llm_provider=provider, llm_model="test-model", llm_api_key="secret",
                            llm_retries=0, **options)
        response = response if response is not None else provider_response(
            provider, {"value": 3}, settings.llm_format == "json_schema")
        seen = []

        with mock_transport(response, seen, status):
            result = LangChainLLMClient(settings).generate(request)
        return result, seen

    def test_real_langchain_classes_and_all_provider_structured_outputs(self):
        for provider in LLM_BASE_URLS:
            with self.subTest(provider=provider):
                model = create_chat_model(Settings(llm_provider=provider, llm_model="test-model",
                                                   llm_api_key="fake"))
                self.assertIsInstance(model, BaseChatModel)
                result, seen = self.invoke(provider)
                self.assertEqual(loads(result.content), {"value": 3})
                self.assertEqual(result.model, "returned-model")
                self.assertTrue(seen)
                self.assertNotIn("secret", str(seen[0].url))

    def test_chat_schema_tokens_and_auth(self):
        _, seen = self.invoke("openai_chat")
        request = seen[0]
        payload = json.loads(request.content)
        self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer secret")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "structured_result")
        self.assertEqual(payload["max_completion_tokens"], 12000)

    def test_chat_legacy_token_parameter_and_deepseek_thinking(self):
        _, seen = self.invoke("openai_chat", llm_format="text", llm_token_parameter="max_tokens",
                              llm_thinking_mode="disabled",
                              llm_base_url="https://api.deepseek.com/chat/completions")
        payload = json.loads(seen[0].content)
        self.assertEqual(str(seen[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(payload["max_tokens"], 12000)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("response_format", payload)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_responses_uses_native_schema_and_never_stores(self):
        _, seen = self.invoke("openai_responses")
        payload = json.loads(seen[0].content)
        self.assertEqual(str(seen[0].url), "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["name"], "structured_result")
        self.assertEqual(payload["max_output_tokens"], 12000)
        self.assertNotIn("messages", payload)

    def test_anthropic_auth_tool_and_gateway_prefix(self):
        for base in ("https://api.deepseek.com/anthropic",
                     "https://api.deepseek.com/anthropic/v1",
                     "https://api.deepseek.com/anthropic/v1/messages"):
            with self.subTest(base=base):
                _, seen = self.invoke("anthropic", llm_base_url=base)
                request = seen[0]
                payload = json.loads(request.content)
                self.assertEqual(str(request.url), "https://api.deepseek.com/anthropic/v1/messages")
                self.assertEqual(request.headers["x-api-key"], "secret")
                self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
                self.assertNotIn("authorization", request.headers)
                self.assertEqual(payload["tool_choice"]["name"], "structured_result")

    def test_anthropic_thinking_budget_and_large_limits(self):
        _, seen = self.invoke("anthropic", llm_format="json_object", llm_thinking_mode="enabled",
                              llm_thinking_budget=8192, llm_max_output_tokens=393216)
        payload = json.loads(seen[0].content)
        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 8192})
        self.assertEqual(payload["max_tokens"], 393216)
        self.assertNotIn("tool_choice", payload)
        for options, code in [
            ({"llm_thinking_mode": "enabled", "llm_format": "json_schema"}, "llm_thinking_tools"),
            ({"llm_thinking_mode": "enabled", "llm_format": "text",
              "llm_max_output_tokens": 4096}, "llm_thinking_budget"),
        ]:
            with self.subTest(options=options), self.assertRaises(ERPError) as caught:
                self.invoke("anthropic", **options)
            self.assertEqual(caught.exception.code, code)

    def test_gemini_native_schema_auth_and_timeout(self):
        _, seen = self.invoke("gemini", llm_base_url="https://proxy.example/gemini/v1beta")
        req = seen[0]
        payload = json.loads(req.content)
        self.assertEqual(str(req.url), "https://proxy.example/gemini/v1beta/models/test-model:generateContent")
        self.assertEqual(req.headers["x-goog-api-key"], "secret")
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(req.extensions["timeout"]["read"], 90)

    def test_ollama_schema_auth_and_output_budget(self):
        _, seen = self.invoke("ollama")
        payload = json.loads(seen[0].content)
        self.assertEqual(str(seen[0].url), "http://localhost:11434/api/chat")
        self.assertEqual(payload["format"]["properties"], SCHEMA["properties"])
        self.assertEqual(payload["options"]["num_predict"], 12000)
        self.assertEqual(seen[0].headers["authorization"], "Bearer secret")
        self.assertTrue(LangChainLLMClient(Settings(llm_provider="ollama", llm_model="local")).configured)
        self.assertFalse(LangChainLLMClient(Settings(llm_provider="ollama", llm_model="remote",
                                                    llm_base_url="https://example.com")).configured)

    def test_json_and_text_modes_use_application_contract(self):
        for provider in LLM_BASE_URLS:
            for output_format in ("json_object", "text"):
                with self.subTest(provider=provider, output_format=output_format):
                    result, seen = self.invoke(provider, llm_format=output_format)
                    self.assertEqual(loads(result.content), {"value": 3})
                    self.assertNotIn("internal", result.content)
                    payload = json.loads(seen[0].content)
                    if provider == "anthropic":
                        self.assertNotIn("tools", payload)
                    if provider == "openai_responses":
                        self.assertEqual(payload.get("text", {}).get("format", {}).get("type"),
                                         "json_object" if output_format == "json_object" else None)

    def test_all_providers_feed_strict_reader_and_evidence_validation(self):
        doc = OOXMLDocumentParser().parse(SourceDocument("d", "topic.docx", "hash", 0, "docx"),
                                          docx_bytes())
        content = {"title": "测试", "facts": [{"id": "cash", "category": "initial",
                   "entity_id": "company", "entity_name": "公司", "key": "initial_cash",
                   "value": "100000", "value_type": "number", "unit": "元", "state": "known",
                   "evidence_ids": [doc.evidence[0].id], "conversion": "identity",
                   "explanation": ""}], "rules": [], "issues": []}
        for provider in LLM_BASE_URLS:
            with self.subTest(provider=provider):
                response = provider_response(provider, content)
                if provider == "anthropic":
                    response["content"][0]["name"] = "erp_reading"
                settings = Settings(llm_provider=provider, llm_model="test-model", llm_api_key="fake")
                with mock_transport(response, []):
                    result = StructuredReader(LangChainLLMClient(settings)).read((doc,), Control())
                self.assertEqual(result.problem.facts[0].state, KnowledgeState.KNOWN)
                self.assertEqual(result.problem.facts[0].value, "100000")

    def test_truncated_chat_preserves_reason_and_reasoning_usage(self):
        response = provider_response("openai_chat", {"value": 3})
        response["choices"][0]["finish_reason"] = "length"
        response["usage"].update(completion_tokens=12000, total_tokens=12010,
                                 completion_tokens_details={"reasoning_tokens": 11900})
        with self.assertRaises(ERPError) as caught:
            self.invoke("openai_chat", response)
        self.assertEqual(caught.exception.code, "llm_incomplete")
        self.assertIn("length", caught.exception.message)
        self.assertIn("11900", caught.exception.message)

    def test_truncation_refusals_and_incomplete_responses_rejected(self):
        for provider in LLM_BASE_URLS:
            response = provider_response(provider, {"value": 3})
            if provider == "openai_chat":
                response["choices"][0]["finish_reason"] = "length"
            elif provider == "openai_responses":
                response["status"] = "incomplete"
                response["incomplete_details"] = {"reason": "max_output_tokens"}
            elif provider == "anthropic":
                response["stop_reason"] = "max_tokens"
            elif provider == "gemini":
                response["candidates"][0]["finishReason"] = "MAX_TOKENS"
            else:
                response["done_reason"] = "length"
            with self.subTest(provider=provider), self.assertRaises(ERPError) as caught:
                self.invoke(provider, response)
            self.assertEqual(caught.exception.code, "llm_incomplete")
        response = provider_response("openai_chat", {"value": 3})
        response["choices"][0]["message"] = {"role": "assistant", "refusal": "no", "content": None}
        with self.assertRaises(ERPError) as caught:
            self.invoke("openai_chat", response)
        self.assertEqual(caught.exception.code, "llm_refusal")

    def test_unknown_stop_metadata_and_incomplete_without_reason(self):
        for metadata in ({"status": "incomplete", "incomplete_details": {}},
                         {"stop_reason": ["invalid"]}, {"done": False}):
            with self.subTest(metadata=metadata), self.assertRaises(ERPError) as caught:
                validate_completion(metadata, {}, Settings())
            self.assertEqual(caught.exception.code, "llm_incomplete")

    def test_invalid_schema_result_and_extra_anthropic_tools_rejected(self):
        for provider in ("openai_chat", "ollama", "gemini", "openai_responses"):
            for content in ('invalid JSON', '{"value":3', '{"value":2,"value":3}'):
                response = provider_response(provider, {"value": 3})
                if provider == "openai_chat":
                    response["choices"][0]["message"]["content"] = content
                elif provider == "openai_responses":
                    response["output"][1]["content"][0]["text"] = content
                elif provider == "gemini":
                    response["candidates"][0]["content"]["parts"] = [{"text": content}]
                else:
                    response["message"]["content"] = content
                with self.subTest(provider=provider, content=content), \
                        self.assertRaises(ERPError) as caught:
                    self.invoke(provider, response)
                self.assertEqual(caught.exception.code, "llm_structured_response")
        for extra in (False, True):
            response = provider_response("anthropic", {"value": 3})
            if extra:
                response["content"].append({"type": "tool_use", "id": "second-tool",
                                            "name": "structured_result", "input": {"value": 4}})
            else:
                response["content"][0]["name"] = "wrong_tool"
            with self.subTest(extra=extra), self.assertRaises(ERPError) as caught:
                self.invoke("anthropic", response)
            self.assertEqual(caught.exception.code, "llm_structured_response")

    def test_responses_refusal_and_missing_incomplete_reason(self):
        response = provider_response("openai_responses", {"value": 3})
        response["output"][1]["content"] = [{"type": "refusal", "refusal": "no"}]
        with self.assertRaises(ERPError) as caught:
            self.invoke("openai_responses", response)
        self.assertEqual(caught.exception.code, "llm_refusal")
        response = provider_response("openai_responses", {"value": 3})
        response.update(status="incomplete", incomplete_details={})
        with self.assertRaises(ERPError) as caught:
            self.invoke("openai_responses", response)
        self.assertEqual(caught.exception.code, "llm_incomplete")

    def test_http_errors_safe_diagnostics_no_html_or_secrets(self):
        for status, response, expected in [
            (401, {"error": {"message": "secret"}}, "HTTP 401"),
            (400, {"error": {"message": "Unsupported tool_choice api_key=secret",
                             "param": "tool_choice"}}, "JSON"),
            (400, {"error": {"message": "bad Input material."}}, "已隐藏"),
            (400, "<html>private contents</html>", "HTTP 400"),
        ]:
            with self.subTest(status=status), self.assertRaises(ERPError) as caught:
                self.invoke("openai_chat", response, status=status)
            self.assertIn(expected, caught.exception.message)
            self.assertNotIn("secret", caught.exception.message)
            self.assertNotIn("private contents", caught.exception.message)
            self.assertNotIn("Input material.", caught.exception.message)

    def test_missing_configuration_input_limit_and_socket_errors(self):
        for settings, code in [
            (Settings(), "llm_not_configured"),
            (Settings(llm_api_key="key", llm_model="model", llm_max_input_chars=2), "llm_input_limit"),
        ]:
            with self.subTest(code=code), self.assertRaises(ERPError) as caught:
                LangChainLLMClient(settings).generate(REQUEST)
            self.assertEqual(caught.exception.code, code)
        from langchain_openai._compat import httpx as openai_httpx
        for error, code in [(openai_httpx.ConnectError("secret"), "llm_connection"),
                            (openai_httpx.ReadTimeout("secret"), "llm_timeout")]:
            with self.subTest(code=code), patch.object(openai_httpx.Client, "send", side_effect=error), \
                    self.assertRaises(ERPError) as caught:
                LangChainLLMClient(Settings(llm_api_key="fake", llm_model="model",
                                           llm_retries=0)).generate(REQUEST)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret", caught.exception.message)

    def test_missing_socks_proxy_dependency_has_actionable_safe_error(self):
        error = ImportError("Using SOCKS proxy, but the 'socksio' package is not installed. secret")
        settings = Settings(llm_model="model", llm_api_key="secret")
        result = model_error(error, settings, REQUEST)
        self.assertEqual(result.code, "llm_proxy_dependency")
        self.assertEqual(result.status, 503)
        self.assertIn("ALL_PROXY", result.message)
        self.assertNotIn("secret", result.message)

    def test_presets_survive_migration_and_validate_large_limits(self):
        with tempfile.TemporaryDirectory(prefix="erp-provider-") as directory:
            settings = Settings(data_dir=Path(directory), llm_api_key="old-secret", llm_model="old")
            service = ModelSettingsService(settings)
            self.assertEqual(len(protocol_catalog()), 5)
            self.assertNotIn("old-secret", dumps(service.public()))
            data = service.update({"provider": "anthropic", "model": "deepseek-flash",
                                   "api_key": "new-secret", "thinking_mode": "disabled",
                                   "max_output_tokens": 393216, "timeout": 1800,
                                   "max_input_chars": 8000000})
            self.assertEqual(data["base_url"], LLM_BASE_URLS["anthropic"])
            reloaded = ModelSettingsService(settings)
            self.assertEqual(reloaded.settings.llm_thinking_mode, "disabled")
            self.assertEqual(reloaded.settings.llm_max_output_tokens, 393216)
            self.assertIsInstance(reloaded.client.current(), LangChainLLMClient)
            before = service.path.read_bytes()
            for update in ({"max_output_tokens": 393217}, {"thinking_mode": "invalid"},
                           {"timeout": 1801}, {"provider": "unknown"}):
                with self.assertRaises(ERPError):
                    service.update(update)
                self.assertEqual(service.path.read_bytes(), before)
            service.update({"base_url": "https://different.example/v1"})
            self.assertFalse(service.public()["api_key_set"])
            service.update({"provider": "ollama", "model": "local", "thinking_mode": "default"})
            self.assertTrue(service.public()["configured"])
            service.path.write_text(dumps({"llm_model": "legacy", "llm_api_key": "secret"}))
            self.assertEqual(ModelSettingsService(settings).settings.llm_provider, "openai_chat")
