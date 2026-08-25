"""Memória modular do PudimAI.

Layout em <workspace>/.pudimai/:
    config.json   -> Settings (gerido por config.settings)
    memory.json   -> fatos e preferências de longo prazo
    project.json  -> metadados da última varredura do projeto
    sessions/     -> conversas persistidas (JSON)
    logs/         -> log rotativo estruturado

IMPLEMENTADO AGORA : Conversation Memory + Project Memory + fatos.
PREPARADO PARA O FUTURO: backend SQLite, embeddings, RAG e memória
                     semântica — basta trocar os métodos de IO desta classe.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.workspace import WorkspaceInfo

logger = logging.getLogger("pudimai.memory")

Message = dict  # {"role": ..., "content": ..., ...}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Session:
    """Uma conversa (histórico completo de mensagens + metadados)."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = "Nova conversa"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    model: str = ""
    workspace: str = ""
    messages: list[Message] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # ex.: {"last_task": {...}}

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Session":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            title=data.get("title", "Nova conversa"),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
            model=data.get("model", ""),
            workspace=data.get("workspace", ""),
            messages=list(data.get("messages", [])),
            meta=dict(data.get("meta", {}) or {}),
        )


class MemoryStore:
    """Persistência JSON simples e à prova de corrupção."""

    def __init__(self, storage_dir: Path) -> None:
        self.dir = Path(storage_dir)
        self.sessions_dir = self.dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Fatos / preferências (longo prazo simplificado)
    # ------------------------------------------------------------------ #
    def _memory_file(self) -> Path:
        return self.dir / "memory.json"

    def _load_memory(self) -> dict:
        return self._read_json(self._memory_file(), default={"facts": []})

    def facts(self) -> list[str]:
        return list(self._load_memory().get("facts", []))

    def remember(self, fact: str) -> bool:
        fact = fact.strip()
        if not fact:
            return False
        memory = self._load_memory()
        facts = memory.setdefault("facts", [])
        if fact in facts:
            return False
        facts.append(fact)
        self._write_json(self._memory_file(), memory)
        return True

    def forget_all(self) -> int:
        memory = self._load_memory()
        removed = len(memory.get("facts", []))
        self._write_json(self._memory_file(), {"facts": []})
        return removed

    # ------------------------------------------------------------------ #
    # Metadados do projeto
    # ------------------------------------------------------------------ #
    def update_project_info(self, info: WorkspaceInfo) -> None:
        payload = {
            "root": str(info.root),
            "stack": info.languages,
            "markers": sorted(info.markers),
            "is_git_repo": info.is_git_repo,
            "updated_at": _now_iso(),
        }
        self._write_json(self.dir / "project.json", payload)

    def project_info(self) -> dict:
        return self._read_json(self.dir / "project.json", default={})

    # ------------------------------------------------------------------ #
    # Sessões
    # ------------------------------------------------------------------ #
    def save_session(self, session: Session) -> None:
        session.touch()
        path = self.sessions_dir / f"{session.id}.json"
        self._write_json(path, session.to_json())

    def load_session(self, session_id: str) -> Session | None:
        path = self.sessions_dir / f"{session_id}.json"
        data = self._read_json(path, default=None)
        if not isinstance(data, dict):
            return None
        try:
            return Session.from_json(data)
        except Exception:  # noqa: BLE001 - arquivo corrompido não derruba app
            logger.exception("Sessão corrompida: %s", path)
            return None

    def list_sessions(self) -> list[Session]:
        sessions: list[Session] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            data = self._read_json(path, default=None)
            if isinstance(data, dict):
                try:
                    sessions.append(Session.from_json(data))
                except Exception:  # noqa: BLE001
                    continue
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)

    def delete_session(self, session_id: str) -> bool:
        path = self.sessions_dir / f"{session_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------------ #
    # IO protegido
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_json(path: Path, default):
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Arquivo de memória inválido, resetando: %s", path)
            backup = path.with_suffix(".corrupt")
            try:
                path.replace(backup)
            except OSError:
                pass
            return default

    @staticmethod
    def _write_json(path: Path, payload) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.exception("Não foi possível gravar %s", path)
