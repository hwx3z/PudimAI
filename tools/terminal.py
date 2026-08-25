"""Tool de terminal do PudimAI.

Executa comandos via subprocess com:
    * classificação de segurança (AUTO/ASK/DENY) ANTES de rodar;
    * timeout configurável;
    * captura de stdout/stderr/exit_code/duração;
    * saída formatada exatamente no bloco que o agente sabe interpretar.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time

from core.events import EventType
from tools.base import ToolContext, ToolParameter, ToolRegistry, ToolResult, clip

MAX_OUTPUT = 6000
SHELL = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"


def register(registry: ToolRegistry) -> None:
    registry.register_function(
        name="executar_terminal",
        description="Executa um comando de shell dentro do workspace. "
                    "Comandos destrutivos são bloqueados; outros podem "
                    "pedir confirmação ao usuário. Retorna stdout, stderr, "
                    "código de saída e duração.",
        parameters=[ToolParameter(
            "comando", "string",
            "Comando a executar (ex.: 'pytest -x', 'python main.py').")],
        handler=_run_terminal,
    )


def _run_terminal(ctx: ToolContext, comando: str) -> ToolResult:
    comando = comando.strip()
    if not comando:
        return ToolResult.fail("Comando vazio.")

    # ---------------- camada de segurança (caminho único) ---------------- #
    from security.permissions import PermissionManager
    manager = PermissionManager(mode=ctx.settings.security_mode)
    decision, reason = manager.evaluate(comando)
    if decision.value == "DENY":
        ctx.bus.publish(EventType.TOOL_DENIED, command=comando,
                        reason=reason)
        return ToolResult.fail(
            f"[BLOQUEADO PELA SEGURANÇA] '{comando}' — motivo: {reason}. "
            "Este comando não será executado. Escolha outra abordagem.")
    if decision.value == "ASK":
        approved, detail = ctx.authorize_detailed(comando)
        if not approved:
            ctx.bus.publish(EventType.TOOL_DENIED, command=comando,
                            reason=detail)
            return ToolResult.fail(
                f"O usuário NEGOU a execução de '{comando}' ({detail}). "
                "Não tente novamente sem permissão; proponha alternativa.")
        approved = True
    else:
        approved = False

    # ---------------- execução ---------------- #
    ctx.bus.publish(EventType.TERMINAL_STARTED, command=comando)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["TERM"] = env.get("TERM", "dumb")
    started = time.perf_counter()
    timed_out = False
    try:
        completed = subprocess.run(
            comando,
            shell=True,
            executable=SHELL,
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ctx.settings.command_timeout,
            env=env,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = _coerce(exc.stdout)
        stderr = (_coerce(exc.stderr) +
                  f"\n[TIMEOUT] Excedeu {ctx.settings.command_timeout}s "
                  "e foi encerrado.")
    except FileNotFoundError as exc:
        duration = time.perf_counter() - started
        return _finish(ctx, comando, None, "", str(exc), duration,
                       approved, success=False)
    except OSError as exc:
        duration = time.perf_counter() - started
        return _finish(ctx, comando, None, "",
                       f"erro de SO: {exc}", duration, approved, success=False)

    duration = time.perf_counter() - started
    return _finish(ctx, comando, exit_code, stdout, stderr, duration,
                   approved, success=(not timed_out and exit_code == 0))


# ---------------------------------------------------------------------- #
def _finish(ctx: ToolContext, comando: str, exit_code: int | None,
            stdout: str, stderr: str, duration: float,
            asked_user: bool, success: bool) -> ToolResult:
    ctx.bus.publish(EventType.TERMINAL_COMPLETED, command=comando,
                    exit_code=exit_code, seconds=round(duration, 3))
    block = (
        f"COMANDO:\n{comando}\n\n"
        f"EXIT CODE:\n{exit_code}\n\n"
        f"STDOUT:\n{clip(stdout, MAX_OUTPUT, 'stdout') or '[vazio]'}\n\n"
        f"STDERR:\n{clip(stderr, MAX_OUTPUT, 'stderr') or '[vazio]'}\n\n"
        f"DURAÇÃO:\n{duration:.2f}s\n"
    )
    prefix = "[confirmado pelo usuário] " if asked_user else ""
    return ToolResult(success=success, output=prefix + block.rstrip(),
                      data={"command": comando, "exit_code": exit_code,
                            "seconds": round(duration, 3)})


def _coerce(value) -> str:
    """subprocess pode devolver bytes ou str dependendo do momento."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def quote(arg: str) -> str:
    """Helper público para montar comandos seguros (uso futuro das tools)."""
    return shlex.quote(arg)


def supports_shell_features() -> bool:  # utilidade para testes/documentação
    return sys.platform != "win32" or SHELL != "/bin/sh"
