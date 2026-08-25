"""Tool de segurança do PudimAI (Security AI — uso defensivo/autorizado).

Expõe o SecurityAnalyzer como uma tool que o agente pode invocar para
auditar segredos expostos no workspace.
"""
from __future__ import annotations

from security.analyzer import SecurityAnalyzer, format_report
from tools.base import ToolContext, ToolParameter, ToolRegistry, ToolResult


def register(registry: ToolRegistry) -> None:
    registry.register_function(
        name="analisar_seguranca",
        description="Varre o projeto em busca de segredos expostos "
                    "(chaves AWS/tokens/senhas hardcoded) e riscos óbvios. "
                    "Análise estática defensiva do código local.",
        parameters=[ToolParameter(
            "caminho", "string",
            "Arquivo ou diretório a auditar ('.' = workspace inteiro).",
            required=False)],
        handler=_security_scan,
    )


def _security_scan(ctx: ToolContext, caminho: str = ".") -> ToolResult:
    target = ctx.workspace / "." if caminho in (".", "", None) else None
    resolved = ctx.workspace / (caminho or ".")
    try:
        resolved.relative_to(ctx.workspace)
    except ValueError:
        return ToolResult.fail(f"Caminho fora do workspace: {caminho}")
    if not resolved.exists() and not (target and ctx.workspace.exists()):
        return ToolResult.fail(f"Caminho não existe: {caminho}")

    analyzer = SecurityAnalyzer(ctx.workspace)
    findings = analyzer.scan(caminho)
    report = format_report(findings, ctx.workspace / (caminho or "."))
    return ToolResult(success=True,
                      output=report,
                      data={"findings": len(findings)})
