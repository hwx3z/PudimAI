"""Fakes compartilhados entre suítes de teste (sem rede, sem Ollama)."""
from __future__ import annotations

from core.llm import LLMResponse


class FakeLLM:
    """Provedor determinístico que segue um roteiro de respostas."""

    name = "fake"

    def __init__(self, script: list[LLMResponse] | None = None) -> None:
        self.script = list(script or [])
        self.calls: int = 0

    def health_check(self) -> list[str]:
        return [self.model_name()]  # pragma: no cover - simples

    def ensure_model(self, installed=None) -> None:
        return None

    def list_models(self) -> list[str]:
        return ["qwen2.5:7b"]

    def model_name(self) -> str:
        return "qwen2.5:7b"

    def chat(self, messages, tools=None, temperature=0.2,
             on_chunk=None, cancel_check=None):  # noqa: ANN001
        self.calls += 1
        if not self.script:
            return LLMResponse(content="roteiro esgotado")
        response = self.script.pop(0)
        if on_chunk and response.content:
            on_chunk(response.content)
        return response
