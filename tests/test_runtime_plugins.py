"""Testes do Runtime, SessionManager, plugins e abstração MCP."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.events import EventBus, EventType
from core.mcp import MCPManager, MCPServerConfig
from core.memory import MemoryStore
from core.plugins import PluginManager
from core.runtime import PudimAIRuntime
from core.sessions import SessionManager
from tests.fakes import FakeLLM


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = PudimAIRuntime(self.root,
                                      llm_override=FakeLLM())

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_core_wiring(self):
        self.assertIs(self.runtime.default_agent.registry,
                      self.runtime.registry)
        # plugin empacotado "hello" deve registrar tool com prefixo
        self.assertIn("hello__dizer_ola", self.runtime.registry.names())
        self.assertIsInstance(self.runtime.model_router.resolve(), str)

    def test_model_router_roles(self):
        self.assertEqual("qwen2.5:7b",
                         self.runtime.model_router.resolve("reasoning"))
        self.runtime.settings.model_roles = {"fast": "qwen2.5:3b"}
        self.assertEqual("qwen2.5:3b", self.runtime.model_router.resolve("fast"))

    def test_scoped_agent_publishes_tagged_events(self):
        events = []
        unsubscribe = self.runtime.bus.subscribe(events.append)
        from web.backend.service import ScopedBus
        scoped = ScopedBus(self.runtime.bus, {"chat_id": "c1",
                                              "task_id": "t1"})
        agent = self.runtime.build_agent(scoped)
        self.runtime.settings.max_iterations = 2
        agent.llm = FakeLLM()
        outcome = agent.run("olá")
        unsubscribe()
        tagged = [e for e in events
                  if e.type is EventType.AGENT_STARTED]
        self.assertTrue(tagged)
        self.assertEqual("c1", tagged[0].payload["chat_id"])
        self.assertEqual("t1", tagged[0].payload["task_id"])
        self.assertTrue(outcome.result.completed)


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.manager = SessionManager(MemoryStore(Path(self._tmp.name)))

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_list_delete_roundtrip(self):
        session = self.manager.create(title="Trabalho", model="m",
                                      workspace="/tmp")
        listed = self.manager.list()
        self.assertEqual([session.id], [s.id for s in listed])
        self.assertTrue(self.manager.delete(session.id))
        self.assertEqual([], self.manager.list())

    def test_visible_messages_filters_tool_noise(self):
        session = self.manager.create()
        session.messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "", "tool_calls": [1]},
            {"role": "tool", "content": "saida"},
            {"role": "assistant", "content": "pronto"},
        ]
        visible = self.manager.visible_messages(session)
        self.assertEqual([("user", "oi"), ("assistant", "pronto")],
                         [(m["role"], m["content"]) for m in visible])

    def test_record_result_metadata(self):
        from core.agent import AgentResult
        session = self.manager.create()
        SessionManager.record_result(session, AgentResult(
            text="ok", iterations=4, tools_used=["ler_arquivo"]))
        meta = session.meta["last_task"]
        self.assertTrue(meta["completed"])
        self.assertEqual(4, meta["iterations"])


class PluginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_plugin(self, name: str, manifest: dict, body: str) -> None:
        directory = self.root / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(
            __import__("json").dumps(manifest), encoding="utf-8")
        (directory / "plugin.py").write_text(body, encoding="utf-8")

    def test_load_valid_plugin_registers_prefixed_tool(self):
        self._write_plugin("meu", {
            "name": "meu", "version": "1.0", "entrypoint": "plugin.py"
        }, "def register(api):\n"
           "    api.register_tool('ping', 'responde ping', [],"
           " lambda ctx: None)\n")
        manager = PluginManager.__new__(PluginManager)
        from tools.base import ToolRegistry
        registry = ToolRegistry()
        PluginManager.__init__(manager, registry, EventBus(),
                               extra_dirs=[self.root])
        manager.load_all()
        self.assertIn("meu__ping", registry.names())
        self.assertEqual([], [n for n, _ in manager.failed])

    def test_broken_plugin_does_not_break_loader(self):
        self._write_plugin("quebrado", {
            "name": "quebrado", "entrypoint": "plugin.py"
        }, "raise RuntimeError('boom no import')\n")
        from tools.base import ToolRegistry
        registry = ToolRegistry()
        manager = PluginManager(registry, EventBus(),
                                extra_dirs=[self.root])
        manager.load_all()  # inclui também o plugin empacotado "hello"
        # o plugin quebrado NÃO registrou nada e não derrubou os demais
        self.assertNotIn("quebrado__x", registry.names())
        self.assertIn("hello__dizer_ola", registry.names())
        self.assertEqual(1, len(manager.failed))
        self.assertIn("boom", manager.failed[0][1])
        self.assertTrue(all(name.startswith("hello")
                            for name in registry.names()))


class MCPAbstractionTests(unittest.TestCase):
    def test_config_validation_and_inventory(self):
        manager = MCPManager()
        config = MCPServerConfig(name="fs", transport="stdio",
                                 command="npx @modelcontextprotocol/fs",
                                 enabled=False)
        manager.register_config(config)
        with self.assertRaises(ValueError):
            manager.register_config(MCPServerConfig(
                name="x", transport="telepatia"))
        self.assertEqual([config], manager.configs())
        self.assertEqual([], manager.enabled())

    def test_bridge_tools_to_schemas_namespacing(self):
        schemas = MCPManager.bridge_tools_to_schemas([
            {"name": "read_file", "description": "lê",
             "inputSchema": {"type": "object"}},
        ], server_name="fs")
        self.assertEqual("mcp__fs__read_file",
                         schemas[0]["function"]["name"])
        self.assertIn("[mcp:fs]", schemas[0]["function"]["description"])


if __name__ == "__main__":
    unittest.main()
