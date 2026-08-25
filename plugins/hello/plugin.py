"""Plugin de exemplo do PudimAI.

Demonstra o contrato mínimo: uma função ``register(api)`` que registra
tools pelo PluginAPI. Serve como gabarito para plugins reais.
"""
from tools.base import ToolParameter, ToolResult


def register(api) -> None:
    api.register_tool(
        name="dizer_ola",
        description="Retorna uma saudação amigável do PudimAI (tool de "
                    "exemplo instalada por plugin).",
        parameters=[ToolParameter("nome", "string",
                                  "Nome de quem será saudado.",
                                  required=False)],
        handler=_dizer_ola,
    )


def _dizer_ola(ctx, nome: str = "mundo") -> ToolResult:
    # ctx é o ToolContext padrão (workspace, settings, bus...)
    return ToolResult.ok(f"Olá, {nome}! Um abraço pudim do plugin 'hello'.")
