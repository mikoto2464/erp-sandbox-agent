import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erp_agent.adapters.sessions import SQLiteSessionStore
from erp_agent.application.access import AccessService, REMEMBER_SECONDS
from erp_agent.domain.errors import ERPError


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="erp-access-")
        self.store = SQLiteSessionStore(Path(self.temp.name) / "sessions.sqlite3")
        self.access = AccessService("test-key", self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_remembered_session_expires_at_thirty_days(self):
        with patch("erp_agent.application.access.time.time", return_value=1000):
            session = self.access.login("test-key", True, "browser")
        with patch("erp_agent.application.access.time.time", return_value=1000 + REMEMBER_SECONDS - 1):
            self.assertTrue(self.access.authenticated(session.token))
        with patch("erp_agent.application.access.time.time", return_value=1000 + REMEMBER_SECONDS):
            self.assertFalse(self.access.authenticated(session.token))

    def test_throttle_expires_and_unconfigured_key_denies_login(self):
        with patch("erp_agent.application.access.time.time", return_value=1000):
            for _ in range(10):
                with self.assertRaises(ERPError) as wrong:
                    self.access.login("wrong", True, "browser")
                self.assertEqual(wrong.exception.status, 401)
            with self.assertRaises(ERPError) as limited:
                self.access.login("test-key", True, "browser")
            self.assertEqual(limited.exception.status, 429)
        with patch("erp_agent.application.access.time.time", return_value=1301):
            self.assertTrue(self.access.login("test-key", False, "browser").token)
        with self.assertRaises(ERPError) as missing:
            AccessService("", self.store).login("", True, "browser")
        self.assertEqual(missing.exception.code, "access_not_configured")
