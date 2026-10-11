import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from erp_agent.adapters.documents import OOXMLDocumentParser
from erp_agent.adapters.llm import CompatibleLLMClient
from erp_agent.adapters.llm_protocols import protocol_catalog
from erp_agent.adapters.model_settings import ModelSettingsService
from erp_agent.application.contracts import dumps, loads
from erp_agent.application.ports import LLMRequest
from erp_agent.application.reader import StructuredReader
from erp_agent.config import LLM_BASE_URLS, Settings
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import KnowledgeState, SourceDocument
from tests.helpers import Control, docx_bytes


class ProtocolTests(unittest.TestCase):
    def invoke(self, provider, response, **options):
        settings = Settings(llm_provider=provider, llm_model="test-model", llm_api_key="secret",
                            **options)
        seen = []

        class Opener:
            def open(self, request, timeout):
                seen.append(request)
                return io.BytesIO(dumps(response).encode())

        with patch("erp_agent.adapters.llm.build_opener", return_value=Opener()):
            result = CompatibleLLMClient(settings).generate(LLMRequest(
                "system", "user", "structured_result", '{"type":"object"}',
            ))
        return result, seen[0], loads(seen[0].data.decode())

    def test_chat_legacy_tokens_plain_json_and_full_endpoint(self):
        result, request, payload = self.invoke("openai_chat", {"choices": [
            {"finish_reason": "stop", "message": {"content": [{"type": "text", "text": "{}"}]}}
        ]}, llm_base_url="https://example.com/v1/chat/completions",
            llm_token_parameter="max_tokens", llm_format="text")
        self.assertEqual(result.content, "{}")
        self.assertEqual(request.full_url, "https://example.com/v1/chat/completions")
        self.assertIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("response_format", payload)

    def test_responses_extracts_text_and_ignores_reasoning(self):
        result, request, payload = self.invoke("openai_responses", {
            "status": "completed", "model": "actual-model", "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": "{}"}]},
            ],
        })
        self.assertEqual(result.content, "{}")
        self.assertEqual(result.model, "actual-model")
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(payload["text"]["format"]["name"], "structured_result")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["store"])
        self.assertNotIn("messages", payload)

    def test_anthropic_uses_native_auth_and_structured_tool(self):
        result, request, payload = self.invoke("anthropic", {
            "stop_reason": "tool_use", "content": [
                {"type": "text", "text": "intro"},
                {"type": "tool_use", "name": "structured_result", "input": {"value": 3}},
            ],
        })
        self.assertEqual(loads(result.content), {"value": 3})
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "secret")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(payload["tool_choice"]["name"], "structured_result")
        self.assertEqual(payload["tools"][0]["input_schema"], {"type": "object"})

    def test_gemini_uses_header_key_and_excludes_thought_text(self):
        result, request, payload = self.invoke("gemini", {
            "modelVersion": "returned-model", "candidates": [
                {"finishReason": "STOP", "content": {"parts": [
                    {"text": "internal", "thought": True}, {"text": "{}"},
                ]}},
            ],
        })
        self.assertEqual(result.content, "{}")
        self.assertEqual(result.model, "returned-model")
        self.assertEqual(request.full_url,
                         "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent")
        self.assertEqual(request.get_header("X-goog-api-key"), "secret")
        self.assertNotIn("secret", request.full_url)
        self.assertEqual(payload["generationConfig"]["responseJsonSchema"], {"type": "object"})

    def test_ollama_schema_and_optional_local_auth(self):
        result, request, payload = self.invoke("ollama", {
            "done": True, "done_reason": "stop", "message": {"content": "{}"},
        })
        self.assertEqual(result.content, "{}")
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["format"], {"type": "object"})
        self.assertEqual(payload["options"]["num_predict"], 12000)
        self.assertTrue(CompatibleLLMClient(Settings(llm_provider="ollama", llm_model="local")).configured)
        self.assertFalse(CompatibleLLMClient(Settings(llm_provider="ollama", llm_model="remote",
                                                     llm_base_url="https://example.com")).configured)

    def test_refusals_truncation_and_wrong_tool_are_rejected(self):
        cases = [
            ("openai_responses", {"status": "incomplete"}, "llm_incomplete"),
            ("openai_responses", {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}, "llm_refusal"),
            ("anthropic", {"stop_reason": "max_tokens"}, "llm_incomplete"),
            ("anthropic", {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "name": "unexpected_tool", "input": {}}]}, "llm_structured_response"),
            ("gemini", {"promptFeedback": {"blockReason": "SAFETY"}}, "llm_refusal"),
            ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS"}]}, "llm_incomplete"),
            ("ollama", {"done": False}, "llm_incomplete"),
            ("openai_chat", {"choices": "invalid"}, "llm_invalid_response"),
        ]
        for provider, response, code in cases:
            with self.subTest(provider=provider, code=code):
                with self.assertRaises(ERPError) as error:
                    self.invoke(provider, response)
                self.assertEqual(error.exception.code, code)

    def test_all_protocols_use_same_reader_contract(self):
        document = OOXMLDocumentParser().parse(
            SourceDocument("d", "topic.docx", "hash", 0, "docx"), docx_bytes(),
        )
        content = {"title": "测试", "facts": [{"id": "cash", "category": "initial",
            "entity_id": "company", "entity_name": "公司", "key": "initial_cash",
            "value": "100000", "value_type": "number", "unit": "元", "state": "known",
            "evidence_ids": [document.evidence[0].id], "conversion": "identity", "explanation": ""}],
            "rules": [], "issues": []}
        responses = {
            "openai_chat": {"choices": [{"finish_reason": "stop", "message": {"content": dumps(content)}}]},
            "openai_responses": {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": dumps(content)}]}]},
            "anthropic": {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "name": "erp_reading", "input": content}]},
            "gemini": {"candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": dumps(content)}]}}]},
            "ollama": {"done": True, "done_reason": "stop", "message": {"content": dumps(content)}},
        }
        for provider, response in responses.items():
            with self.subTest(provider=provider):
                class Opener:
                    def open(self, request, timeout):
                        return io.BytesIO(dumps(response).encode())
                client = CompatibleLLMClient(Settings(llm_provider=provider, llm_model="model",
                                                       llm_api_key="secret"))
                with patch("erp_agent.adapters.llm.build_opener", return_value=Opener()):
                    result = StructuredReader(client).read((document,), Control())
                self.assertEqual(result.problem.facts[0].state, KnowledgeState.KNOWN)
                self.assertEqual(result.problem.facts[0].value, "100000")

    def test_configuration_switching_persistence_and_old_file(self):
        with tempfile.TemporaryDirectory(prefix="erp-provider-") as directory:
            settings = Settings(data_dir=Path(directory), llm_api_key="old-secret", llm_model="old")
            service = ModelSettingsService(settings)
            catalog = service.public()["providers"]
            self.assertEqual(len(catalog), 5)
            self.assertNotIn("old-secret", dumps(service.public()))
            data = service.update({"provider": "anthropic", "model": "claude", "api_key": "new-secret"})
            self.assertEqual(data["base_url"], LLM_BASE_URLS["anthropic"])
            self.assertEqual(ModelSettingsService(settings).settings.llm_provider, "anthropic")
            service.update({"base_url": "https://different.example/v1"})
            self.assertFalse(service.public()["api_key_set"])
            service.update({"provider": "ollama", "model": "local"})
            self.assertTrue(service.public()["configured"])
            with self.assertRaises(ERPError):
                service.update({"provider": "unknown"})
            service.path.write_text(dumps({"llm_model": "legacy", "llm_api_key": "secret"}))
            self.assertEqual(ModelSettingsService(settings).settings.llm_provider, "openai_chat")
            self.assertEqual(len(protocol_catalog()), 5)

    def test_json_and_text_formats_omit_unsupported_schema_parameters(self):
        cases = [
            ("openai_responses", {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}),
            ("anthropic", {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]}),
            ("gemini", {"candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"text": "{}"}]}}]}),
            ("ollama", {"done": True, "done_reason": "stop", "message": {"content": "{}"}}),
        ]
        for provider, response in cases:
            for output_format in ["json_object", "text"]:
                with self.subTest(provider=provider, output_format=output_format):
                    result, _, payload = self.invoke(provider, response, llm_format=output_format)
                    self.assertEqual(result.content, "{}")
                    self.assertNotIn("tools", payload)
                    self.assertNotIn("responseJsonSchema", payload.get("generationConfig", {}))
                    if output_format == "text":
                        self.assertNotIn("text", payload)
                        self.assertNotIn("format", payload)

    def test_empty_native_response_is_rejected(self):
        with self.assertRaises(ERPError) as error:
            self.invoke("openai_responses", {"status": "completed", "output": []})
        self.assertEqual(error.exception.code, "llm_empty_response")

    def test_env_chooses_provider_default_url(self):
        with patch.dict("os.environ", {"ERP_LLM_PROVIDER": "gemini"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.llm_base_url, LLM_BASE_URLS["gemini"])
        with self.assertRaises(ValueError):
            replace(settings, llm_provider="invalid")
