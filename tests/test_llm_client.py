import pytest

from devmind.agent.llm import LLMClient, LLMError


def test_ollama_uses_local_defaults_without_a_secret(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("DEVMIND_MODEL", raising=False)

    client = LLMClient("ollama")

    assert client.endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    assert client.model == "qwen2.5-coder:14b"
    assert client.api_key == "ollama"


def test_ollama_endpoint_and_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.internal:11434/v1/chat/completions")
    monkeypatch.setenv("DEVMIND_MODEL", "qwen2.5-coder:32b")

    client = LLMClient("ollama")

    assert client.endpoint == "http://ollama.internal:11434/v1/chat/completions"
    assert client.model == "qwen2.5-coder:32b"


@pytest.mark.parametrize(("provider", "env_name"), [
    ("openai", "OPENAI_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
    ("groq", "GROQ_API_KEY"),
])
def test_hosted_provider_requires_its_api_key(monkeypatch, provider, env_name):
    monkeypatch.delenv(env_name, raising=False)

    with pytest.raises(LLMError, match=env_name):
        LLMClient(provider)
