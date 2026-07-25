"""Proves auth operations close every SQLite connection they open."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.auth_agent as auth


class AuthConnectionClosureTests(unittest.TestCase):
    def test_auth_workflow_closes_all_opened_connections(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "auth.db"
            real_connect = auth.connect
            opened = []

            def recording_connect():
                connection = real_connect()
                opened.append(connection)
                return connection

            with mock.patch.object(auth, "AUTH_DB", database), mock.patch.object(
                auth, "connect", side_effect=recording_connect
            ):
                auth.init_auth_db()
                auth.create_user(
                    "member",
                    "correct-horse-battery-staple",
                    email="member@example.com",
                )
                user = auth.authenticate_user(
                    "member", "correct-horse-battery-staple"
                )
                session = auth.create_session(user)
                csrf = auth.issue_csrf_token(session)
                self.assertTrue(auth.verify_csrf_token(session, csrf))
                token, _ = auth.create_reset_token("member")
                self.assertIsNotNone(auth.get_user_for_reset_token(token))
                auth.delete_all_sessions_for_user(user["id"])

            self.assertGreater(len(opened), 0)
            for connection in opened:
                with self.assertRaisesRegex(
                    auth.sqlite3.ProgrammingError, "closed database"
                ):
                    connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
