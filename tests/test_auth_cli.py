"""Testes da autenticação compartilhada (CLI + Web) e do gate da CLI."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config.settings import Settings
from core.auth import AuthError, AuthService
from core.runtime import PudimAIRuntime
from tests.fakes import FakeLLM


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = AuthService(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_register_login_verify_roundtrip(self):
        self.service.register("ana", "segredo123")
        token = self.service.login("ana", "segredo123")
        self.assertEqual("ana", self.service.verify(token))

    def test_wrong_password_rejected(self):
        self.service.register("ana", "segredo123")
        with self.assertRaises(AuthError):
            self.service.login("ana", "errada999")

    def test_duplicate_user_and_weak_credentials(self):
        self.service.register("ana", "segredo123")
        with self.assertRaises(AuthError):
            self.service.register("ana", "outra456")
        with self.assertRaises(AuthError):
            self.service.register("ab", "curta1")       # usuário curto
        with self.assertRaises(AuthError):
            self.service.register("carla", "123")       # senha curta

    def test_unknown_user_login_takes_constantish_path(self):
        with self.assertRaises(AuthError):
            self.service.login("fantasma", "qualquer1")

    def test_revoke_invalidates_token(self):
        self.service.register("ana", "segredo123")
        token = self.service.login("ana", "segredo123")
        self.service.revoke(token)
        self.assertIsNone(self.service.verify(token))
        self.assertIsNone(self.service.verify(None))


class CliAuthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = PudimAIRuntime(self.root, llm_override=FakeLLM())

    def tearDown(self):
        self._tmp.cleanup()

    def test_runtime_exposes_shared_auth(self):
        # mesma base de contas que a Web usaria (mesmo users.json)
        self.assertIsInstance(self.runtime.auth, AuthService)
        self.assertEqual(
            self.runtime.auth.users_file,
            self.runtime.storage_dir / "users.json")
        self.runtime.auth.register("dev", "local123")
        token = self.runtime.auth.login("dev", "local123")
        self.assertEqual("dev", self.runtime.auth.verify(token))

    def test_cli_require_login_gate_setting(self):
        with mock.patch.dict(os.environ,
                             {"PUDIMAI_CLI_REQUIRE_LOGIN": "1"}):
            settings = Settings.load(self.root)
            self.assertTrue(settings.cli_require_login)
        settings_default = Settings(workspace=str(self.root))
        self.assertFalse(settings_default.cli_require_login)


if __name__ == "__main__":
    unittest.main()
