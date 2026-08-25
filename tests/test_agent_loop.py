"""Testes da tool de terminal + integração do Agent Loop com LLM falso.

O FakeLLM devolve respostas roteirizadas, permitindo testar o ciclo
ReAct (planejar -> agir -> observar -> corrigir -> responder) sem rede.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from core.agent import Agent
from core.context import ContextManager
from core.events import Event, EventBus, EventType
from core.llm import LLMResponse, ToolCall
from core.memory import Message
from core.workspace import Workspace
from tools.base import ToolContext, ToolRegistry
from tools.filesystem import register as register_fs
from tools.registry import build_default_registry
from tools.terminal import register as register_terminal


class FakeLLM:
    """Provedor determinístico que segue um roteiro de respostas."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[Message]] = []

    def chat(self, messages, tools=None, temperature=0.2,
             on_chunk=None, cancel_check=None):  # noqa: ANN001
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            return LLMResponse(content="roteiro esgotado")
        response = self.script.pop(0)
        if on_chunk and response.content:
            on_chunk(response.content)
        return response


class TerminalToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = ToolContext(workspace=self.root, settings=Settings(),
                               bus=EventBus(), confirm_callback=None)
        self.registry = build_default_registry()

    def tearDown(self):
        self._tmp.cleanup()

    def test_successful_command(self):
        result = self.registry.execute("executar_terminal",
                                       {"comando": "echo pudim"}, self.ctx)
        self.assertTrue(result.success, result.output)
        self.assertIn("pudim", result.output)
        self.assertIn("EXIT CODE:\n0", result.output)

    def test_failing_command_reports_stderr(self):
        result = self.registry.execute(
            "executar_terminal", {"comando": "ls /definitivamente/inexistente"},
            self.ctx)
        self.assertFalse(result.success)
        self.assertIn("STDERR:", result.output)

    def test_deny_without_confirmation(self):
        result = self.registry.execute("executar_terminal",
                                       {"comando": "rm -rf /"}, self.ctx)
        self.assertFalse(result.success)
        self.assertIn("BLOQUEADO", result.output)


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bus = EventBus()
        self.settings = Settings(workspace=str(self.root))
        workspace = Workspace(self.root)
        registry = ToolRegistry()
        register_fs(registry)
        register_terminal(registry)
        self.ctx = ToolContext(workspace=self.root, settings=self.settings,
                               bus=self.bus, confirm_callback=None)
        from core.planner import Planner
        self.agent = Agent(
            settings=self.settings,
            llm=None,  # definido por teste
            registry=registry,
            context=ContextManager(self.settings, workspace),
            planner=Planner(FakeLLM([]), workspace),
            bus=self.bus,
            tool_ctx=self.ctx,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _attach(self, llm: FakeLLM) -> None:
        self.agent.llm = llm

    def test_agent_writes_file_and_finishes(self):
        llm = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(
                name="escrever_arquivo",
                arguments={"caminho": "hello.py",
                           "conteudo": "print('olá do pudim')\n"})]),
            LLMResponse(content="Arquivo `hello.py` criado com sucesso."),
        ])
        self._attach(llm)

        outcome = self.agent.run("crie um hello.py")
        self.assertTrue(outcome.result.completed)
        self.assertIn("criado", outcome.result.text)
        self.assertEqual(2, outcome.result.iterations)
        self.assertEqual(["escrever_arquivo"], outcome.result.tools_used)
        self.assertEqual("print('olá do pudim')\n",
                         (self.root / "hello.py").read_text(encoding="utf-8"))

        # histórico deve conter user -> assistant(tool_call) -> tool -> assistant
        roles = [m["role"] for m in outcome.messages]
        self.assertEqual(["system", "user", "assistant", "tool", "assistant"],
                         roles)

    def test_auto_correction_after_failure(self):
        llm = FakeLLM([
            # 1ª tentativa: executa comando que falha
            LLMResponse(tool_calls=[ToolCall(
                name="executar_terminal",
                arguments={"comando": "python3 arquivo_inexistente.py"})]),
            # observa o erro e corrige criando o arquivo
            LLMResponse(tool_calls=[ToolCall(
                name="escrever_arquivo",
                arguments={"caminho": "arquivo_inexistente.py",
                           "conteudo": "x = 1\n"})]),
            # valida executando novamente
            LLMResponse(tool_calls=[ToolCall(
                name="executar_terminal",
                arguments={"comando": "python3 arquivo_inexistente.py"})]),
            LLMResponse(content="Corrigido e validado."),
        ])
        self._attach(llm)

        outcome = self.agent.run("rode o script e corrija erros")
        self.assertTrue(outcome.result.completed)
        self.assertEqual(4, outcome.result.iterations)
        self.assertTrue((self.root / "arquivo_inexistente.py").exists())

    def test_max_iterations_forces_summary(self):
        endless = LLMResponse(
            tool_calls=[ToolCall(name="listar_diretorio", arguments={})])
        llm = FakeLLM([endless] * 50)
        self._attach(llm)
        self.agent.settings.max_iterations = 3

        outcome = self.agent.run("liste tudo repetidamente")
        self.assertFalse(outcome.result.completed)
        self.assertGreaterEqual(outcome.result.iterations, 3)
        self.assertIn("Limite de 3 iterações", outcome.result.error or "")

    def test_cancellation(self):
        class SelfCancellingLLM(FakeLLM):
            """Cancela o agente de dentro da geração (como um Ctrl+C)."""

            def chat(self, messages, tools=None, temperature=0.2,
                     on_chunk=None, cancel_check=None):
                self.agent_ref.cancel()
                return LLMResponse(content="resposta parcial")

        self.agent.llm = SelfCancellingLLM([])
        self.agent.llm.agent_ref = self.agent
        outcome = self.agent.run("qualquer coisa")
        self.assertTrue(outcome.result.cancelled)
        self.assertFalse(outcome.result.completed)

    def test_unknown_tool_returns_structured_error(self):
        llm = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(name="tool_inexistente",
                                             arguments={})]),
            LLMResponse(content="Entendi o erro; encerrando."),
        ])
        self._attach(llm)
        events: list[Event] = []
        unsubscribe = self.bus.subscribe(events.append,
                                         EventType.TOOL_FAILED)
        try:
            outcome = self.agent.run("use ferramenta inválida")
        finally:
            unsubscribe()
        self.assertTrue(outcome.result.completed)
        self.assertTrue(any(e.type is EventType.TOOL_FAILED
                            for e in events))


if __name__ == "__main__":
    unittest.main()
