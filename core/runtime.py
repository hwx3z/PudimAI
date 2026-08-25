"""PudimAIRuntime — montagem canônica e ÚNICA do PudimAI Core.

Garante o princípio ONE CORE / MULTIPLE INTERFACES: CLI, Web e testes
recebem exatamente os mesmos objetos (Agent, LLM, Registry, Sessions,
Permissions, Plugins) construídos aqui. Nenhuma interface monta o core
manualmente; trocar de workspace = construir um novo Runtime.
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import Settings
from core.agent import Agent
from core.auth import AuthService
from core.context import ContextManager
from core.events import EventBus
from core.llm import LLMProvider, create_provider
from core.memory import MemoryStore
from core.models import ModelRouter
from core.mcp import MCPManager
from core.plugins import PluginManager
from core.planner import Planner
from core.sessions import SessionManager
from core.workspace import Workspace
from tools.base import ToolContext
from tools.registry import build_default_registry

logger = logging.getLogger("pudimai.runtime")


class PudimAIRuntime:
    """Container com injeção de dependências do núcleo."""

    def __init__(self,
                 workspace: Path,
                 settings: Settings | None = None,
                 llm_override: LLMProvider | None = None) -> None:
        self.settings = settings or Settings.load(workspace)
        self.settings.workspace = str(workspace)

        self.bus = EventBus()
        self.workspace = Workspace(workspace)
        self.storage_dir = self.workspace.storage_dir()

        # persistência + sessões (camada única para toda interface)
        self.memory = MemoryStore(self.storage_dir)
        self.sessions = SessionManager(self.memory)

        # contas locais compartilhadas entre CLI (/login) e Web (login)
        self.auth = AuthService(self.storage_dir)

        # LLM desacoplado (llm_override permite testes sem Ollama)
        self.llm: LLMProvider = llm_override or create_provider(self.settings)
        self.model_router = ModelRouter(self.settings)

        # ferramentas base + plugins do projeto/empacotados
        self.registry = build_default_registry()
        self.plugins = PluginManager(
            self.registry, self.bus,
            extra_dirs=[self.storage_dir / "plugins"])
        self.plugins.load_all()
        if self.plugins.failed:
            logger.warning("Plugins com falha: %s", self.plugins.failed)

        # MCP (inventário passivo; bridges reais são futuros)
        self.mcp = MCPManager()

        self.context = ContextManager(self.settings, self.workspace)
        self.planner = Planner(self.llm, self.workspace)

        self.tool_ctx = ToolContext(
            workspace=self.workspace.root,
            settings=self.settings,
            bus=self.bus,
            confirm_callback=None,  # cada interface injeta o seu
        )
        self.default_agent = self.build_agent(self.bus, self.tool_ctx)

    # ------------------------------------------------------------------ #
    def build_agent(self, bus: EventBus | None = None,
                    tool_ctx: ToolContext | None = None) -> Agent:
        """Cria um Agent sobre ESTE núcleo (permite buses com escopo)."""
        return Agent(
            settings=self.settings,
            llm=self.llm,
            registry=self.registry,
            context=ContextManager(self.settings, self.workspace),
            planner=Planner(self.llm, self.workspace),
            bus=bus or self.bus,
            tool_ctx=tool_ctx or self.tool_ctx,
        )

    # ------------------------------------------------------------------ #
    # Infraestrutura de log por workspace (um único lugar)
    # ------------------------------------------------------------------ #
    def setup_file_logging(self) -> Path:
        logs_dir = self.storage_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "pudimai.log"
        root_logger = logging.getLogger("pudimai")
        already = any(isinstance(handler, logging.FileHandler) and
                      getattr(handler, "baseFilename", "") == str(log_path)
                      for handler in root_logger.handlers)
        if not already:
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"))
            root_logger.addHandler(handler)
            self.bus.subscribe(lambda event: logger.info(
                "%s %s", event.type.value, _compact(event.payload)))
        return log_path


def _compact(payload: dict, limit: int = 300) -> str:
    import json
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(payload)
    return text[:limit]
