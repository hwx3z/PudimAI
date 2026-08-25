"""Serviço de tarefas do backend Web.

Responsabilidades:
    * executar o Agent Core em threads (uma tarefa por conversa);
    * escopar eventos por conversa (ScopedBus injeta ``chat_id``/``task_id``);
    * expor o fluxo de permissão ASK ao navegador (pending requests +
      resolução via REST), com timeout e fail-safe para DENY.

NENHUMA lógica de agente vive aqui: este módulo apenas orquestra chamadas
ao mesmo PudimAIRuntime usado pela CLI.
"""
from __future__ import annotations

import threading
import uuid
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.agent import AgentOutcome
from core.events import EventBus, EventType
from core.exceptions import PudimAIError
from core.runtime import PudimAIRuntime
from tools.base import ToolContext


class ScopedBus(EventBus):
    """EventBus que etiqueta automaticamente os eventos de UMA tarefa."""

    def __init__(self, parent: EventBus, tags: dict[str, str]) -> None:
        super().__init__()
        self._parent = parent
        self._tags = tags

    def publish(self, event_type: EventType, **payload: Any) -> None:
        merged = {**self._tags, **payload}
        self._parent.publish(event_type, **merged)
        # assinantes locais (WS desta conversa) também recebem
        super().publish(event_type, **merged)


@dataclass
class PendingPermission:
    request_id: str
    command: str
    reason: str
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool | None = None


@dataclass
class TaskHandle:
    id: str
    chat_id: str
    thread: threading.Thread
    started_at: float = field(default_factory=time.time)
    outcome: AgentOutcome | None = None
    error: str | None = None
    _agent: Any = None
    _cancel_flag: threading.Event = field(
        default_factory=threading.Event)

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def cancel(self) -> bool:
        """Cancelamento cooperativo: sinaliza o agente em execução."""
        self._cancel_flag.set()
        if self._agent is not None:
            self._agent.cancel()
            return True
        return False


class PermissionBroker:
    """Liga pedidos ASK vindos das threads de tarefa ao mundo HTTP."""

    def __init__(self, timeout_seconds: int) -> None:
        self.timeout = timeout_seconds
        self._pending: dict[str, PendingPermission] = {}
        self._lock = threading.Lock()

    def ask(self, command: str, reason: str,
            notify=None) -> tuple[bool, str]:  # noqa: ANN001
        """Cria um pedido pendente e aguarda decisão humana."""
        request = PendingPermission(
            request_id=uuid.uuid4().hex[:12],
            command=command, reason=reason)
        with self._lock:
            self._pending[request.request_id] = request
        try:
            if notify is not None:
                notify(request)
            granted = request.event.wait(timeout=self.timeout)
            if not granted:
                return False, "timeout aguardando aprovação humana"
            if request.approved is True:
                return True, "aprovado via interface Web"
            return False, f"negado via interface Web ({request.reason})"
        finally:
            with self._lock:
                self._pending.pop(request.request_id, None)

    def resolve(self, request_id: str, approved: bool) -> bool:
        with self._lock:
            request = self._pending.get(request_id)
        if request is None:
            return False
        request.approved = bool(approved)
        request.event.set()
        return True

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending)


class TaskService:
    """Executa tarefas do Agent Core sob demanda das rotas REST."""

    def __init__(self, runtime: PudimAIRuntime) -> None:
        self.runtime = runtime
        self.tasks: dict[str, TaskHandle] = {}
        self.permissions = PermissionBroker(
            runtime.settings.permission_timeout)
        self._lock = threading.Lock()
        self._agents: dict[threading.Thread, Any] = {}

    # ------------------------------------------------------------------ #
    def start_task(self, chat_id: str, task_text: str,
                   force_plan: bool = False) -> tuple[TaskHandle, bool]:
        """Inicia uma tarefa; retorna (handle, started)."""
        with self._lock:
            busy = any(handle.running and handle.chat_id == chat_id
                       for handle in self.tasks.values())
            if busy:
                return self._last_for(chat_id), False

            tags = {"chat_id": chat_id}
            task_id = uuid.uuid4().hex[:12]
            tags["task_id"] = task_id
            scoped_bus = ScopedBus(self.runtime.bus, tags)
            confirm = self._make_confirm_callback(scoped_bus)
            tool_ctx = ToolContext(
                workspace=self.runtime.workspace.root,
                settings=self.runtime.settings,
                bus=scoped_bus,
                confirm_callback=confirm,
            )
            agent = self.runtime.build_agent(scoped_bus, tool_ctx)

            handle = TaskHandle(id=task_id, chat_id=chat_id,
                                thread=threading.Thread(
                                    target=self._run,
                                    args=(agent, chat_id, task_text,
                                          force_plan, task_id),
                                    daemon=True))
            handle._agent = agent
            self.tasks[task_id] = handle

        handle.thread.start()
        return handle, True

    # ------------------------------------------------------------------ #
    def _make_confirm_callback(self, bus: ScopedBus):
        """Callback ASK que publica permission.required e espera o humano."""

        def _confirm(command: str, reason: str) -> bool:
            def _notify(request: PendingPermission) -> None:
                bus.publish(EventType.PERMISSION_REQUIRED,
                            request_id=request.request_id,
                            command=request.command,
                            reason=request.reason)

            approved, _detail = self.permissions.ask(
                command, reason, notify=_notify)
            return approved

        return _confirm

    def _run(self, agent, chat_id: str, task_text: str, force_plan: bool,
             task_id: str) -> None:  # noqa: ANN001
        handle = self.tasks[task_id]
        try:
            session = self.runtime.sessions.get(chat_id)
            if session is None:
                raise PudimAIError(f"Conversa não encontrada: {chat_id}")
            prior = [m for m in session.messages
                     if m.get("role") in ("user", "assistant")]
            outcome = agent.run(task_text, prior_messages=prior,
                                force_plan=force_plan)

            session.messages = outcome.messages[-400:]
            session.model = self.runtime.settings.model
            from core.sessions import SessionManager
            SessionManager.record_result(session, outcome.result)
            if session.title == "Nova conversa":
                session.title = task_text[:48]
            self.runtime.sessions.save(session)
            handle.outcome = outcome
        except Exception as exc:  # noqa: BLE001 - vira evento, não crash
            handle.error = f"{type(exc).__name__}: {exc}"
            self.runtime.bus.publish(EventType.AGENT_ERROR,
                                     chat_id=chat_id, task_id=task_id,
                                     error=handle.error)

    def _last_for(self, chat_id: str) -> TaskHandle | None:
        candidates = [h for h in self.tasks.values() if h.chat_id == chat_id]
        return max(candidates, key=lambda h: h.started_at, default=None)

    # ------------------------------------------------------------------ #
    def get(self, task_id: str) -> TaskHandle | None:
        return self.tasks.get(task_id)

    def running_for(self, chat_id: str) -> TaskHandle | None:
        handle = self._last_for(chat_id)
        return handle if handle and handle.running else None

    def subscribe_all(self, callback) -> Callable[[], None]:  # noqa: ANN001
        """Acesso ao barramento global (WebSocket da aplicação)."""
        return self.runtime.bus.subscribe(callback)
