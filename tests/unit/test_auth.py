import tempfile
import unittest
from pathlib import Path

from animemachine.api import auth


class AuthTests(unittest.TestCase):
    def test_hidden_titles_are_account_scoped_idempotent_and_persisted(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "auth.sqlite3"
            store = auth.Store(path, enabled=False)
            store.set_hidden(1, 265, True)
            store.set_hidden(1, 265, True)
            store.set_hidden(2, 300, True)
            self.assertEqual([265], auth.Store(path, enabled=False).hidden_works(1))
            self.assertEqual([300], store.hidden_works(2))
            store.set_hidden(1, 265, False)
            store.set_hidden(1, 265, False)
            self.assertEqual([], store.hidden_works(1))
            for identity, hidden in ((0, True), (True, True), (265, "false"), (265, None)):
                with self.assertRaises(ValueError):
                    store.set_hidden(1, identity, hidden)

    def test_bootstrap_login_roles_sessions_and_disable(self):
        with tempfile.TemporaryDirectory() as raw:
            store = auth.Store(Path(raw) / "auth.sqlite3", enabled=True,
                               bootstrap_username="admin", bootstrap_password="correct-horse-battery")
            session = store.login("admin", "correct-horse-battery")
            self.assertEqual("admin", session.role)
            self.assertIsNotNone(store.from_cookie(f"anm_session={session.token}"))
            self.assertTrue(store.csrf_valid(session, session.csrf))
            user = store.create_user("viewer", "another-secure-password", "user")
            self.assertEqual("user", store.login("viewer", "another-secure-password").role)
            self.assertTrue(store.set_enabled(user["id"], False, actor_id=session.user_id))
            self.assertIsNone(store.login("viewer", "another-secure-password"))

    def test_disabled_mode_is_local_admin(self):
        with tempfile.TemporaryDirectory() as raw:
            store = auth.Store(Path(raw) / "auth.sqlite3", enabled=False)
            session = store.from_cookie("")
            self.assertEqual("admin", session.role)
            self.assertTrue(store.csrf_valid(session, ""))


if __name__ == "__main__":
    unittest.main()

