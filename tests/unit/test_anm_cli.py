from __future__ import annotations

import os
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animemachine import cli as anm_cli


class CliBootstrapTests(unittest.TestCase):
    def test_torrent_collector_cli_self_test_dispatches_before_main_runtime(self):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["animemachine", "torrent-collector", "--self-test"]), \
             contextlib.redirect_stdout(output), \
             mock.patch.object(anm_cli, "migrate_legacy_state", side_effect=AssertionError("main runtime must not start")):
            self.assertEqual(0, anm_cli.main())
        self.assertIn("torrent-collector self-test passed", output.getvalue())

    def test_interrupted_catalog_shell_requires_resumable_bootstrap(self):
        with tempfile.TemporaryDirectory() as raw:
            db_path = Path(raw) / "anime-catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.executemany("INSERT INTO metadata VALUES(?,?)", (
                    ("archive_name", "bootstrap-pending"), ("record_count", "0")))
            with mock.patch.object(anm_cli, "DB", db_path), \
                 mock.patch.object(anm_cli, "ensure_runtime_config"):
                self.assertTrue(anm_cli.ensure_catalog_shell())
                with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                    db.execute("UPDATE metadata SET value='dump.zip' WHERE key='archive_name'")
                    db.execute("UPDATE metadata SET value='123' WHERE key='record_count'")
                self.assertFalse(anm_cli.ensure_catalog_shell())

    def test_instance_lock_rejects_second_writer_and_releases_cleanly(self):
        with tempfile.TemporaryDirectory() as raw:
            first = anm_cli.InstanceLock(Path(raw))
            second = anm_cli.InstanceLock(Path(raw))
            first.acquire()
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                second.acquire()
            first.release()
            second.acquire()
            second.release()

    def test_windows_bootstrap_credential_file_is_acl_hardened(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "initial-admin.txt"
            with mock.patch.object(anm_cli.os, "name", "nt"), \
                 mock.patch.dict(os.environ, {"USERNAME": "tester", "USERDOMAIN": "WORKGROUP"}, clear=False), \
                 mock.patch.object(anm_cli.subprocess, "run") as run:
                run.return_value.returncode = 0
                anm_cli._write_bootstrap_credentials(target, "admin", "unit-test-password")
            args = run.call_args.args[0]
            self.assertEqual("icacls.exe", args[0])
            self.assertIn("/inheritance:r", args)
            self.assertIn("WORKGROUP\\tester:F", args)
            self.assertIn("*S-1-5-18:F", args)

    def test_initial_credentials_do_not_publish_access_address_before_readiness(self):
        output = io.StringIO()
        values = {
            "address": "http://127.0.0.1:8787",
            "username": "admin",
            "password": "unit-test-password",
            "path": "initial-admin.txt",
        }
        with contextlib.redirect_stdout(output):
            anm_cli._print_initial_credentials(values)
        text = output.getvalue()
        self.assertIn("Username: admin", text)
        self.assertIn("Password: unit-test-password", text)
        self.assertNotIn("Access:", text)
        self.assertNotIn("http://127.0.0.1:8787", text)


    def test_remote_zero_config_generates_and_reports_initial_admin(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            environment = {"ANM_AUTH_DB": str(state / "auth" / "auth.sqlite3")}
            with mock.patch.object(anm_cli, "STATE", state), mock.patch.dict(os.environ, environment, clear=True):
                values = anm_cli.configure_web_security("0.0.0.0", 8787)
                self.assertIsNotNone(values)
                assert values is not None
                self.assertEqual("admin", values["username"])
                self.assertTrue(values["password"].startswith("anm_"))
                self.assertEqual("true", os.environ["ANM_AUTH_ENABLED"])
                self.assertTrue((state / "auth" / "initial-admin.txt").is_file())

    def test_remote_zero_config_reuses_and_reports_saved_admin_after_bootstrap(self):
        from animemachine.api import auth
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            auth_db = state / "auth" / "auth.sqlite3"
            environment = {"ANM_AUTH_DB": str(auth_db)}
            with mock.patch.object(anm_cli, "STATE", state), mock.patch.dict(os.environ, environment, clear=True):
                first = anm_cli.configure_web_security("0.0.0.0", 8787)
                assert first is not None
                auth.Store(auth_db, enabled=True, bootstrap_username=first["username"],
                           bootstrap_password=first["password"])
            with mock.patch.object(anm_cli, "STATE", state), mock.patch.dict(os.environ, environment, clear=True):
                second = anm_cli.configure_web_security("0.0.0.0", 8787)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(first["username"], second["username"])
            self.assertEqual(first["password"], second["password"])

    def test_remote_first_start_reports_deployment_supplied_admin_password(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            environment = {
                "ANM_AUTH_DB": str(state / "auth" / "auth.sqlite3"),
                "ANM_ADMIN_USERNAME": "operator",
                "ANM_ADMIN_PASSWORD": "deployment-password-123",
            }
            with mock.patch.object(anm_cli, "STATE", state), mock.patch.dict(os.environ, environment, clear=True):
                values = anm_cli.configure_web_security("0.0.0.0", 8787)
            self.assertEqual("operator", values["username"] if values else None)
            self.assertEqual("deployment-password-123", values["password"] if values else None)

    def test_managed_qbt_bootstrap_generates_stable_zero_config_secrets(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(raw)
            anm_cli.bootstrap_qbittorrent(root)
            first_key = (root / ".animemachine" / "api-key").read_text(encoding="utf-8").strip()
            first_password = (root / ".animemachine" / "web-password").read_text(encoding="utf-8").strip()
            self.assertRegex(first_key, r"^qbt_[A-Za-z0-9]{28}$")
            self.assertGreaterEqual(len(first_password), 12)
            anm_cli.bootstrap_qbittorrent(root)
            self.assertEqual(first_key, (root / ".animemachine" / "api-key").read_text().strip())
            self.assertEqual(first_password, (root / ".animemachine" / "web-password").read_text().strip())

    def test_managed_ani_rss_bootstrap_generates_stable_zero_config_api_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            qbt_key = root / "qbt-key"
            qbt_key.write_text("qbt_0123456789abcdefghijklmnopqr", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANM_QBT_API_KEY_FILE": str(qbt_key)}, clear=True):
                ani_root = root / "ani"
                anm_cli.bootstrap_ani_rss(ani_root)
                first = (ani_root / ".animemachine" / "api-key").read_text(encoding="utf-8").strip()
                self.assertGreaterEqual(len(first), 24)
                anm_cli.bootstrap_ani_rss(ani_root)
                self.assertEqual(first, (ani_root / ".animemachine" / "api-key").read_text().strip())
                data = __import__("json").loads((ani_root / "config.v2.json").read_text(encoding="utf-8"))
                self.assertEqual(first, data["apiKey"])
                self.assertEqual(qbt_key.read_text().strip(), data["downloadToolPassword"])
                self.assertEqual("http://qbittorrent:8080", data["downloadToolHost"])
                self.assertTrue(data["autoStart"])
                self.assertTrue(data["qbUseDownloadPath"])
                self.assertEqual("/Media/番剧/${title}/Season ${season}", data["downloadPathTemplate"])
                self.assertEqual("/Media/剧场版/${title}", data["ovaDownloadPathTemplate"])

    def test_qbt_bootstrap_preserves_preferences_and_sets_api_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            secret = root / "api-key"
            secret.write_text("qbt_0123456789abcdefghijklmnopqr", encoding="utf-8")
            conf = root / "qbt" / "qBittorrent" / "qBittorrent.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text("[Preferences]\nDownloads\\SavePath=/Library\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANM_QBT_API_KEY_FILE": str(secret),
                                              "ANM_QBT_WEB_USERNAME": "admin",
                                              "ANM_QBT_WEB_PASSWORD": "unit-test-web-password",
                                              "ANM_QBT_LIBRARY_DIR": "/Library",
                                              "ANM_INCOMPLETE_DIR": "/incomplete"}):
                anm_cli.bootstrap_qbittorrent(root / "qbt")
            text = conf.read_text(encoding="utf-8")
            self.assertIn("Downloads\\SavePath=/Library", text)
            api_key_line = "WebUI\\APIKey=" + secret.read_text(encoding="utf-8")
            self.assertIn(api_key_line, text)
            self.assertIn("WebUI\\Address=0.0.0.0", text)
            self.assertIn("WebUI\\Username=admin", text)
            self.assertIn("WebUI\\Password_PBKDF2=", text)
            self.assertIn("Session\\DefaultSavePath=/Library", text)
            self.assertIn("Session\\TempPath=/incomplete", text)
            self.assertIn("Session\\TempPathEnabled=true", text)

    def test_qbt_bootstrap_rejects_nonstandard_api_key(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {"ANM_QBT_API_KEY": "qbt_too_long_or_invalid_key_value",
                             "ANM_QBT_WEB_PASSWORD": "unit-test-web-password"}):
            with self.assertRaisesRegex(ValueError, "28 alphanumeric"):
                anm_cli.bootstrap_qbittorrent(Path(raw))

    def test_legacy_state_is_copied_only_when_new_state_is_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy = root / "config" / "state" / "catalog"
            legacy.mkdir(parents=True)
            (legacy / "anime-catalog.sqlite3").write_bytes(b"legacy")
            current = root / "data" / "state"
            with mock.patch.object(anm_cli, "CONFIG", root / "config" / "config.json"), \
                 mock.patch.object(anm_cli, "STATE", current):
                anm_cli.migrate_legacy_state()
                self.assertEqual(b"legacy", (current / "catalog" / "anime-catalog.sqlite3").read_bytes())
                (legacy / "anime-catalog.sqlite3").write_bytes(b"changed")
                anm_cli.migrate_legacy_state()
                self.assertEqual(b"legacy", (current / "catalog" / "anime-catalog.sqlite3").read_bytes())

    def test_ani_rss_bootstrap_seeds_api_and_external_qbt_without_losing_settings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.v2.json"
            config.write_text(
                '{"customValue":true,"autoStart":false,"qbUseDownloadPath":false,'
                '"downloadPathTemplate":"/Media/custom"}\n', encoding="utf-8")
            environment = {
                "ANM_ANI_RSS_API_KEY": "ani_unit-test-abcdefghijklmnopqrstuvwxyz",
                "ANM_QBT_API_KEY": "qbt_unit-test-abcdefghijklmnopqrstuvwxyz",
                "ANM_MANAGED_QBITTORRENT_URL": "http://download-client:8080",
            }
            with mock.patch.dict(os.environ, environment):
                anm_cli.bootstrap_ani_rss(root)
            data = __import__("json").loads(config.read_text(encoding="utf-8"))
            self.assertTrue(data["customValue"])
            self.assertFalse(data["autoStart"])
            self.assertFalse(data["qbUseDownloadPath"])
            self.assertEqual("/Media/custom", data["downloadPathTemplate"])
            self.assertEqual("http://download-client:8080", data["downloadToolHost"])
            self.assertEqual(environment["ANM_ANI_RSS_API_KEY"], data["apiKey"])

    def test_ani_rss_bootstrap_seeds_first_run_defaults(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment = {
                "ANM_ANI_RSS_API_KEY": "ani_unit-test-abcdefghijklmnopqrstuvwxyz",
                "ANM_QBT_API_KEY": "qbt_unit-test-abcdefghijklmnopqrstuvwxyz",
            }
            with mock.patch.dict(os.environ, environment):
                anm_cli.bootstrap_ani_rss(root)
            data = __import__("json").loads((root / "config.v2.json").read_text(encoding="utf-8"))
            self.assertTrue(data["autoStart"])
            self.assertTrue(data["qbUseDownloadPath"])
            self.assertEqual("/Media/番剧/${title}/Season ${season}", data["downloadPathTemplate"])
            self.assertEqual("/Media/剧场版/${title}", data["ovaDownloadPathTemplate"])



if __name__ == "__main__":
    unittest.main()
