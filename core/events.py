"""Event Bus do PudimAI.

O Agent Core emite eventos; as interfaces (CLI hoje, WebSocket no futuro)
apenas assinam. Assim Terminal e Website observam exatamente o mesmo
fluxo de execução, sem lógica duplicada.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("pudimai.events")


class EventType(str, Enum):
    """Eventos emitidos pelo Agent Core e pelas ferramentas."""

    AGENT_STARTED = "agent.started"
    AGENT_THINKING = "agent.thinking"
    AGENT_PLANNING = "agent.planning"
    PLAN_CREATED = "agent.plan_created"
    AGENT_ITERATION = "agent.iteration"
    LLM_CHUNK = "llm.chunk"

    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_DENIED = "tool.denied"
    PERMISSION_REQUIRED = "permission.required"

    FILE_READ = "file.read"
    FILE_CHANGED = "file.changed"
    TERMINAL_STARTED = "terminal.started"
    TERMINAL_COMPLETED = "terminal.completed"

    AGENT_ERROR = "agent.error"
    AGENT_COMPLETED = "agent.completed"
    TASK_CANCELLED = "task.cancelled"


@dataclass
class Event:
    """Um evento único publicado no barramento."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:  # útil para logging
        return f"{self.type.value} {self.payload}"


Callback = Callable[[Event], None]


class EventBus:
    """Barramento pub/sub thread-safe.

    Assinaturas com ``event_type=None`` recebem todos os eventos.
    Exceções em callbacks são capturadas para nunca derrubar o Agent.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[EventType | None, list[Callback]] = {}

    def subscribe(self, callback: Callback,
                  event_type: EventType | None = None) -> Callable[[], None]:
        """Registra um callback; retorna a função de cancelamento."""
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                subs = self._subscribers.get(event_type, [])
                if callback in subs:
                    subs.remove(callback)

        return _unsubscribe

    def publish(self, event_type: EventType, **payload: Any) -> None:
        """Publica um evento para todos os assinantes interessados."""
        event = Event(type=event_type, payload=payload)
        with self._lock:
            targets = (list(self._subscribers.get(event_type, [])) +
                       list(self._subscribers.get(None, [])))
        for callback in targets:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - callback nunca derruba o bus
                logger.exception("Falha em assinante de evento %s",
                                 event_type.value)
