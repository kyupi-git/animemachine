import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animemachine.config import credentials


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        credentials._STATE_LOADED.clear()

    def tearDown(self):
        credentials._STATE_LOADED.clear()

    def _clean_environment(self):
        names = set(credentials.CREDENTIAL_FILES) | set(credentials.CREDENTIAL_FILE_ENV.values())
        return mock.patch.dict(os.environ, {name: "" for name in names}, clear=False)

    def test_web_saved_credential_survives_environment_reload(self):
        with tempfile.TemporaryDirectory() as directory, self._clean_environment():
            state = Path(directory)
            path = credentials.store("ANM_ANI_RSS_API_KEY", "saved-key", state)
            self.assertEqual("saved-key", os.environ["ANM_ANI_RSS_API_KEY"])
            self.assertEqual("saved-key", path.read_text(encoding="utf-8").strip())
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
            os.environ.pop("ANM_ANI_RSS_API_KEY", None)
            credentials._STATE_LOADED.clear()
            self.assertEqual({"ANM_ANI_RSS_API_KEY"}, credentials.load_into_environment(state))
            self.assertEqual("saved-key", os.environ["ANM_ANI_RSS_API_KEY"])

    def test_direct_deployment_environment_keeps_precedence_over_web_saved_value(self):
        with tempfile.TemporaryDirectory() as directory, self._clean_environment():
            state = Path(directory)
            os.environ["ANM_QBT_API_KEY"] = "deployment-key"
            credentials.store("ANM_QBT_API_KEY", "web-key", state)
            self.assertEqual("deployment-key", os.environ["ANM_QBT_API_KEY"])
            self.assertEqual("web-key", credentials.credential_path("ANM_QBT_API_KEY", state).read_text().strip())

    def test_deployment_secret_file_keeps_precedence_and_missing_file_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory, self._clean_environment():
            state = Path(directory) / "state"
            credentials.store("ANM_ANI_RSS_API_KEY", "web-key", state)
            os.environ.pop("ANM_ANI_RSS_API_KEY", None)
            credentials._STATE_LOADED.clear()
            secret = Path(directory) / "ani-secret"
            secret.write_text("deployment-key\n", encoding="utf-8")
            os.environ["ANM_ANI_RSS_API_KEY_FILE"] = str(secret)
            self.assertEqual({"ANM_ANI_RSS_API_KEY"}, credentials.load_into_environment(state))
            self.assertEqual("deployment-key", os.environ["ANM_ANI_RSS_API_KEY"])
            os.environ.pop("ANM_ANI_RSS_API_KEY", None)
            os.environ["ANM_ANI_RSS_API_KEY_FILE"] = str(Path(directory) / "missing-secret")
            self.assertEqual(set(), credentials.load_into_environment(state))
            self.assertNotIn("ANM_ANI_RSS_API_KEY", os.environ)

    def test_windows_directory_acl_is_inherited_by_new_credential_files(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_directory = Path(directory)
            with mock.patch.object(credentials.os, "name", "nt"), \
                    mock.patch.dict(os.environ, {"USERNAME": "anm", "USERDOMAIN": "HOST"}, clear=False), \
                    mock.patch.object(credentials.subprocess, "run") as run:
                credentials._restrict_permissions(credential_directory, directory=True)
        arguments = run.call_args.args[0]
        self.assertIn("HOST\\anm:(OI)(CI)F", arguments)
        self.assertIn("*S-1-5-18:(OI)(CI)F", arguments)
        self.assertIn("/inheritance:r", arguments)
        self.assertIn("/grant:r", arguments)

    def test_subtitle_credentials_use_the_same_durable_store(self):
        with tempfile.TemporaryDirectory() as directory, self._clean_environment():
            state = Path(directory)
            credentials.store("ASSRT_API_TOKEN", "assrt-token", state)
            credentials.store("OPEN_SUBTITLES_API_KEY", "opensubtitles-key", state)
            os.environ.pop("ASSRT_API_TOKEN", None)
            os.environ.pop("OPEN_SUBTITLES_API_KEY", None)
            credentials._STATE_LOADED.clear()
            loaded = credentials.load_into_environment(state)
            self.assertEqual({"ASSRT_API_TOKEN", "OPEN_SUBTITLES_API_KEY"}, loaded)
            self.assertEqual("assrt-token", os.environ["ASSRT_API_TOKEN"])
            self.assertEqual("opensubtitles-key", os.environ["OPEN_SUBTITLES_API_KEY"])


    def test_multi_credential_store_rolls_back_files_environment_and_loaded_state(self):
        with tempfile.TemporaryDirectory() as directory, self._clean_environment():
            state = Path(directory)
            first = credentials.store("ANM_QBT_API_KEY", "old-qbt", state)
            second = credentials.credential_path("ANM_ANI_RSS_API_KEY", state)
            os.environ.pop("ANM_ANI_RSS_API_KEY", None)
            original = credentials.store
            calls = 0

            def fail_second(environment, value, state_dir=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated credential write failure")
                return original(environment, value, state_dir)

            with mock.patch.object(credentials, "store", side_effect=fail_second):
                with self.assertRaises(OSError):
                    credentials.store_many({
                        "ANM_QBT_API_KEY": "new-qbt",
                        "ANM_ANI_RSS_API_KEY": "new-ani",
                    }, state)
            self.assertEqual("old-qbt", first.read_text(encoding="utf-8").strip())
            self.assertFalse(second.exists())
            self.assertEqual("old-qbt", os.environ["ANM_QBT_API_KEY"])
            self.assertNotIn("ANM_ANI_RSS_API_KEY", os.environ)
            self.assertIn("ANM_QBT_API_KEY", credentials._STATE_LOADED)
            self.assertNotIn("ANM_ANI_RSS_API_KEY", credentials._STATE_LOADED)

    def test_multi_credential_store_rejects_unknown_environment_before_writing(self):
        with tempfile.TemporaryDirectory() as directory, self._clean_environment():
            with self.assertRaises(ValueError):
                credentials.store_many({"UNKNOWN_SECRET": "value"}, Path(directory))
            self.assertFalse((Path(directory) / "credentials").exists())



if __name__ == "__main__":
    unittest.main()
