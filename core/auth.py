"""Autenticação do PudimAI (núcleo compartilhado).

UMA única base de contas por workspace (<workspace>/.pudimai/users.json),
usada simultaneamente pela interface Web e pela CLI — /login no terminal
e o login no navegador falam com o mesmo AuthService.

Escopo honesto para uma plataforma LOCAL:
    * senhas hasheadas (PBKDF2-HMAC-SHA256 + salt, 200k iterações);
    * tokens opacos em memória com expiração deslizante;
    * na Web, autenticação é porteiro obrigatório; na CLI é identidade
      opcional para auditoria (exigência configurável via
      ``cli_require_login``).

PREPARADO PARA O FUTURO: separação de dados por usuário (owner_id em
chats/workspaces) e persistência de sessões.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

PBKDF2_ITERATIONS = 200_000
MIN_USER = 3
MIN_PASS = 6


class AuthError(Exception):
    """Credenciais inválidas ou registro malformado."""


class AuthService:
    """Registro/login de usuários locais + emissão de tokens."""

    def __init__(self, storage_dir: Path,
                 ttl_seconds: int = 7 * 24 * 3600) -> None:
        self.users_file = Path(storage_dir) / "users.json"
        self.ttl = ttl_seconds
        self._tokens: dict[str, dict] = {}  # token -> {user, expires}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Usuários
    # ------------------------------------------------------------------ #
    def _load_users(self) -> dict:
        if not self.users_file.is_file():
            return {}
        try:
            return json.loads(
                self.users_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_users(self, users: dict) -> None:
        self.users_file.parent.mkdir(parents=True, exist_ok=True)
        self.users_file.write_text(
            json.dumps(users, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt), PBKDF2_ITERATIONS)
        return digest.hex()

    def user_exists(self, username: str) -> bool:
        return username.strip().lower() in self._load_users()

    # ------------------------------------------------------------------ #
    def register(self, username: str, password: str) -> None:
        username = (username or "").strip().lower()
        if len(username) < MIN_USER:
            raise AuthError(f"usuário precisa de {MIN_USER}+ caracteres")
        if len(password or "") < MIN_PASS:
            raise AuthError(f"senha precisa de {MIN_PASS}+ caracteres")
        users = self._load_users()
        if username in users:
            raise AuthError("usuário já existe")
        salt = secrets.token_hex(16)
        users[username] = {
            "salt": salt,
            "hash": self._hash(password, salt),
            "created_at": int(time.time()),
        }
        self._save_users(users)

    def login(self, username: str, password: str) -> str:
        username = (username or "").strip().lower()
        user = self._load_users().get(username)
        if user is None:
            time.sleep(0.25)  # equaliza tempo de resposta (anti-enumeration)
            raise AuthError("usuário ou senha inválidos")
        candidate = self._hash(password or "", user["salt"])
        if not secrets.compare_digest(candidate, user["hash"]):
            raise AuthError("usuário ou senha inválidos")
        return self._issue(username)

    # ------------------------------------------------------------------ #
    # Tokens
    # ------------------------------------------------------------------ #
    def _issue(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._cleanup_expired_locked()
            self._tokens[token] = {
                "user": username,
                "expires": time.time() + self.ttl,
            }
        return token

    def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return None
            if entry["expires"] < time.time():
                del self._tokens[token]
                return None
            entry["expires"] = time.time() + self.ttl  # renovação deslizante
            return entry["user"]

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def _cleanup_expired_locked(self) -> None:
        now = time.time()
        expired = [t for t, info in self._tokens.items()
                   if info["expires"] < now]
        for token in expired:
            del self._tokens[token]
