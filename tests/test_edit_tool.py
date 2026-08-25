"""Testes da tool editar_arquivo (edição cirúrgica)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.events import EventBus
from tools.base import ToolContext, ToolRegistry
from tools.filesystem import register

CONTENT = "def calcular():\n    return 1\n\n\ndef outra():\n    return 1\n"


class EditFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "app.py").write_text(CONTENT, encoding="utf-8")
        self.ctx = ToolContext(workspace=self.root,
                               settings=type("S", (), {
                                   "context_max_chars": 90_000})(),
                               bus=EventBus(), confirm_callback=None)
        self.registry = ToolRegistry()
        register(self.registry)

    def tearDown(self):
        self._tmp.cleanup()

    def _edit(self, **overrides):
        args = {"caminho": "app.py",
                "antigo": "def calcular():\n    return 1",
                "novo": "def calcular():\n    return 42"}
        args.update(overrides)
        return self.registry.execute("editar_arquivo", args, self.ctx)

    def test_targeted_replacement(self):
        result = self._edit()
        self.assertTrue(result.success, result.output)
        text = (self.root / "app.py").read_text(encoding="utf-8")
        self.assertIn("return 42", text)
        # apenas a PRIMEIRA definição muda; o resto permanece intacto
        self.assertIn("def outra():\n    return 1", text)

    def test_occurrence_selector(self):
        result = self._edit(antigo="return 1", novo="return 9",
                            ocorrencia=2)
        self.assertTrue(result.success)
        text = (self.root / "app.py").read_text(encoding="utf-8")
        self.assertEqual(1, text.count("return 9"))
        self.assertIn("def calcular():\n    return 1", text)

    def test_ambiguous_without_occurrence_fails_clean(self):
        result = self._edit(antigo="return 1", novo="x")
        self.assertFalse(result.success)
        self.assertIn("aparece 2x", result.output)

    def test_missing_snippet_reports_exact_error(self):
        result = self._edit(antigo="isso nao existe", novo="x")
        self.assertFalse(result.success)
        self.assertIn("não encontrado", result.output)

    def test_requires_existing_file(self):
        result = self.registry.execute("editar_arquivo", {
            "caminho": "novo.py", "antigo": "a", "novo": "b"}, self.ctx)
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
