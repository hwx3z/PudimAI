"""Testes da API Web (REST + WebSocket) sobre o mesmo Agent Core.

Usa fastapi TestClient com LLM falso — sem Ollama, sem rede.
A suíte é pulada automaticamente se fastapi não estiver instalado.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    _HAS_FASTAPI = False

from core.llm import LLMResponse, ToolCall
from core.runtime import PudimAIRuntime
from tests.fakes import FakeLLM


def _make_client(root: Path, script=None,
                 llm=None) -> tuple[TestClient, PudimAIRuntime, str]:
    """Cria app + cliente já autenticado; retorna (client, runtime, token)."""
    runtime = PudimAIRuntime(root, llm_override=llm or FakeLLM(script))
    from web.backend.app import create_app
    client = TestClient(create_app(runtime))
    client.post("/api/auth/register",
                json={"username": "tester", "password": "secret123"})
    result = client.post("/api/auth/login",
                         json={"username": "tester",
                               "password": "secret123"}).json()
    client.headers.update({"Authorization":
                           f"Bearer {result['token']}"})
    return client, runtime, result["token"]


@unittest.skipUnless(_HAS_FASTAPI, "fastapi não instalado")
class WebApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.client, self.runtime, self.token = _make_client(self.root, [
            LLMResponse(tool_calls=[ToolCall(
                name="escrever_arquivo",
                arguments={"caminho": "gerado.txt",
                           "conteudo": "web\n"})]),
            LLMResponse(content="Arquivo `gerado.txt` criado."),
        ])
        self.service = self.client.app.state.task_service

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    def test_status_endpoint(self):
        response = self.client.get("/api/status")
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("0.4.0", data["version"])
        self.assertEqual("tester", data["user"])
        self.assertIn("ler_arquivo", data["tools"])
        self.assertIn("hello__dizer_ola", data["tools"])

    def test_auth_gate_and_lifecycle(self):
        fresh_root = self.root / "auth-proj"
        fresh_root.mkdir()
        runtime_a = PudimAIRuntime(fresh_root,
                                   llm_override=FakeLLM())
        from web.backend.app import create_app
        client_a = TestClient(create_app(runtime_a))

        # sem token => 401 em rota protegida
        self.assertEqual(401, client_a.get("/api/status").status_code)
        # validação de credenciais fracas (pydantic 422 OU auth 400)
        weak = client_a.post("/api/auth/register",
                             json={"username": "ab",
                                   "password": "123"})
        self.assertIn(weak.status_code, (400, 422))
        # registro -> auto-login com token válido
        reg = client_a.post("/api/auth/register",
                            json={"username": "ana",
                                  "password": "segredo123"})
        self.assertEqual(201, reg.status_code)
        token = reg.json()["token"]
        ok = client_a.get("/api/status",
                          headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(200, ok.status_code)
        self.assertEqual("ana", ok.json()["user"])
        # senha errada => 401; duplicado => 400
        bad = client_a.post("/api/auth/login",
                            json={"username": "ana",
                                  "password": "erradíssima"})
        self.assertEqual(401, bad.status_code)
        dup = client_a.post("/api/auth/register",
                            json={"username": "ana",
                                  "password": "outra456"})
        self.assertEqual(400, dup.status_code)
        # logout revoga o token
        client_a.post("/api/auth/logout",
                      headers={"Authorization": f"Bearer {token}"})
        revoked = client_a.get("/api/status",
                               headers={"Authorization":
                                        f"Bearer {token}"})
        self.assertEqual(401, revoked.status_code)

    def test_rename_chat(self):
        chat = self.client.post("/api/chats", json={}).json()
        patched = self.client.patch(
            f"/api/chats/{chat['id']}",
            json={"title": "Projeto Pudim"}).json()
        self.assertEqual("Projeto Pudim", patched["title"])
        detail = self.client.get(f"/api/chats/{chat['id']}").json()
        self.assertEqual("Projeto Pudim", detail["title"])

    # ------------------------------------------------------------------ #
    # Computador da IA: fs tree + editor bruto
    # ------------------------------------------------------------------ #
    def test_fs_tree_and_raw_editor(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('oi')\n",
                                                  encoding="utf-8")
        tree = self.client.get("/api/fs/tree").json()
        names = {entry["name"]: entry for entry in tree["entries"]}
        self.assertIn("src", names)
        self.assertEqual("dir", names["src"]["type"])

        sub = self.client.get("/api/fs/tree",
                              params={"path": "src"}).json()
        self.assertIn("app.py", {e["name"] for e in sub["entries"]})

        # leitura bruta para o editor
        raw = self.client.get("/api/file/raw",
                              params={"path": "src/app.py"})
        self.assertEqual(200, raw.status_code)
        self.assertIn("print('oi')", raw.json()["content"])

        # escrita via editor cria/salva dentro do workspace
        saved = self.client.put("/api/file/raw", json={
            "path": "src/app.py", "content": "print('editado')\n"})
        self.assertTrue(saved.json()["saved"])
        self.assertEqual("print('editado')\n",
                         (self.root / "src" / "app.py").read_text(
                             encoding="utf-8"))

        # path traversal continua bloqueado
        evil = self.client.put("/api/file/raw", json={
            "path": "../fora.txt", "content": "x"})
        self.assertEqual(400, evil.status_code)

    def test_terminal_disabled_by_flag(self):
        from config.settings import Settings
        term_root = self.root / "term-off"
        term_root.mkdir()
        settings = Settings(workspace=str(term_root))
        settings.web_terminal = False
        assert settings.web_terminal is False
        runtime_off = PudimAIRuntime(term_root, settings=settings,
                                     llm_override=FakeLLM())
        assert runtime_off.settings.web_terminal is False
        from web.backend.app import create_app
        app_off = create_app(runtime_off)
        assert app_off.state.auth is not None
        client_off = TestClient(app_off)
        client_off.post("/api/auth/register",
                        json={"username": "tester",
                              "password": "secret123"})
        login = client_off.post("/api/auth/login",
                                json={"username": "tester",
                                      "password": "secret123"}).json()
        with client_off.websocket_connect(
                f"/ws/term?token={login['token']}") as ws:
            message = ws.receive_json()
            self.assertEqual("ws.error", message["type"])
            self.assertIn("desativado", message["payload"]["reason"])

    @unittest.skipUnless(hasattr(__import__("os"), "fork"),
                         "PTY requer POSIX")
    def test_terminal_ws_echo(self):
        chat_root = self.root / "term-on"
        chat_root.mkdir()
        client_term, _rt, token = _make_client(chat_root)
        with client_term.websocket_connect(
                f"/ws/term?token={token}") as ws:
            ready = ws.receive_json()
            self.assertEqual("ws.ready", ready["type"])
            ws.send_text(json.dumps(
                {"type": "input", "data": "echo pudim_term_$((40+2))\n"}))
            deadline = time.time() + 15
            collected = ""
            while time.time() < deadline:
                message = ws.receive_json()
                if message["type"] == "output":
                    collected += message["data"]
                    if "pudim_term_42" in collected:
                        break
                if message["type"] == "ws.closed":
                    break
            self.assertIn("pudim_term_42", collected)

    def test_workspace_and_files_endpoints(self):
        summary = self.client.get("/api/workspace").json()
        self.assertIn("WORKSPACE", summary["summary"])
        listing = self.client.get("/api/files").json()
        self.assertIn("listing", listing)
        bad = self.client.get("/api/file", params={"path": "../fora"})
        self.assertEqual(400, bad.status_code)

    def test_chat_lifecycle_and_task_execution(self):
        chat = self.client.post("/api/chats",
                                json={"title": "Teste"}).json()

        done = self.client.post(
            f"/api/chats/{chat['id']}/tasks",
            json={"task": "crie gerado.txt", "wait": True}).json()
        self.assertFalse(done["running"])
        self.assertTrue(done["result"]["completed"])
        self.assertTrue(done["result"]["text"].find("criado") >= 0)

        # efeito real no disco — mesma tool que a CLI usa
        self.assertTrue((self.root / "gerado.txt").exists())

        detail = self.client.get(f"/api/chats/{chat['id']}").json()
        roles = [message["role"] for message in detail["messages"]]
        self.assertEqual(["user", "assistant"], roles)
        self.assertFalse(detail["running"])

    def test_duplicate_task_on_same_chat_conflicts(self):
        class BlockingLLM(FakeLLM):
            def __init__(self):
                super().__init__()
                self.gate = threading.Event()
                self.entered = threading.Event()

            def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.entered.set()
                self.gate.wait(timeout=15)
                return LLMResponse(content="devagar")

        blocking = BlockingLLM()
        client_slow, _runtime_slow, _tok = _make_client(
            self.root / "slow-proj", llm=blocking)
        (self.root / "slow-proj").mkdir(exist_ok=True)

        chat = client_slow.post("/api/chats", json={}).json()
        handle, started = client_slow.app.state.task_service.start_task(
            chat["id"], "tarefa lenta")
        self.assertTrue(started)
        try:
            self.assertTrue(blocking.entered.wait(timeout=10))
            conflict = client_slow.post(f"/api/chats/{chat['id']}/tasks",
                                        json={"task": "segunda"})
            self.assertEqual(409, conflict.status_code)
        finally:
            blocking.gate.set()
            handle.thread.join(timeout=15)

    def test_cancel_endpoint_requests_cooperative_stop(self):
        class HangingLLM(FakeLLM):
            def __init__(self):
                super().__init__()
                self.released = threading.Event()

            def chat(self, messages, tools=None, temperature=0.2,
                     on_chunk=None, cancel_check=None):
                for _ in range(100):
                    if cancel_check and cancel_check():
                        raise RuntimeError("cancelado")
                    time.sleep(0.05)
                return LLMResponse(content="nunca")

        hanging = HangingLLM()
        client_h, _rt, _tok = _make_client(self.root / "hang-proj",
                                           llm=hanging)
        (self.root / "hang-proj").mkdir(exist_ok=True)
        chat = client_h.post("/api/chats", json={}).json()
        started = client_h.post(f"/api/chats/{chat['id']}/tasks",
                                json={"task": "trava"}).json()
        self.assertEqual(200, client_h.post(
            f"/api/tasks/{started['task_id']}/cancel").status_code)

        deadline = time.time() + 10
        while time.time() < deadline:
            status = client_h.get(
                f"/api/tasks/{started['task_id']}").json()
            if not status["running"]:
                break
            time.sleep(0.1)
        self.assertFalse(status["running"])

    def test_delete_chat(self):
        chat = self.client.post("/api/chats", json={}).json()
        self.assertEqual(200, self.client.delete(
            f"/api/chats/{chat['id']}").status_code)
        self.assertEqual(404, self.client.get(
            f"/api/chats/{chat['id']}").status_code)

    def test_permission_ask_denied_without_human(self):
        """ASK sem humano => negado por timeout curto + evento emitido."""
        from config.settings import Settings
        from core.llm import LLMResponse
        ask_root = self.root / "ask-proj"
        ask_root.mkdir(exist_ok=True)
        settings = Settings(workspace=str(ask_root))
        settings.permission_timeout = 1  # não travar a suíte
        runtime_ask = PudimAIRuntime(ask_root, settings=settings,
                                     llm_override=FakeLLM([
                                         LLMResponse(tool_calls=[ToolCall(
                                             name="executar_terminal",
                                             arguments={"comando":
                                                        "npm install x"})]),
                                     ]))
        events: list = []
        runtime_ask.bus.subscribe(events.append,
                                  EventType.PERMISSION_REQUIRED)
        from web.backend.app import create_app
        client_ask = TestClient(create_app(runtime_ask))
        client_ask.post("/api/auth/register",
                        json={"username": "tester",
                              "password": "secret123"})
        login = client_ask.post("/api/auth/login",
                                json={"username": "tester",
                                      "password": "secret123"}).json()
        client_ask.headers.update({"Authorization":
                                   f"Bearer {login['token']}"})
        chat = client_ask.post("/api/chats", json={}).json()
        done = client_ask.post(f"/api/chats/{chat['id']}/tasks",
                               json={"task": "instale x",
                                     "wait": True}).json()
        self.assertFalse(done["running"])
        self.assertTrue(any(e.type is EventType.PERMISSION_REQUIRED
                            for e in events))

    def test_websocket_streams_tagged_task_events(self):
        runtime_ws = PudimAIRuntime(self.root / "ws-proj", llm_override=(
            FakeLLM([
                LLMResponse(tool_calls=[ToolCall(
                    name="escrever_arquivo",
                    arguments={"caminho": "a.txt", "conteudo": "x"})]),
                LLMResponse(content="pronto!"),
            ])))
        (self.root / "ws-proj").mkdir(exist_ok=True)
        from web.backend.app import create_app
        client_ws = TestClient(create_app(runtime_ws))
        client_ws.post("/api/auth/register",
                       json={"username": "tester",
                             "password": "secret123"})
        login = client_ws.post("/api/auth/login",
                               json={"username": "tester",
                                     "password": "secret123"}).json()
        token = login["token"]
        client_ws.headers.update({"Authorization": f"Bearer {token}"})
        chat = client_ws.post("/api/chats", json={}).json()

        # token inválido é rejeitado no handshake do WebSocket
        with client_ws.websocket_connect(
                f"/ws/{chat['id']}?token=invalido") as ws:
            rejected = ws.receive_json()
            self.assertEqual("ws.unauthorized", rejected["type"])

        seen: set[str] = set()
        foreign: list[dict] = []
        with client_ws.websocket_connect(
                f"/ws/{chat['id']}?token={token}") as ws:
            ready = ws.receive_json()
            self.assertEqual("ws.ready", ready["type"])
            client_ws.post(f"/api/chats/{chat['id']}/tasks",
                           json={"task": "faça", "wait": False})
            deadline = time.time() + 20
            while time.time() < deadline:
                envelope = ws.receive_json()
                seen.add(envelope["type"])
                payload = envelope.get("payload") or {}
                if payload.get("chat_id") and \
                        payload["chat_id"] != chat["id"]:
                    foreign.append(envelope)
                if envelope["type"] == "agent.completed":
                    break

        self.assertIn("agent.started", seen)
        self.assertIn("tool.started", seen)
        self.assertIn("tool.completed", seen)
        self.assertIn("agent.completed", seen)
        self.assertEqual([], foreign)


# import tardio para o skip funcionar em ambientes sem fastapi
from core.events import EventType  # noqa: E402

if __name__ == "__main__":
    unittest.main()
