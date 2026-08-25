"""Configuração central do PudimAI.

Precedência (da menor para a maior):
    1. Defaults desta classe
    2. Arquivo  <workspace>/.pudimai/config.json
    3. Variáveis de ambiente  PUDIMAI_*

A configuração vive dentro do workspace ativo, de modo que cada projeto
carrega suas próprias preferências (.pudimai/config.json).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from core.exceptions import ConfigurationError

CONFIG_DIRNAME = ".pudimai"
CONFIG_FILENAME = "config.json"

_ENV_MAP = {
    "ollama_url": "PUDIMAI_OLLAMA_URL",
    "model": "PUDIMAI_MODEL",
    "max_iterations": "PUDIMAI_MAX_ITERATIONS",
    "command_timeout": "PUDIMAI_COMMAND_TIMEOUT",
    "llm_timeout": "PUDIMAI_LLM_TIMEOUT",
    "workspace": "PUDIMAI_WORKSPACE",
    "log_level": "PUDIMAI_LOG_LEVEL",
    "security_mode": "PUDIMAI_SECURITY_MODE",
    "context_messages": "PUDIMAI_CONTEXT_MESSAGES",
    "context_max_chars": "PUDIMAI_CONTEXT_MAX_CHARS",
    "web_host": "PUDIMAI_WEB_HOST",
    "web_port": "PUDIMAI_WEB_PORT",
    "web_terminal": "PUDIMAI_WEB_TERMINAL",
    "permission_timeout": "PUDIMAI_PERMISSION_TIMEOUT",
    "cli_require_login": "PUDIMAI_CLI_REQUIRE_LOGIN",
}

_INT_FIELDS = {"max_iterations", "command_timeout", "llm_timeout",
               "context_messages", "context_max_chars", "web_port",
               "permission_timeout"}

_BOOL_FIELDS = {"auto_plan", "web_terminal", "cli_require_login"}

VALID_SECURITY_MODES = ("relaxed", "standard", "strict")


@dataclass
class Settings:
    """Parâmetros globais do PudimAI."""

    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b"
    max_iterations: int = 30
    command_timeout: int = 120
    llm_timeout: int = 300
    workspace: str = ""
    log_level: str = "INFO"
    security_mode: str = "standard"
    context_messages: int = 60
    context_max_chars: int = 90_000
    auto_plan: bool = True

    # Web (usada por `pudim.py --web`)
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_terminal: bool = True   # terminal interativo do "Computador da IA"
    # Tempo máximo (s) aguardando aprovação humana de um comando ASK
    permission_timeout: int = 300
    # Papéis de modelo (multi-modelo futuro): {"coding": "...", ...}
    model_roles: dict = None  # type: ignore[assignment]
    # CLI: exigir /login antes de executar tarefas (padrão: livre)
    cli_require_login: bool = False

    def __post_init__(self) -> None:
        if self.model_roles is None:
            object.__setattr__(self, "model_roles", {})

    # ------------------------------------------------------------------ #
    # Carga / persistência
    # ------------------------------------------------------------------ #
    @classmethod
    def config_dir(cls, workspace: Path) -> Path:
        return workspace / CONFIG_DIRNAME

    @classmethod
    def config_path(cls, workspace: Path) -> Path:
        return cls.config_dir(workspace) / CONFIG_FILENAME

    @classmethod
    def load(cls, workspace: Path) -> "Settings":
        """Carrega defaults <- config.json <- variáveis de ambiente."""
        data: dict = {}
        path = cls.config_path(workspace)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ConfigurationError(
                    f"Não foi possível ler {path}: {exc}"
                ) from exc

        values: dict = {}
        for f in fields(cls):
            if f.name in data:
                values[f.name] = data[f.name]
            env = _ENV_MAP.get(f.name)
            if env and os.environ.get(env):
                raw = os.environ[env]
                if f.name in _INT_FIELDS:
                    values[f.name] = int(raw)
                elif f.name in _BOOL_FIELDS:
                    values[f.name] = raw.strip().lower() not in (
                        "0", "false", "off", "no")
                else:
                    values[f.name] = raw

        settings = cls(**values)

        if not isinstance(settings.model_roles, dict):
            raise ConfigurationError(
                "model_roles deve ser um objeto {papel: modelo}.")
        if settings.security_mode not in VALID_SECURITY_MODES:
            raise ConfigurationError(
                f"security_mode inválido: '{settings.security_mode}'. "
                f"Use um de {VALID_SECURITY_MODES}."
            )
        if settings.max_iterations < 1 or settings.max_iterations > 200:
            raise ConfigurationError("max_iterations deve estar entre 1 e 200.")
        return settings

    def save(self, workspace: Path) -> None:
        """Persiste a configuração em <workspace>/.pudimai/config.json."""
        cfg_dir = self.config_dir(workspace)
        cfg_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["workspace"] = ""  # o workspace é implícito (diretório-pai)
        self.config_path(workspace).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def resolve_workspace(cli_arg: str | None) -> Path:
    """Resolve o diretório de trabalho inicial.

    Ordem: argumento CLI > variável de ambiente > diretório atual.
    """
    candidate = cli_arg or os.environ.get("PUDIMAI_WORKSPACE") or os.getcwd()
    path = Path(candidate).expanduser().resolve()
    if not path.exists():
        raise ConfigurationError(f"Workspace não existe: {path}")
    if not path.is_dir():
        raise ConfigurationError(f"Workspace não é um diretório: {path}")
    return path
