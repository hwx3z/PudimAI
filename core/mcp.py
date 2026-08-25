"""Abstração MCP (Model Context Protocol) do PudimAI.

PREPARADO PARA O FUTURO — intencionalmente mínimo.

Objetivo: quando o PudimAI passar a consumir servidores MCP externos
(ferramentas e contexto padronizados), nenhum componente precisará ser
reescrito: um ``MCPBridge`` traduz ``list_tools``/``call_tool`` do
protocolo para as estruturas nativas (ToolParameter/ToolResult) e o
``MCPManager`` injeta essas tools no ToolRegistry como proxies.

Nada disso é carregado por padrão hoje; não há dependências novas.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("pudimai.mcp")


@dataclass(frozen=True)
class MCPServerConfig:
    """Descriptor declarativo de um servidor MCP futuro."""

    name: str
    transport: str                 # "stdio" | "http" (futuro)
    command: str = ""              # ex.: ["npx", "-y", "@x/server"]
    url: str = ""                  # para transporte http
    enabled: bool = False
    env: dict[str, str] = field(default_factory=dict)


class MCPBridge(Protocol):
    """Contrato que um adaptador de transporte MCP deverá implementar."""

    def start(self) -> None: ...
    def list_tools(self) -> list[dict[str, Any]]: ...
    def call_tool(self, name: str,
                  arguments: dict[str, Any]) -> str: ...
    def stop(self) -> None: ...


class MCPManager:
    """Registro passivo de servidores MCP configurados.

    Hoje: valida configs, mantém o inventário e expõe introspecção
    (/api/status futuro). Amanhã: instancia bridges e publica tools proxy
    no ToolRegistry — sem alterar Agent Core nem interfaces.
    """

    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}

    def register_config(self, config: MCPServerConfig) -> None:
        if config.transport not in ("stdio", "http"):
            raise ValueError(
                f"transporte MCP desconhecido: {config.transport}")
        self._configs[config.name] = config
        logger.info("MCP config registrada: %s (%s, enabled=%s)",
                    config.name, config.transport, config.enabled)

    def configs(self) -> list[MCPServerConfig]:
        return list(self._configs.values())

    def enabled(self) -> list[MCPServerConfig]:
        return [config for config in self._configs.values()
                if config.enabled]

    # ------------------------------------------------------------------ #
    # Implementado somente como contrato; chamado apenas por bridges reais.
    # ------------------------------------------------------------------ #
    @staticmethod
    def bridge_tools_to_schemas(tools: list[dict[str, Any]],
                                server_name: str) -> list[dict[str, Any]]:
        """Converte tool descriptors MCP em schemas nativos (proxy).

        Mantido público e puro para poder evoluir/testar isoladamente.
        """
        schemas: list[dict[str, Any]] = []
        for tool in tools:
            name = f"mcp__{server_name}__{tool.get('name', 'unnamed')}"
            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"[mcp:{server_name}] "
                                   f"{tool.get('description', '')}",
                    "parameters": tool.get("inputSchema", {
                        "type": "object", "properties": {},
                        "required": []}),
                },
            }
            schemas.append(schema)
        return schemas
