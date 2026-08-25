"""Exceções compartilhadas do PudimAI Core.

Todos os erros de domínio herdam de PudimAIError, permitindo que as
interfaces (CLI, futura Web) capturem qualquer falha do núcleo com um
único except e apresentem mensagens úteis ao usuário.
"""
from __future__ import annotations


class PudimAIError(Exception):
    """Erro base do PudimAI."""


class ConfigurationError(PudimAIError):
    """Configuração inválida ou ausente."""


class LLMError(PudimAIError):
    """Falha genérica na comunicação com o provedor LLM."""


class OllamaUnavailableError(LLMError):
    """O servidor Ollama não está acessível."""


class ModelNotFoundError(LLMError):
    """O modelo configurado não está instalado no Ollama."""


class ToolError(PudimAIError):
    """Falha na execução de uma ferramenta."""


class SecurityViolation(PudimAIError):
    """Ação bloqueada pela política de segurança."""


class TaskCancelled(PudimAIError):
    """Tarefa cancelada pelo usuário."""
