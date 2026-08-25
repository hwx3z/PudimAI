"""Sessão de terminal (PTY) do "Computador da IA".

Cria um shell interativo real dentro do workspace, conectado ao
WebSocket /ws/term. É o MESMO ambiente que o agente usa (mesmo cwd,
mesmas ferramentas enxergando os mesmos arquivos) — o usuário apenas
assume o teclado.

Segurança:
    * exige login (validado na rota WS);
    * desligável via settings.web_terminal / PUDIMAI_WEB_TERMINAL=0;
    * servidor escuta em 127.0.0.1 por padrão.

Limitação conhecida: o cliente é line-based (sem emulação TUI completa),
então apps full-screen como vim não renderizam; comandos e saídas normais
funcionam perfeitamente.
"""
from __future__ import annotations

import os
import select
import signal

try:
    import pty  # POSIX
    PTY_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows
    pty = None  # type: ignore[assignment]
    PTY_AVAILABLE = False

READ_CHUNK = 65_536


def default_shell() -> str:
    """Shell do 'Computador da IA'.

    Preferimos bash/sh explícitos em vez de $SHELL do usuário: shells
    interativos modernos (ex.: fish) emitem queries de capabilities no
    boot e ficam aguardando respostas — sem emulador completo do lado
    do cliente isso trava o prompt.
    """
    for candidate in ("/bin/bash", "/bin/sh"):
        if os.path.exists(candidate):
            return candidate
    return os.environ.get("SHELL", "/bin/sh")


class PtySession:
    """Shell filho com PTY para I/O interativo."""

    def __init__(self, cwd: str, shell: str | None = None) -> None:
        if not PTY_AVAILABLE:
            raise RuntimeError("PTY indisponível nesta plataforma.")
        self.shell = shell or default_shell()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # processo filho vira o shell
            try:
                os.chdir(cwd)
            except OSError:
                pass
            env = os.environ.copy()
            env["TERM"] = "dumb"          # sem queries de terminal
            env["NO_COLOR"] = "1"
            env["PUDIMAI"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            os.execvpe(self.shell, [self.shell, "--norc", "-i"], env)
        self._closed = False

    # ------------------------------------------------------------------ #
    def write(self, data: str) -> None:
        if self._closed or not data:
            return
        try:
            os.write(self.fd, data.encode("utf-8"))
        except OSError:
            pass

    def read(self, timeout: float = 0.25) -> str | None:
        """Lê saída disponível.

        Retorna None se nada chegou no intervalo, "" quando o lado do
        shell fechou (EOF/EIO), ou o texto decodificado.
        """
        if self._closed:
            return ""
        try:
            readable, _, _ = select.select([self.fd], [], [], timeout)
        except OSError:
            return ""
        if not readable:
            return None
        try:
            chunk = os.read(self.fd, READ_CHUNK)
        except OSError:  # EIO após exit do filho é o caminho comum
            return ""
        if not chunk:
            return ""
        return chunk.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.kill(self.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed
