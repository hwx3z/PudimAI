"""Montagem do registro padrão de ferramentas do PudimAI.

Adicionar uma ferramenta nova = criar módulo + chamar ``register`` aqui.
O Agent Core não precisa saber que ela existe.
"""
from __future__ import annotations

from tools.base import ToolRegistry
from tools import filesystem, git, security_tools, terminal


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    filesystem.register(registry)
    terminal.register(registry)
    git.register(registry)
    security_tools.register(registry)
    return registry
