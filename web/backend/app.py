"""Aplicação FastAPI do PudimAI Web.

Princípios:
    * as rotas NÃO contêm lógica de agente — apenas chamam o Core;
    * eventos chegam ao navegador em tempo real via WebSocket;
    * permissões ASK são resolvidas pelo humano no navegador (REST).

Montagem: create_app(runtime) -> app; executar com uvicorn ou
`python pudim.py --web`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core.events import EventType
from core.exceptions import ModelNotFoundError, OllamaUnavailableError
from core.runtime import PudimAIRuntime
from web.backend.auth import AuthService, AuthError
from web.backend.service import TaskService
from web.backend.terminal import PTY_AVAILABLE, PtySession, default_shell

logger = logging.getLogger("pudimai.web")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
STARTED_AT = time.time()
MAX_EDITOR_BYTES = 512_000

#: Prefixos acessíveis sem login (a raiz serve apenas o shell da SPA).
PUBLIC_PATHS = ("/api/auth", "/static", "/api/docs", "/openapi.json", "/")


# ---------------------------------------------------------------------- #
# Schemas de entrada
# ---------------------------------------------------------------------- #
class CreateChat(BaseModel):
    title: str = Field(default="Nova conversa", max_length=120)


class RenameChat(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=6, max_length=200)


class TaskIn(BaseModel):
    task: str = Field(min_length=1, max_length=20000)
    force_plan: bool = False
    wait: bool = False  # útil para testes/scripting


class PermissionIn(BaseModel):
    approved: bool


class WriteFile(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    content: str = Field(max_length=2_000_000)


# ---------------------------------------------------------------------- #
def create_app(runtime: PudimAIRuntime) -> FastAPI:
    app = FastAPI(title="PudimAI", version="0.4.0",
                  docs_url="/api/docs")
    service = TaskService(runtime)
    auth = AuthService(runtime.storage_dir)
    app.state.task_service = service
    app.state.auth = auth

    # ------------------------------------------------------------------ #
    # Middleware: exige Bearer token em tudo que não é público
    # ------------------------------------------------------------------ #
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        is_public = any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in PUBLIC_PATHS)
        if not is_public:
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if \
                header.lower().startswith("bearer ") else None
            username = auth.verify(token)
            if username is None:
                return JSONResponse({"detail": "não autenticado"},
                                    status_code=401)
            request.state.user = username
        return await call_next(request)

    # ------------------------------------------------------------------ #
    # Autenticação (público)
    # ------------------------------------------------------------------ #
    @app.post("/api/auth/register", status_code=201)
    def register(body: Credentials) -> dict[str, str]:
        try:
            auth.register(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        return {"token": auth.login(body.username, body.password),
                "username": body.username.strip().lower()}

    @app.post("/api/auth/login")
    def login(body: Credentials) -> dict[str, str]:
        try:
            token = auth.login(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(401, str(exc))
        return {"token": token,
                "username": body.username.strip().lower()}

    @app.post("/api/auth/logout")
    def logout(request: Request) -> dict[str, bool]:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if \
            header.lower().startswith("bearer ") else None
        auth.revoke(token)
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _chat_or_404(chat_id: str):
        session = runtime.sessions.get(chat_id)
        if session is None:
            raise HTTPException(404, f"Conversa não encontrada: {chat_id}")
        return session

    # ------------------------------------------------------------------ #
    # Sistema
    # ------------------------------------------------------------------ #
    @app.get("/api/status")
    def status(request: Request) -> dict[str, Any]:
        models: list[str] = []
        ollama_ok = True
        try:
            models = runtime.llm.list_models()
        except (OllamaUnavailableError, ModelNotFoundError):
            ollama_ok = False
        return {
            "version": "0.4.0",
            "user": getattr(request.state, "user", None),
            "ollama_ok": ollama_ok,
            "models": models,
            "model": runtime.settings.model,
            "security_mode": runtime.settings.security_mode,
            "auto_plan": runtime.settings.auto_plan,
            "workspace": {
                "root": str(runtime.workspace.root),
                "stack": runtime.workspace.info.languages,
                "git": runtime.workspace.info.is_git_repo,
                "markers": sorted(runtime.workspace.info.markers),
            },
            "tools": runtime.registry.names(),
            "plugins": [p.name for p in runtime.plugins.loaded],
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
        }

    @app.get("/api/workspace")
    def workspace_summary() -> dict[str, Any]:
        return {"summary": runtime.workspace.summary(),
                "tree": runtime.workspace.tree()}

    # ------------------------------------------------------------------ #
    # Arquivos (somente leitura; escrita continua exclusiva do Agent)
    # ------------------------------------------------------------------ #
    @app.get("/api/files")
    def list_files(path: str = ".") -> dict[str, Any]:
        result = runtime.registry.execute("listar_diretorio",
                                          {"caminho": path},
                                          runtime.tool_ctx)
        if not result.success:
            raise HTTPException(400, result.output)
        return {"path": path, "listing": result.output}

    @app.get("/api/file")
    def read_file(path: str) -> dict[str, Any]:
        result = runtime.registry.execute("ler_arquivo",
                                          {"caminho": path},
                                          runtime.tool_ctx)
        if not result.success:
            raise HTTPException(400, result.output)
        return {"path": path, "content": result.output}

    # ------------------------------------------------------------------ #
    # Computador da IA: explorador + editor (mesma fronteira do workspace)
    # ------------------------------------------------------------------ #
    @app.get("/api/fs/tree")
    def fs_tree(path: str = ".") -> dict[str, Any]:
        from core.workspace import DEFAULT_IGNORES
        try:
            target = runtime.workspace.resolve_path(path)
        except PermissionError as exc:
            raise HTTPException(400, str(exc))
        if not target.exists():
            raise HTTPException(404, f"Caminho não existe: {path}")
        if not target.is_dir():
            raise HTTPException(400, f"'{path}' não é um diretório.")
        try:
            children = sorted(target.iterdir(),
                              key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            raise HTTPException(400, f"Erro lendo diretório: {exc}")

        entries: list[dict[str, Any]] = []
        for child in children[:500]:
            if child.name in DEFAULT_IGNORES or \
                    child.name.endswith(".egg-info"):
                continue
            is_dir = child.is_dir()
            try:
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                size = -1
            entries.append({"name": child.name,
                            "type": "dir" if is_dir else "file",
                            "size": size})
        relative = target.relative_to(runtime.workspace.root)
        display = "." if str(relative) == "." else str(relative)
        return {"path": display, "entries": entries}

    def _resolve_or_400(path: str) -> Path:
        try:
            return runtime.workspace.resolve_path(path)
        except PermissionError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/file/raw")
    def file_raw(path: str) -> dict[str, Any]:
        resolved = _resolve_or_400(path)
        if not resolved.exists() or not resolved.is_file():
            raise HTTPException(404, f"Arquivo não existe: {path}")
        size = resolved.stat().st_size
        if size > MAX_EDITOR_BYTES:
            raise HTTPException(
                400, f"Arquivo grande demais para o editor "
                     f"({size} bytes; limite {MAX_EDITOR_BYTES}).")
        raw = resolved.read_bytes()
        if b"\x00" in raw[:1024]:
            raise HTTPException(400, "Arquivo binário não editável.")
        content = raw.decode("utf-8", errors="replace")
        return {"path": path, "content": content,
                "size": len(raw), "truncated": False}

    @app.put("/api/file/raw")
    def file_raw_write(body: WriteFile) -> dict[str, Any]:
        resolved = _resolve_or_400(body.path)
        if resolved.exists() and resolved.is_dir():
            raise HTTPException(400, f"'{body.path}' é um diretório.")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(body.content, encoding="utf-8")
        except OSError as exc:
            raise HTTPException(400, f"Falha ao salvar: {exc}")
        size = len(body.content.encode("utf-8"))
        runtime.bus.publish(EventType.FILE_CHANGED,
                            path=str(resolved), bytes=size)
        return {"saved": True, "path": body.path, "bytes": size}

    # ------------------------------------------------------------------ #
    # Conversas
    # ------------------------------------------------------------------ #
    @app.get("/api/chats")
    def chats() -> list[dict[str, Any]]:
        return [{
            "id": s.id, "title": s.title, "updated_at": s.updated_at,
            "messages": len(s.messages),
        } for s in runtime.sessions.list(limit=100)]

    @app.post("/api/chats", status_code=201)
    def create_chat(body: CreateChat) -> dict[str, Any]:
        session = runtime.sessions.create(
            title=body.title or "Nova conversa",
            model=runtime.settings.model,
            workspace=str(runtime.workspace.root))
        return {"id": session.id, "title": session.title}

    @app.get("/api/chats/{chat_id}")
    def get_chat(chat_id: str) -> dict[str, Any]:
        session = _chat_or_404(chat_id)
        running = service.running_for(chat_id)
        return {
            "id": session.id, "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "messages": [
                {"role": m["role"], "content": m.get("content", "")}
                for m in runtime.sessions.visible_messages(session)
            ],
            "meta": session.meta,
            "running": bool(running),
            "task_id": running.id if running else None,
        }

    @app.patch("/api/chats/{chat_id}")
    def rename_chat(chat_id: str, body: RenameChat) -> dict[str, Any]:
        session = _chat_or_404(chat_id)
        if service.running_for(chat_id):
            raise HTTPException(409, "Conversa com tarefa em execução.")
        session.title = body.title.strip()
        runtime.sessions.save(session)
        return {"id": session.id, "title": session.title}

    @app.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: str) -> dict[str, str]:
        _chat_or_404(chat_id)
        if service.running_for(chat_id):
            raise HTTPException(409, "Conversa com tarefa em execução.")
        runtime.sessions.delete(chat_id)
        return {"deleted": chat_id}

    # ------------------------------------------------------------------ #
    # Tarefas do agente
    # ------------------------------------------------------------------ #
    @app.post("/api/chats/{chat_id}/tasks", status_code=202)
    def start_task(chat_id: str, body: TaskIn) -> dict[str, Any]:
        _chat_or_404(chat_id)
        handle, started = service.start_task(chat_id, body.task.strip(),
                                             body.force_plan)
        if not started and handle is not None:
            raise HTTPException(409, "Já existe tarefa em execução nesta "
                                     "conversa.")
        if handle is None:
            raise HTTPException(500, "Falha ao iniciar tarefa.")

        if body.wait:
            handle.thread.join(timeout=600)
        payload: dict[str, Any] = {
            "task_id": handle.id, "chat_id": chat_id,
            "running": handle.running,
        }
        if body.wait and handle.outcome is not None:
            payload["result"] = {
                "text": handle.outcome.result.text,
                "completed": handle.outcome.result.completed,
                "iterations": handle.outcome.result.iterations,
            }
        return JSONResponse(payload, status_code=(
            200 if body.wait else 202))

    @app.get("/api/tasks/{task_id}")
    def task_status(task_id: str) -> dict[str, Any]:
        handle = service.get(task_id)
        if handle is None:
            raise HTTPException(404, "Tarefa não encontrada.")
        outcome = handle.outcome
        return {
            "task_id": handle.id, "chat_id": handle.chat_id,
            "running": handle.running,
            "error": handle.error,
            "result": None if outcome is None else {
                "text": outcome.result.text,
                "completed": outcome.result.completed,
                "cancelled": outcome.result.cancelled,
                "iterations": outcome.result.iterations,
                "tools_used": outcome.result.tools_used,
            },
        }

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, Any]:
        handle = service.get(task_id)
        if handle is None:
            raise HTTPException(404, "Tarefa não encontrada.")
        requested = handle.cancel()
        return {"task_id": task_id, "cancel_requested": requested}

    # ------------------------------------------------------------------ #
    # Permissões (fluxo ASK vindo do navegador)
    # ------------------------------------------------------------------ #
    @app.get("/api/permissions/pending")
    def pending_permissions() -> dict[str, Any]:
        ids = service.permissions.pending_ids()
        return {"pending": ids}

    @app.post("/api/permissions/{request_id}")
    def resolve_permission(request_id: str, body: PermissionIn):
        ok = service.permissions.resolve(request_id, body.approved)
        if not ok:
            raise HTTPException(404, "Pedido de permissão inexistente "
                                     "ou já resolvido.")
        return {"request_id": request_id, "approved": body.approved}

    # ------------------------------------------------------------------ #
    # Computador da IA: terminal interativo (PTY real no workspace)
    # REGISTRADO ANTES de /ws/{chat_id} — rotas casam em ordem e "term"
    # seria capturado como chat_id se viesse depois.
    # ------------------------------------------------------------------ #
    @app.websocket("/ws/term")
    async def term_ws(websocket: WebSocket, token: str = "") -> None:
        username = auth.verify(token)
        await websocket.accept()
        if username is None:
            await websocket.send_json({"type": "ws.unauthorized"})
            await websocket.close(code=4401)
            return
        if not runtime.settings.web_terminal or not PTY_AVAILABLE:
            await websocket.send_json({
                "type": "ws.error",
                "payload": {"reason":
                            "terminal desativado nesta instância"
                            if not runtime.settings.web_terminal
                            else "PTY indisponível nesta plataforma"},
            })
            await websocket.close(code=4500)
            return

        shell = default_shell()
        session = PtySession(cwd=str(runtime.workspace.root), shell=shell)
        await websocket.send_json(
            {"type": "ws.ready", "payload": {"shell": shell,
                                             "cwd": str(runtime.workspace.root)}})

        loop = asyncio.get_running_loop()
        out_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1000)
        stop = threading.Event()

        def _push(item: str | None) -> None:
            try:
                loop.call_soon_threadsafe(out_queue.put_nowait, item)
            except RuntimeError:
                pass

        def _pump() -> None:
            """Thread leitora da PTY -> fila assíncrona."""
            while not stop.is_set():
                try:
                    chunk = session.read(timeout=0.25)
                except Exception:  # noqa: BLE001 - pty pode sumir
                    break
                if chunk is None:       # timeout sem dados
                    continue
                _push(chunk)
                if chunk == "":         # EOF/EIO: shell encerrou
                    break
            _push(None)
            stop.set()

        pump_thread = threading.Thread(target=_pump, daemon=True,
                                       name="pudimai-term-pump")
        pump_thread.start()

        async def _receiver() -> None:
            try:
                while True:
                    message = json.loads(await websocket.receive_text())
                    if message.get("type") == "input":
                        data = str(message.get("data", ""))
                        await loop.run_in_executor(None, session.write, data)
            except (WebSocketDisconnect, RuntimeError):
                return

        receiver = asyncio.create_task(_receiver())
        try:
            while True:
                chunk = await out_queue.get()
                if chunk is None:
                    await websocket.send_json({"type": "ws.closed"})
                    break
                await websocket.send_json({"type": "output",
                                           "data": chunk})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            stop.set()
            session.close()
            receiver.cancel()

    # ------------------------------------------------------------------ #
    # WebSocket — stream de eventos em tempo real por conversa
    # ------------------------------------------------------------------ #
    @app.websocket("/ws/{chat_id}")
    async def agent_ws(websocket: WebSocket, chat_id: str,
                       token: str = "") -> None:
        username = auth.verify(token)
        if username is None:
            await websocket.accept()
            await websocket.send_json({"type": "ws.unauthorized"})
            await websocket.close(code=4401)
            return
        await websocket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=500)

        def _on_event(event) -> None:  # noqa: ANN001
            payload_chat = event.payload.get("chat_id")
            if payload_chat is not None and payload_chat != chat_id:
                return
            envelope = {
                "type": event.type.value,
                "payload": event.payload,
                "timestamp": event.timestamp,
            }
            try:
                loop.call_soon_threadsafe(queue.put_nowait, envelope)
            except RuntimeError:
                pass  # loop encerrado durante shutdown

        unsubscribe = runtime.bus.subscribe(_on_event)
        try:
            await websocket.send_json({"type": "ws.ready",
                                       "payload": {"chat_id": chat_id}})
            receiver = asyncio.create_task(_receive(websocket, service))
            while True:
                envelope = await queue.get()
                if envelope is None:
                    break
                await websocket.send_json(envelope)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            unsubscribe()
            receiver.cancel()

    async def _receive(websocket: WebSocket, svc: TaskService) -> None:
        """Mensagens de controle do cliente: cancelamento."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "cancel":
                    task_id = message.get("task_id")
                    handle = svc.get(task_id) if task_id else \
                        svc.running_for(message.get("chat_id", ""))
                    if handle is not None:
                        handle.cancel()
        except (WebSocketDisconnect, RuntimeError):
            return

    # ------------------------------------------------------------------ #
    # Frontend estático (SPA própria, sem build step)
    # ------------------------------------------------------------------ #
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/static", _static_app(), name="static")
    return app


def _static_app():
    from fastapi.staticfiles import StaticFiles
    return StaticFiles(directory=str(FRONTEND_DIR))
