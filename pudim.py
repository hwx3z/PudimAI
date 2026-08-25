#!/usr/bin/env python3
"""PudimAI — AI Software Engineering Platform local (Ollama).

Entrypoints:

    python pudim.py                     # CLI interativa
    python pudim.py --web               # interface Web (mesmo Core)
    python pudim.py --check             # diagnóstico do ambiente
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.ui import UI  # noqa: E402
from config.settings import Settings, resolve_workspace  # noqa: E402
from core.exceptions import ConfigurationError  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pudim",
        description="PudimAI — plataforma local de engenharia de software "
                    "com IA (CLI + Web sobre um único Agent Core).",
    )
    parser.add_argument("--version", action="version",
                        version="PudimAI 0.4.0")
    parser.add_argument("--workspace", metavar="CAMINHO",
                        help="diretório do projeto a ser aberto")
    parser.add_argument("--model", metavar="MODELO",
                        help="modelo Ollama (padrão: qwen2.5:7b)")
    parser.add_argument("--security-mode",
                        choices=("relaxed", "standard", "strict"),
                        help="política de execução de comandos")
    parser.add_argument("--check", action="store_true",
                        help="executa diagnóstico do ambiente e sai")
    parser.add_argument("--web", action="store_true",
                        help="inicia a interface Web em vez da CLI")
    parser.add_argument("--host", metavar="HOST",
                        help="host do servidor Web (padrão: 127.0.0.1)")
    parser.add_argument("--port", type=int, metavar="PORTA",
                        help="porta do servidor Web (padrão: 8000)")
    return parser.parse_args(argv)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


def _build_runtime(args: argparse.Namespace):
    from core.runtime import PudimAIRuntime

    workspace = resolve_workspace(args.workspace)
    settings = Settings.load(workspace)
    if args.model:
        settings.model = args.model
    if args.security_mode:
        settings.security_mode = args.security_mode
    if args.host:
        settings.web_host = args.host
    if args.port:
        settings.web_port = args.port
    settings.workspace = str(workspace)
    runtime = PudimAIRuntime(workspace, settings=settings)
    return runtime


# ---------------------------------------------------------------------- #
def _doctor(args: argparse.Namespace) -> int:
    ui = UI()
    ui.plain("PudimAI 0.4.0 — diagnóstico")
    failures = 0

    try:
        workspace = resolve_workspace(args.workspace)
        settings = Settings.load(workspace)
        if args.model:
            settings.model = args.model
        if args.security_mode:
            settings.security_mode = args.security_mode
        ui.success(f"Workspace: {workspace}")
    except ConfigurationError as exc:
        ui.error(f"Workspace: {exc}")
        return 1

    from core.llm import create_provider
    provider = create_provider(settings)

    try:
        models = provider.health_check()
        ui.success(f"Ollama acessível em {settings.ollama_url} "
                   f"({len(models)} modelo(s))")
        try:
            provider.ensure_model(models)
            ui.success(f"Modelo '{settings.model}' disponível")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            ui.error(str(exc).splitlines()[0])
            ui.dim(f"  → instale com: ollama pull {settings.model}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        ui.error(str(exc).splitlines()[0])
        ui.dim("  → inicie com: ollama serve")

    from tools.registry import build_default_registry
    registry = build_default_registry()
    ui.success(f"Ferramentas base ({len(registry)}): "
               + ", ".join(registry.names()))

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        ui.success("Interface Web disponível (fastapi + uvicorn)")
    except ImportError as exc:
        ui.warn(f"Web indisponível ({exc.name}) — instale as dependências "
                "para usar `pudim.py --web`")

    try:
        import requests  # noqa: F401
        import rich  # noqa: F401
        ui.success("Dependências base ok (requests, rich)")
    except ImportError as exc:
        failures += 1
        ui.error(f"Dependência ausente: {exc.name} — "
                 "rode: pip install -r requirements.txt")

    ui.plain()
    if failures:
        ui.error(f"{failures} problema(s) encontrado(s).")
        return 1
    ui.success("Ambiente pronto. Execute: python pudim.py  |  "
               "python pudim.py --web")
    return 0


# ---------------------------------------------------------------------- #
def _run_cli(runtime) -> int:
    from cli.interface import CLI
    app = CLI(runtime=runtime)
    try:
        return app.run()
    except KeyboardInterrupt:
        print("\nEncerrado.")
        return 130


def _run_web(runtime) -> int:
    try:
        import uvicorn
    except ImportError:
        print("[ERRO] Interface Web requer fastapi+uvicorn.\n"
              "       Instale com: pip install -r requirements.txt",
              file=sys.stderr)
        return 3

    from web.backend.app import create_app
    host = runtime.settings.web_host
    port = runtime.settings.web_port
    runtime.setup_file_logging()
    print(f"\n  PudimAI Web · http://{host}:{port}\n"
          f"  Workspace : {runtime.workspace.root}\n"
          f"  Modelo    : {runtime.settings.model}\n")
    uvicorn.run(create_app(runtime), host=host, port=port,
                log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.check:
        return _doctor(args)

    try:
        runtime = _build_runtime(args)
    except ConfigurationError as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 1

    _setup_logging(runtime.settings.log_level)
    try:
        return _run_web(runtime) if args.web else _run_cli(runtime)
    except KeyboardInterrupt:
        print("\nEncerrado.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
