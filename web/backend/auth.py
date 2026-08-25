"""Compatibilidade: o AuthService vive agora em core/auth (compartilhado
pela CLI). Este módulo mantém os imports existentes da Web funcionando."""
from core.auth import AuthError, AuthService  # noqa: F401

__all__ = ["AuthService", "AuthError"]
