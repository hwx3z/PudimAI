"""Testes do Context Manager e da normalização de respostas do Ollama."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from core.context import ContextManager
from core.llm import OllamaProvider
from core.workspace import Workspace


class ContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pyproject.toml").write_text("[project]\n",
                                                  encoding="utf-8")
        self.settings = Settings(workspace=str(self.root))
        self.manager = ContextManager(self.settings, Workspace(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_system_prompt_contains_workspace_and_tools(self):
        schemas = [{
            "type": "function",
            "function": {"name": "ler_arquivo", "description": "lê arquivo",
                         "parameters": {}},
        }]
        prompt = self.manager.build_system_prompt(schemas, plan=["Passo 1"])
        for expected in ("PudimAI", "ler_arquivo", "Passo 1",
                         "STANDARD", str(self.root)):
            self.assertIn(expected, prompt)

    def test_trim_keeps_system_and_recent(self):
        settings = Settings(context_messages=5, context_max_chars=10_000)
        manager = ContextManager(settings, Workspace(self.root))
        messages = [{"role": "system", "content": "sistema"}]
        messages += [{"role": "user", "content": f"mensagem {i}"}
                     for i in range(30)]
        trimmed = manager.trim(messages)
        self.assertEqual("system", trimmed[0]["role"])
        self.assertLessEqual(len(trimmed), 6)
        self.assertEqual("mensagem 29", trimmed[-1]["content"])


class OllamaNormalizationTests(unittest.TestCase):
    def test_tool_call_arguments_from_json_string(self):
        call = OllamaProvider._normalize_tool_call({
            "function": {"name": "ler_arquivo",
                         "arguments": '{"caminho": "main.py"}'}})
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual({"caminho": "main.py"}, call.arguments)

    def test_tool_call_malformed_arguments_become_empty(self):
        with self.assertLogs("pudimai.llm", level="WARNING"):
            call = OllamaProvider._normalize_tool_call({
                "function": {"name": "x", "arguments": "{quebrado"}})
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual({}, call.arguments)

    def test_tool_call_without_name_is_dropped(self):
        self.assertIsNone(
            OllamaProvider._normalize_tool_call({"function": {}}))


if __name__ == "__main__":
    unittest.main()
