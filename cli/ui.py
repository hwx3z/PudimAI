"""Camada de renderização da CLI.

Usa `rich` quando disponível (painéis, tabelas, cores) e degrada para
texto ANSI puro caso a biblioteca não esteja instalada — o PudimAI nunca
deixa de iniciar por causa do visual.
"""
from __future__ import annotations

import sys

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    _RICH = True
except Exception:  # noqa: BLE001 - rich é opcional
    _RICH = False


class UI:
    """Wrapper minimalista sobre rich/ANSI."""

    def __init__(self) -> None:
        self._console = Console() if _RICH else None

    @property
    def is_rich(self) -> bool:
        return bool(self._console)

    # ------------------------------------------------------------------ #
    def banner(self, model: str, url: str, workspace: str,
               security_mode: str, version: str) -> None:
        body = (
            f"[bold]PudimAI[/bold] v{version}\n"
            "Agente Autônomo de Engenharia de Software (IA local)\n\n"
            f"Modelo   : {model}\n"
            f"Provedor : Ollama ({url})\n"
            f"Workspace: {workspace}\n"
            f"Segurança: {security_mode}  •  /help para comandos"
        )
        if self._console:
            self._console.print(Panel(body,
                                      border_style="cyan", expand=False))
        else:
            width = 56
            print("╭" + "─" * width + "╮")
            for line in body.replace("[bold]", "").replace("[/bold]", "").splitlines():
                print("│ " + line.ljust(width - 1) + "│")
            print("╰" + "─" * width + "╯")

    # ------------------------------------------------------------------ #
    def info(self, text: str) -> None:
        if self._console:
            self._console.print(f"[cyan]•[/cyan] {text}")
        else:
            print(f"\033[36m•\033[0m {text}")

    def success(self, text: str) -> None:
        if self._console:
            self._console.print(f"[green]✓[/green] {text}")
        else:
            print(f"\033[32m✓\033[0m {text}")

    def warn(self, text: str) -> None:
        if self._console:
            self._console.print(f"[yellow]![/yellow] {text}")
        else:
            print(f"\033[33m!\033[0m {text}")

    def error(self, text: str) -> None:
        if self._console:
            self._console.print(f"[red]✗[/red] {text}")
        else:
            print(f"\033[31m✗\033[0m {text}")

    def dim(self, text: str) -> None:
        if self._console:
            self._console.print(f"[dim]{text}[/dim]")
        else:
            print(f"\033[2m{text}\033[0m")

    def plain(self, text: str = "") -> None:
        if self._console:
            self._console.print(text)
        else:
            print(text)

    def markdown(self, text: str) -> None:
        if self._console:
            self._console.print(Markdown(text))
        else:
            print(text)

    def stream_delta(self, delta: str) -> None:
        """Imprime tokens conforme chegam (efeito máquina de escrever)."""
        try:
            sys.stdout.write(delta)
            sys.stdout.flush()
        except OSError:
            pass

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        if self._console:
            table = Table(show_header=True, header_style="bold")
            for header in headers:
                table.add_column(header)
            for row in rows:
                table.add_row(*row)
            self._console.print(table)
        else:
            print(" | ".join(headers))
            print("-" * 40)
            for row in rows:
                print(" | ".join(row))

    def prompt(self) -> str:
        label = "PudimAI › "
        if self._console and self.is_rich:
            self._console.print(f"[bold cyan]{label}[/bold cyan]", end="")
            return ""
        return f"\033[1;36m{label}\033[0m"

    def confirm(self, command: str, reason: str) -> bool:
        """Pede confirmação humana para comandos ASK."""
        self.warn(f"Confirma execução? ({reason})")
        if self._console:
            self._console.print(f"  [bold]$ {command}[/bold]")
        else:
            print(f"  $ {command}")
        try:
            answer = input("  [s/N] › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("s", "sim", "y", "yes")
