"""Tools Git (leitura) do PudimAI.

Git é apenas mais uma tool — nada de lógica de git acoplada ao Agent.
Operações que alteram histórico/remoto passam pela classificação ASK da
camada de permissões quando executadas via executar_terminal.
"""
from __future__ import annotations

import subprocess

from tools.base import ToolContext, ToolParameter, ToolRegistry, ToolResult, clip


def register(registry: ToolRegistry) -> None:
    registry.register_function(
        name="git_status",
        description="Retorna o status atual do repositório git "
                    "(equivalente a `git status --short --branch`). "
                    "Falha graciosamente fora de um repositório.",
        parameters=[],
        handler=_git_status,
    )
    registry.register_function(
        name="git_diff",
        description="Mostra as diferenças não commitadas (`git diff`) ou, "
                    "com staged=true, o que está no stage.",
        parameters=[ToolParameter("staged", "boolean",
                                  "Mostrar apenas alterações em staging.",
                                  required=False)],
        handler=_git_diff,
    )
    registry.register_function(
        name="git_log",
        description="Lista os commits recentes (hash curto, autor, data, "
                    "mensagem).",
        parameters=[ToolParameter("limite", "integer",
                                  "Número de commits (padrão 10).",
                                  required=False)],
        handler=_git_log,
    )


def _run_git(ctx: ToolContext, args: list[str]) -> tuple[int | None, str, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except FileNotFoundError:
        return None, "", "git não está instalado no sistema."
    except subprocess.TimeoutExpired:
        return None, "", "git excedeu o tempo limite (30s)."
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _not_a_repo(stderr: str) -> ToolResult:
    if "not a git repository" in stderr.lower():
        return ToolResult.ok("Este workspace não é um repositório git.")
    return ToolResult.fail(clip(stderr, 2000))


def _git_status(ctx: ToolContext) -> ToolResult:
    code, out, err = _run_git(ctx, ["status", "--short", "--branch"])
    if code is None:
        return ToolResult.fail(err)
    if code != 0:
        return _not_a_repo(err)
    return ToolResult.ok(clip(out.strip() or "[árvore limpa]", 4000))


def _git_diff(ctx: ToolContext, staged: bool = False) -> ToolResult:
    args = ["diff", "--stat"] if staged else ["diff"]
    code, out, err = _run_git(ctx, args)
    if code is None:
        return ToolResult.fail(err)
    if code != 0:
        return _not_a_repo(err)
    return ToolResult.ok(clip(out.strip() or "[sem diferenças]", 5000))


def _git_log(ctx: ToolContext, limite: int = 10) -> ToolResult:
    limite = max(1, min(int(limite), 50))
    fmt = "--pretty=format:%h %an %ad %s" + (" " * 0)
    args = ["log", "-n", str(limite), "--date=short", fmt]
    code, out, err = _run_git(ctx, args)
    if code is None:
        return ToolResult.fail(err)
    if code != 0:
        if "does not have any commits yet" in err.lower() or \
                "bad revision" in err.lower():
            return ToolResult.ok("Repositório sem commits ainda.")
        return _not_a_repo(err)
    return ToolResult.ok(clip(out.strip(), 5000))
