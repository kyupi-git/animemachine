import tempfile
import unittest
from pathlib import Path

from animemachine.api import auth


class AuthTests(unittest.TestCase):
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

