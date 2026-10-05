"""Thin synchronous client for a local Ollama server's OpenAI-compatible API.

We talk plain HTTP (httpx) to ``{base_url}/chat/completions`` so there's no hard
dependency on the ``openai`` package. Any OpenAI-compatible local server works
(Ollama, llama.cpp ``llama-server``, LM Studio) by pointing ``base_url`` at it.
"""

from __future__ import annotations

import httpx


class AiUnavailable(Exception):
    """Raised when the local model server can't be reached or returns an error."""


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/models", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
    ) -> str:
        """``reasoning_effort="none"`` switches off a thinking model's hidden reasoning
        (e.g. gemma4: ~1s instead of ~100s and ~1000 reasoning tokens for a one-number
        answer). Only sent when given; servers that don't know it ignore it."""
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions", json=body, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise AiUnavailable(f"cannot reach model server: {exc}") from exc
        if resp.status_code != 200:
            raise AiUnavailable(f"model server HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise AiUnavailable(f"unexpected model response: {exc}") from exc
