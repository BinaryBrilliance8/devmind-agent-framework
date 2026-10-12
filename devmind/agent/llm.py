"""Streaming clients for local Ollama and hosted OpenAI-compatible APIs."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


PROVIDERS = {
    "ollama": ("http://127.0.0.1:11434/v1/chat/completions", "", "qwen2.5-coder:14b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY", "gemini-2.5-flash"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "gpt-4o-mini"),
}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMEvent:
    type: str
    data: dict


class LLMClient:
    def __init__(self, provider: str = "openai", model: str | None = None, *, api_key: str | None = None):
        if provider not in PROVIDERS:
            raise LLMError(f"Unsupported provider {provider!r}; choose {', '.join(PROVIDERS)}")
        endpoint, key_env, default_model = PROVIDERS[provider]
        self.provider = provider
        self.endpoint = os.getenv("OLLAMA_BASE_URL", endpoint) if provider == "ollama" else endpoint
        # Ollama's OpenAI-compatible local endpoint does not require credentials.
        self.api_key = api_key or (os.getenv(key_env) if key_env else "ollama")
        self.model = model or os.getenv("DEVMIND_MODEL") or default_model
        if key_env and not self.api_key:
            raise LLMError(f"Provider {provider} requires {key_env}")

    async def stream(self, messages: list[dict], *, temperature: float = 0.1) -> AsyncIterator[LLMEvent]:
        payload = {"model": self.model, "messages": messages, "temperature": temperature,
                   "stream": True, "stream_options": {"include_usage": True}}
        started = time.perf_counter()
        text = ""
        usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as client:
                async with client.stream("POST", self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")[:3000]
                        raise LLMError(f"{self.provider} returned HTTP {response.status_code}: {body}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if frame.get("usage"):
                            u = frame["usage"]
                            usage = {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"), "total_tokens": u.get("total_tokens")}
                        for choice in frame.get("choices", []):
                            delta = choice.get("delta", {}).get("content")
                            if delta:
                                text += delta
                                yield LLMEvent("token", {"text": delta})
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.provider} request failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000
        yield LLMEvent("complete", {"provider": self.provider, "model": self.model, "text": text,
                                    "usage": usage, "latency_ms": round(latency_ms, 2)})
