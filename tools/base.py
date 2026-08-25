"""Framework de Tools do PudimAI.

Uma Tool é uma função tipada, com schema JSON, registrada em um
``ToolRegistry``. O Agent Core só conhece schemas + executor; adicionar
uma ferramenta nova não exige mudança no agente.

Fluxo: LLM -> tool_call -> ToolExecutor -> ToolResult -> LLM
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config.settings import Settings
from core.events import EventBus, EventType

logger = logging.getLogger("pudimai.tools")

Handler = Callable[..., "ToolResult"]
ConfirmCallback = Callable[[str, str], bool]  # (comando, motivo) -> bool


@dataclass
class ToolResult:
    """Resultado estruturado de uma ferramenta."""

    success: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, output: str, **data: Any) -> "ToolResult":
        return cls(success=True, output=output, data=data)

    @classmethod
    def fail(cls, output: str, **data: Any) -> "ToolResult":
        return cls(success=False, output=output, data=data)


@dataclass(frozen=True)
class ToolParameter:
    """Parâmetro declarado no schema da ferramenta."""

    name: str
    ptype: str          # "string" | "integer" | "boolean" | "number"
    description: str
    required: bool = True


@dataclass
class ToolContext:
    """Dependências compartilhadas injetadas nas ferramentas.

    ``authorize`` é o ÚNICO caminho para decisões AUTO/ASK/DENY — a
    segurança não depende do prompt nem da boa vontade do LLM. O fluxo
    publica eventos permission.required/granted/denied no barramento,
    permitindo que qualquer interface (CLI hoje, WebSocket amanhã)
    apresente o pedido de confirmação ao usuário.
    """

    workspace: Path
    settings: Settings
    bus: EventBus
    confirm_callback: ConfirmCallback | None = None

    def authorize(self, command: str) -> bool:
        """Decide e, quando necessário, confirma com o usuário.

        Sem callback configurado, comandos ASK são negados (fail-safe).
        """
        approved, _detail = self.authorize_detailed(command)
        return approved

    def authorize_detailed(self, command: str) -> tuple[bool, str]:
        """Como ``authorize``, retornando também a decisão detalhada."""
        from security.permissions import Decision, PermissionManager
        manager = PermissionManager(mode=self.settings.security_mode)
        decision, reason = manager.evaluate(command)

        if decision is Decision.AUTO:
            return True, "AUTO"

        # informativo (sem request_id): interfaces com fluxo interativo
        # publicarão um segundo evento com request_id ao abrir o prompt
        self.bus.publish(EventType.PERMISSION_REQUIRED,
                         command=command, reason=reason,
                         mode=self.settings.security_mode,
                         informational=True)

        if decision is Decision.DENY:
            return False, f"DENY: {reason}"
        if self.confirm_callback is None:
            return False, "ASK sem usuário conectado (negado)"
        approved = bool(self.confirm_callback(command, reason))
        detail = "ASK aprovado pelo usuário" if approved \
            else "ASK negado pelo usuário"
        return approved, detail


@dataclass
class Tool:
    """Ferramenta registrada: metadados + handler executável."""

    name: str
    description: str
    parameters: list[ToolParameter]
    handler: Handler

    def to_schema(self) -> dict[str, Any]:
        """Schema no formato aceito pela API /api/chat do Ollama."""
        properties = {
            parameter.name: {
                "type": parameter.ptype,
                "description": parameter.description,
            }
            for parameter in self.parameters
        }
        required = [parameter.name for parameter in self.parameters
                    if parameter.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    """Registro central de ferramentas + executor seguro."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------ #
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Ferramenta duplicada: {tool.name}")
        self._tools[tool.name] = tool

    def register_function(self,
                          name: str,
                          description: str,
                          parameters: list[ToolParameter],
                          handler: Handler) -> None:
        self.register(Tool(name=name, description=description,
                           parameters=parameters, handler=handler))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in
                sorted(self._tools.values(), key=lambda t: t.name)]

    # ------------------------------------------------------------------ #
    def execute(self, name: str,
                arguments: dict[str, Any] | None,
                ctx: ToolContext) -> ToolResult:
        """Executa uma ferramenta convertendo qualquer falha em resultado."""
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(self.names())
            return ToolResult.fail(
                f"Ferramenta desconhecida: '{name}'. Disponíveis: {known}")
        arguments = arguments or {}
        try:
            return tool.handler(ctx, **arguments)
        except TypeError as exc:
            expected = ", ".join(parameter.name for parameter in tool.parameters)
            return ToolResult.fail(
                f"Argumentos inválidos para '{name}': {exc}. "
                f"Esperados: {expected}")
        except Exception as exc:  # noqa: BLE001 - erro vira dado para o LLM
            logger.exception("Falha na tool %s", name)
            return ToolResult.fail(f"{type(exc).__name__}: {exc}")


def clip(text: str, limit: int = 6000, label: str = "") -> str:
    """Trunca saídas longas mantendo o aviso explícito."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    suffix = f"\n...[{label} truncado: {omitted} caracteres omitidos]" \
        if label else f"\n...[truncado: {omitted} caracteres omitidos]"
    return text[:limit] + suffix
