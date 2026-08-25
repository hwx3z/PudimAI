"""Testes de memória, planner e security analyzer."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.llm import LLMResponse
from core.memory import MemoryStore, Session
from core.planner import Planner, needs_planning
from core.workspace import WorkspaceInfo
from security.analyzer import SecurityAnalyzer


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_session_roundtrip(self):
        session = Session(title="Teste", model="qwen2.5:7b")
        session.messages = [{"role": "user", "content": "olá"},
                            {"role": "assistant", "content": "oi"}]
        self.store.save_session(session)

        loaded = self.store.load_session(session.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual("Teste", loaded.title)
        self.assertEqual(2, len(loaded.messages))

        sessions = self.store.list_sessions()
        self.assertEqual([session.id], [s.id for s in sessions])

        self.assertTrue(self.store.delete_session(session.id))
        self.assertIsNone(self.store.load_session(session.id))

    def test_facts_dedupe_and_clear(self):
        self.assertTrue(self.store.remember("usuário prefere pytest"))
        self.assertFalse(self.store.remember("usuário prefere pytest"))
        self.assertEqual(1, len(self.store.facts()))
        self.assertEqual(1, self.store.forget_all())
        self.assertEqual([], self.store.facts())

    def test_project_info(self):
        info = WorkspaceInfo(root=Path("/tmp"), markers={"package.json": "x"},
                             languages=["TypeScript"], is_git_repo=True)
        self.store.update_project_info(info)
        payload = self.store.project_info()
        self.assertEqual(["TypeScript"], payload["stack"])
        self.assertIn("package.json", payload["markers"])


class FakePlannerLLM:
    def __init__(self, content: str) -> None:
        self.content = content

    def chat(self, messages, tools=None, temperature=0.2,
             on_chunk=None, cancel_check=None):  # noqa: ANN001
        return LLMResponse(content=self.content)


class PlannerTests(unittest.TestCase):
    def test_needs_planning_heuristic(self):
        self.assertFalse(needs_planning("liste os arquivos"))
        self.assertTrue(needs_planning(
            "crie um sistema completo de autenticação com JWT e testes"))

    def test_parse_json_plan(self):
        plan = Planner.parse_plan(
            '```json\n{"plan": ["Analisar projeto", '
            '"Criar modelos", "Executar testes"]}\n```')
        self.assertEqual(3, len(plan))
        self.assertEqual("Analisar projeto", plan[0])

    def test_parse_numbered_lines_fallback(self):
        plan = Planner.parse_plan("1. Ler arquivos\n2. Escrever código")
        self.assertEqual(["Ler arquivos", "Escrever código"], plan)

    def test_parse_garbage_returns_empty(self):
        self.assertEqual([], Planner.parse_plan("desculpe, não sei"))


class SecurityAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.root / name).write_text(content, encoding="utf-8")

    def test_detects_aws_key(self):
        self._write("config.py", 'KEY = "AKIAABCDEFGHIJKLMNOP"\n')
        findings = SecurityAnalyzer(self.root).scan()
        rules = {finding.rule for finding in findings}
        self.assertIn("aws_access_key", rules)

    def test_ignores_placeholders_and_env_indirection(self):
        self._write("settings.py",
                    "PASSWORD = os.environ['PASSWORD']\n"
                    "SECRET = 'change_me_placeholder'\n")
        findings = SecurityAnalyzer(self.root).scan()
        self.assertEqual([], findings)

    def test_skips_node_modules_and_binaries(self):
        node = self.root / "node_modules" / "pkg"
        node.mkdir(parents=True)
        (node / "index.js").write_text('k="AKIAIOSFODNN7EXAMPLE"',
                                       encoding="utf-8")
        (self.root / "blob.bin").write_bytes(b"\x00AKIAIOSFODNN7EXAMPLE\x00")
        self.assertEqual([], SecurityAnalyzer(self.root).scan())


if __name__ == "__main__":
    unittest.main()
