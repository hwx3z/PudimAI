"""Session Manager do PudimAI.

Camada única de conversas usada por TODAS as interfaces (CLI, Web, futuras).
Delega persistência ao MemoryStore; quando o backend migrar de JSON para
SQLite/Postgres, só esta família de classes muda.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.memory import MemoryStore, Message, Session

logger = logging.getLogger("pudimai.sessions")


@dataclass
class TaskRecord:
    """Metadados do último resultado de tarefa de uma sessão."""

    completed: bool = False
    cancelled: bool = False
    iterations: int = 0
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "cancelled": self.cancelled,
            "iterations": self.iterations,
            "tools_used": self.tools_used,
            "error": self.error,
        }


class SessionManager:
    """Criação, recuperação e ciclo de vida de sessões/conversas."""

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    # ------------------------------------------------------------------ #
    def create(self, title: str = "", model: str = "",
               workspace: str = "") -> Session:
        session = Session(title=title or "Nova conversa", model=model,
                          workspace=workspace)
        self.save(session)
        return session

    def get(self, session_id: str) -> Session | None:
        return self._memory.load_session(session_id)

    def save(self, session: Session) -> None:
        self._memory.save_session(session)

    def list(self, limit: int = 50) -> list[Session]:
        return self._memory.list_sessions()[:limit]

    def delete(self, session_id: str) -> bool:
        return self._memory.delete_session(session_id)

    # ------------------------------------------------------------------ #
    # Metadados de execução (consumidos pela Web UI)
    # ------------------------------------------------------------------ #
    @staticmethod
    def record_result(session: Session, result) -> None:  # noqa: ANN001
        """Anexa o resultado da última tarefa aos metadados da sessão."""
        session.meta["last_task"] = TaskRecord(
            completed=result.completed,
            cancelled=result.cancelled,
            iterations=result.iterations,
            tools_used=result.tools_used[-20:],
            error=result.error,
        ).to_dict()

    @staticmethod
    def visible_messages(session: Session) -> list[Message]:
        """Mensagens prontas para exibição (user/assistant apenas)."""
        return [message for message in session.messages
                if message.get("role") in ("user", "assistant")
                and (message.get("content") or "").strip()]
