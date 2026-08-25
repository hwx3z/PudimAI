"""Abstração de LLM + provedor Ollama.

A interface ``LLMProvider`` isola o Agent Core do provedor concreto.
Hoje implementamos Ollama (local, gratuito); amanhã, OpenAI-compatible,
Anthropic-compatible ou provedores customizados — sem tocar no agente.

IMPLEMENTADO AGORA : chat em streaming, tool calling, health check,
                     verificação de modelo, timeouts, recuperação de erro.
PREPARADO PARA O FUTURO: registro de provedores adicionais via
                     ``PROVIDERS`` / ``create_provider``.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

from config.settings import Settings
from core.exceptions import (
    LLMError,
    ModelNotFoundError,
    OllamaUnavailableError,
    TaskCancelled,
)

logger = logging.getLogger("pudimai.llm")

ChunkCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]

REQUEST_TIMEOUT = 10          # segundos para conexão inicial


# ---------------------------------------------------------------------- #
# Estruturas de dados
# ---------------------------------------------------------------------- #
@dataclass
class ToolCall:
    """Uma chamada de ferramenta solicitada pelo modelo."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Resposta normalizada de uma rodada de chat."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMProvider(Protocol):
    """Contrato que todo provedor deve cumprir."""

    def chat(self,
             messages: list[dict],
             tools: list[dict] | None = None,
             temperature: float = 0.2,
             on_chunk: ChunkCallback | None = None,
             cancel_check: CancelCheck | None = None) -> LLMResponse:
        ...  # pragma: no cover - apenas assinatura


# ---------------------------------------------------------------------- #
# Provedor Ollama
# ---------------------------------------------------------------------- #
class OllamaProvider:
    """Provedor local via API HTTP do Ollama (http://localhost:11434)."""

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._url = settings.ollama_url.rstrip("/")
        self._model = settings.model
        self._timeout = settings.llm_timeout
        self._stream_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Saúde do servidor
    # ------------------------------------------------------------------ #
    def health_check(self) -> list[str]:
        """Verifica o servidor e retorna os modelos instalados."""
        try:
            response = requests.get(f"{self._url}/api/tags", timeout=4)
        except requests.ConnectionError as exc:
            raise OllamaUnavailableError(
                "Ollama não está em execução.\n"
                f"  URL testada: {self._url}\n"
                "  Inicie com:  ollama serve"
            ) from exc
        except requests.Timeout as exc:
            raise OllamaUnavailableError(
                f"Ollama não respondeu em {self._url} (timeout)."
            ) from exc
        if response.status_code != 200:
            raise OllamaUnavailableError(
                f"Ollama retornou HTTP {response.status_code} em /api/tags."
            )
        try:
            models = response.json().get("models", [])
        except json.JSONDecodeError as exc:
            raise LLMError("Resposta inválida do Ollama (/api/tags).") from exc
        return [model.get("name", "") for model in models]

    def ensure_model(self, installed: list[str] | None = None) -> None:
        """Garante que o modelo configurado está disponível."""
        installed = installed if installed is not None else self.health_check()
        if self._model in installed:
            return
        base = self._model.split(":")[0]
        if any(name == base or name.startswith(base + ":") for name in installed):
            logger.warning(
                "Modelo exato '%s' ausente, mas variante de '%s' existe.",
                self._model, base)
            return
        raise ModelNotFoundError(
            f"Modelo '{self._model}' não encontrado no Ollama.\n"
            f"  Instale com:  ollama pull {self._model}"
        )

    def list_models(self) -> list[str]:
        return self.health_check()

    # ------------------------------------------------------------------ #
    # Chat (sempre em streaming; tool calls são montadas ao final)
    # ------------------------------------------------------------------ #
    def chat(self,
             messages: list[dict],
             tools: list[dict] | None = None,
             temperature: float = 0.2,
             on_chunk: ChunkCallback | None = None,
             cancel_check: CancelCheck | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools

        with self._stream_lock:  # uma geração por vez por provedor
            return self._stream_request(payload, on_chunk, cancel_check)

    # ------------------------------------------------------------------ #
    def _open_stream(self, payload: dict) -> requests.Response:
        try:
            response = requests.post(
                f"{self._url}/api/chat",
                json=payload,
                stream=True,
                timeout=(REQUEST_TIMEOUT, self._timeout),
            )
        except requests.ConnectionError as exc:
            raise OllamaUnavailableError(
                "Conexão perdida com o Ollama durante a geração.\n"
                f"  Verifique se `ollama serve` está ativo em {self._url}."
            ) from exc
        except requests.Timeout as exc:
            raise LLMError(
                f"Ollama excedeu {self._timeout}s sem responder."
            ) from exc

        if response.status_code == 404:
            detail = ""
            try:
                detail = response.json().get("error", "")
            except Exception:  # noqa: BLE001
                pass
            raise ModelNotFoundError(
                f"Modelo '{self._model}' indisponível no Ollama. "
                f"{detail}".strip() +
                f"\n  Instale com:  ollama pull {self._model}"
            )
        if response.status_code != 200:
            snippet = response.text[:300]
            raise LLMError(f"HTTP {response.status_code} do Ollama: {snippet}")
        return response

    def _stream_request(self,
                        payload: dict,
                        on_chunk: ChunkCallback | None,
                        cancel_check: CancelCheck | None) -> LLMResponse:
        response = self._open_stream(payload)
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        meta: dict[str, Any] = {}

        try:
            for line in response.iter_lines(decode_unicode=True):
                if cancel_check and cancel_check():
                    raise TaskCancelled("Geração interrompida pelo usuário.")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Linha inválida ignorada no stream: %.80s",
                                 line)
                    continue
                if obj.get("error"):
                    raise LLMError(f"Erro do Ollama: {obj['error']}")

                message = obj.get("message") or {}
                delta = message.get("content") or ""
                if delta:
                    content_parts.append(delta)
                    if on_chunk:
                        on_chunk(delta)

                for raw_call in message.get("tool_calls") or []:
                    call = self._normalize_tool_call(raw_call)
                    if call and all(c.name != call.name or
                                    c.arguments != call.arguments
                                    for c in tool_calls):
                        tool_calls.append(call)

                if obj.get("done"):
                    meta = {
                        key: obj[key]
                        for key in ("total_duration", "eval_count",
                                    "prompt_eval_count")
                        if key in obj
                    }
        finally:
            response.close()

        return LLMResponse(content="".join(content_parts),
                           tool_calls=tool_calls, meta=meta)

    @staticmethod
    def _normalize_tool_call(raw: dict) -> ToolCall | None:
        function = raw.get("function") or {}
        name = function.get("name")
        if not name:
            return None
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                logger.warning("Argumentos inválidos para tool '%s': %.120s",
                               name, arguments)
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return ToolCall(name=name, arguments=arguments)


# ---------------------------------------------------------------------- #
# Fábrica de provedores (extensível sem alterar o Agent Core)
# ---------------------------------------------------------------------- #
PROVIDERS: dict[str, type[OllamaProvider]] = {
    "ollama": OllamaProvider,
}


def create_provider(settings: Settings) -> OllamaProvider:
    provider_cls = PROVIDERS.get("ollama")
    if provider_cls is None:
        raise LLMError("Nenhum provedor LLM registrado.")
    return provider_cls(settings)
