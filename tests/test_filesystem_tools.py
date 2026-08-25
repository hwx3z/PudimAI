"""Testes das tools de sistema de arquivos e da fronteira do workspace."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from core.events import EventBus
from core.workspace import Workspace
from tools.base import ToolContext, ToolRegistry
from tools.filesystem import register


def make_context(root: Path) -> ToolContext:
    return ToolContext(workspace=root, settings=Settings(),
                       bus=EventBus(), confirm_callback=None)


class FilesystemToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = make_context(self.root)
        self.registry = ToolRegistry()
        register(self.registry)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    def test_write_and_read_roundtrip(self):
        result = self.registry.execute(
            "escrever_arquivo",
            {"caminho": "src/app.py", "conteudo": "print('olá')"},
            self.ctx)
        self.assertTrue(result.success, result.output)

        read = self.registry.execute("ler_arquivo",
                                     {"caminho": "src/app.py"}, self.ctx)
        self.assertTrue(read.success, read.output)
        self.assertIn("print('olá')", read.output)

    def test_read_missing_file(self):
        result = self.registry.execute("ler_arquivo",
                                       {"caminho": "nao_existe.txt"},
                                       self.ctx)
        self.assertFalse(result.success)
        self.assertIn("não existe", result.output)

    def test_path_traversal_blocked(self):
        result = self.registry.execute("escrever_arquivo",
                                       {"caminho": "../fora.txt",
                                        "conteudo": "x"}, self.ctx)
        self.assertFalse(result.success)
        self.assertIn("proibido", result.output)

    def test_list_directory(self):
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        (self.root / "sub").mkdir()
        result = self.registry.execute("listar_diretorio", {}, self.ctx)
        self.assertTrue(result.success, result.output)
        self.assertIn("a.txt", result.output)
        self.assertIn("sub/", result.output)

    def test_search_files_respects_ignores(self):
        node = self.root / "node_modules" / "pkg"
        node.mkdir(parents=True)
        (node / "index.js").write_text(";", encoding="utf-8")
        (self.root / "main.py").write_text("x", encoding="utf-8")

        result = self.registry.execute("buscar_arquivos",
                                       {"consulta": "*.py"}, self.ctx)
        self.assertTrue(result.success)
        self.assertIn("main.py", result.output)

        hidden = self.registry.execute("buscar_arquivos",
                                       {"consulta": "index.js"}, self.ctx)
        self.assertIn("Nenhum arquivo", hidden.output)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_project(self) -> None:
        (self.root / "pyproject.toml").write_text("[project]\nname='x'\n",
                                                  encoding="utf-8")
        (self.root / "app").mkdir()
        (self.root / "app" / "main.py").write_text("print('oi')\n",
                                                   encoding="utf-8")

    def test_detects_python_stack(self):
        self._make_project()
        info = Workspace(self.root).info
        self.assertIn("pyproject.toml", info.markers)
        self.assertIn("Python", info.languages)

    def test_resolve_allows_inside_and_blocks_outside(self):
        workspace = Workspace(self.root)
        inside = workspace.resolve_path("app/main.py")
        self.assertTrue(str(inside).startswith(str(self.root)))
        with self.assertRaises(PermissionError):
            workspace.resolve_path("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
