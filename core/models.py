"""Roteamento de modelos por papel (multi-modelo).

PREPARADO PARA O FUTURO — fundação leve, sem complexidade agora.

Hoje: tudo resolve para ``settings.model`` (qwen2.5:7b), a menos que o
usuário declare papéis em ``.pudimai/config.json``:

    "model_roles": {"coding": "qwen2.5-coder:7b",
                    "reasoning": "qwen2.5:14b"}

Amanhã: o Agent/Planner/Security poderão pedir um papel específico e o
roteador decide qual provider+modelo atender — sem mudar chamadas.
"""
from __future__ import annotations

from config.settings import Settings

ROLES = ("default", "coding", "reasoning", "fast", "security")


class ModelRouter:
    """Resolve papel -> nome do modelo no provedor ativo."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, role: str = "default") -> str:
        roles = getattr(self._settings, "model_roles", None) or {}
        return roles.get(role) or self._settings.model

    def table(self) -> dict[str, str]:
        return {role: self.resolve(role) for role in ROLES}
