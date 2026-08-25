"""Sistema de Plugins do PudimAI.

Um plugin é um diretório com:

    meu-plugin/
    ├── manifest.json    {"name", "version", "description", "entrypoint",
    │                     "permissions": [...]}
    └── <entrypoint>     módulo Python expondo register(api: PluginAPI)

Diretórios varridos (em ordem):
    1. <workspace>/.pudimai/plugins   (plugins do projeto)
    2. <raiz do PudimAI>/plugins      (plugins empacotados)

Contrato estável v1 — um plugin pode registrar:
    * Tools  (aparecem para o Agent Core como qualquer outra)

Falhas em um plugin NUNCA derrubam o núcleo: são registradas e ignoradas.

PREPARADO PARA O FUTURO: comandos CLI, painéis Web, providers, hooks de
evento e enforcement granular das permissões declaradas no manifest.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from core.events import EventBus, EventType
from tools.base import ToolParameter, ToolRegistry

logger = logging.getLogger("pudimai.plugins")

MANIFEST_NAME = "manifest.json"


@dataclass
class PluginManifest:
    name: str
    version: str
    description: str
    entrypoint: str
    permissions: list[str]
    path: Path


class PluginAPI:
    """Superfície pública entregue a cada plugin na inicialização."""

    def __init__(self, plugin_name: str, registry: ToolRegistry,
                 bus: EventBus) -> None:
        self.plugin_name = plugin_name
        self._registry = registry
        self.bus = bus

    def register_tool(self, name: str, description: str,
                      parameters: list[ToolParameter],
                      handler: Callable[..., object]) -> None:
        """Registra uma tool em nome do plugin (prefixo pelo nome dele)."""
        qualified = f"{self.plugin_name}__{name}"
        self._registry.register_function(
            name=qualified,
            description=f"[plugin:{self.plugin_name}] {description}",
            parameters=parameters,
            handler=handler,
        )
        logger.info("Tool '%s' registrada pelo plugin '%s'",
                    qualified, self.plugin_name)


class PluginManager:
    """Descobre, valida e carrega plugins isolando falhas individuais."""

    def __init__(self, registry: ToolRegistry, bus: EventBus,
                 extra_dirs: list[Path] | None = None) -> None:
        self._registry = registry
        self._bus = bus
        package_root = Path(__file__).resolve().parent.parent
        self.search_dirs: list[Path] = [
            *(extra_dirs or []),
            package_root / "plugins",
        ]
        self.loaded: list[PluginManifest] = []
        self.failed: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ #
    def load_all(self, workspace_plugin_dir: Path | None = None) -> None:
        directories: list[Path] = []
        if workspace_plugin_dir is not None:
            directories.append(workspace_plugin_dir)
        directories.extend(self.search_dirs)

        seen: set[Path] = set()
        for base in directories:
            if not base.is_dir() or base in seen:
                continue
            seen.add(base)
            for candidate in sorted(base.iterdir()):
                if candidate.is_dir():
                    self._try_load(candidate)

    def _try_load(self, directory: Path) -> None:
        try:
            manifest = self._read_manifest(directory)
        except Exception as exc:  # noqa: BLE001 - plugin ruim != app ruim
            self.failed.append((directory.name, f"manifest inválido: {exc}"))
            return

        try:
            module = self._import_entrypoint(manifest)
            api = PluginAPI(manifest.name, self._registry, self._bus)
            register = getattr(module, "register", None)
            if not callable(register):
                raise AttributeError(
                    "entrypoint sem função register(api)")
            register(api)
            self.loaded.append(manifest)
            self._bus.publish(EventType.TOOL_COMPLETED,
                              summary=f"plugin carregado: {manifest.name} "
                                      f"v{manifest.version}",
                              name="plugin_loader")
            logger.info("Plugin carregado: %s v%s", manifest.name,
                        manifest.version)
        except Exception as exc:  # noqa: BLE001
            self.failed.append((directory.name, repr(exc)))
            logger.exception("Falha ao carregar plugin '%s'", directory.name)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_manifest(directory: Path) -> PluginManifest:
        manifest_path = directory / MANIFEST_NAME
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = PluginManifest(
            name=str(raw["name"]).strip(),
            version=str(raw.get("version", "0.0.0")),
            description=str(raw.get("description", "")),
            entrypoint=str(raw.get("entrypoint", "plugin.py")),
            permissions=list(raw.get("permissions", [])),
            path=directory,
        )
        if not manifest.name.replace("_", "").isalnum():
            raise ValueError(f"nome de plugin inválido: {manifest.name!r}")
        if not (directory / manifest.entrypoint).is_file():
            raise FileNotFoundError(
                f"entrypoint não encontrado: {manifest.entrypoint}")
        return manifest

    def _import_entrypoint(self, manifest: PluginManifest) -> ModuleType:
        module_path = manifest.path / manifest.entrypoint
        module_name = f"pudimai_plugin_{manifest.name}"
        spec = importlib.util.spec_from_file_location(module_name,
                                                      module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"não foi possível importar {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module  # permite imports relativos futuros
        spec.loader.exec_module(module)
        return module
