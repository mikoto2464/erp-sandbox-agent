from urllib.parse import urlsplit

from erp_agent.application.contracts import JSONObject
from erp_agent.config import LLM_BASE_URLS, Settings

PROVIDER_NAMES = {
    "openai_chat": "OpenAI / 兼容 Chat Completions",
    "openai_responses": "OpenAI Responses",
    "anthropic": "Anthropic / 兼容 Messages",
    "gemini": "Google Gemini",
    "ollama": "Ollama",
}


def protocol_catalog() -> list[JSONObject]:
    return [{"id": key, "name": name, "base_url": LLM_BASE_URLS[key],
             "api_key_required": key != "ollama"}
            for key, name in PROVIDER_NAMES.items()]


def requires_api_key(settings: Settings) -> bool:
    return settings.llm_provider != "ollama" or urlsplit(settings.llm_base_url).hostname not in {
        "localhost", "127.0.0.1", "::1",
    }
