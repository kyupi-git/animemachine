import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("anm_docker_entrypoint", ROOT / "packaging" / "docker" / "entrypoint.py")
entrypoint = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(entrypoint)


class DockerEntrypointTests(unittest.TestCase):
    def _release(self, state: Path, version: str) -> Path:
        root = state / "application-update" / "releases" / version
        (root / "animemachine").mkdir(parents=True)
        (root / "animemachine" / "__main__.py").write_text("", encoding="utf-8")
        info = root / f"animemachine-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Name: animemachine\nVersion: {version}\n", encoding="utf-8")
        return root

    def test_health_probe_tracks_bind_address_and_ip_family(self):
        self.assertEqual("127.0.0.1", entrypoint._health_probe_host("0.0.0.0"))
        self.assertEqual("192.168.1.50", entrypoint._health_probe_host("192.168.1.50"))
        self.assertEqual("[::1]", entrypoint._health_probe_host("::"))
        self.assertEqual("[2001:db8::10]", entrypoint._health_probe_host("2001:db8::10"))

    def test_supervisor_parses_equals_form_host_and_port(self):
        arguments = ["run", "--host=192.168.1.50", "--port=8899"]
        self.assertEqual("192.168.1.50", entrypoint._host(arguments))
        self.assertEqual(8899, entrypoint._port(arguments))

    def test_health_request_uses_effective_bind_address(self):
        response = mock.MagicMock()
        response.__enter__.return_value = io.StringIO(
            json.dumps({"ok": True, "service": "AnimeMachine", "version": "1.2.4"})
        )
        with mock.patch.object(entrypoint.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertTrue(entrypoint._healthy(8787, "1.2.4", "192.168.1.50"))
        self.assertEqual("http://192.168.1.50:8787/api/health/live", urlopen.call_args.args[0])

    def test_writable_paths_are_command_specific(self):
        with mock.patch.dict(os.environ, {
                "ANM_CONFIG_PATH": "/config/config.json", "ANM_STATE_DIR": "/data/state",
                "ANM_LIBRARY_DIR": "/library", "ANM_QBT_LIBRARY_DIR": "/qbt-library",
                "ANM_ANI_RSS_MEDIA_DIR": "/media", "ANM_INCOMPLETE_DIR": "/incomplete",
                "TORRENT_COLLECTOR_STATE_DIR": "/collector", "ANM_TORRENT_POOL_DIR": "/torrents"}, clear=False):
            expected_app_paths = [Path("/config"), Path("/data/state"), Path("/library"), Path("/incomplete")]
            for command in ("run", "init", "serve", "healthcheck", "validate-config", "sync"):
                self.assertEqual(expected_app_paths, entrypoint._writable_paths([command]))
            self.assertEqual([], entrypoint._writable_paths(["storage-preflight"]))
            self.assertEqual([Path("/bootstrap"), Path("/qbt-library"), Path("/media"), Path("/incomplete")],
                             entrypoint._writable_paths(["qbt-bootstrap", "--config-dir", "/bootstrap"]))
            self.assertEqual([Path("/bootstrap")], entrypoint._writable_paths(
                ["ani-rss-bootstrap", "--config-dir", "/bootstrap"]
            ))
            self.assertEqual([Path("/collector"), Path("/torrents")],
                             entrypoint._writable_paths(["torrent-collector"]))

    @unittest.skipUnless(os.name == "posix", "Docker identity switching is POSIX-only")
    def test_root_entrypoint_prepares_mounts_then_drops_privileges(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "PUID": "1234", "PGID": "2345", "HOME": "/root",
                "ANM_CONFIG_PATH": str(root / "config" / "config.json"),
                "ANM_STATE_DIR": str(root / "data" / "state"),
                "ANM_LIBRARY_DIR": str(root / "library"),
                "ANM_INCOMPLETE_DIR": str(root / "incomplete"),
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(entrypoint.os, "geteuid", return_value=0), \
                    mock.patch.object(entrypoint.os, "chown") as chown, \
                    mock.patch.object(entrypoint.os, "setgroups") as setgroups, \
                    mock.patch.object(entrypoint.os, "setgid") as setgid, \
                    mock.patch.object(entrypoint.os, "setuid") as setuid:
                entrypoint._drop_privileges(["run"])
                self.assertEqual("/tmp", os.environ["HOME"])
            for path in (root / "config", root / "data" / "state", root / "library", root / "incomplete"):
                self.assertTrue(path.is_dir())
                self.assertIn(mock.call(path, 1234, 2345), chown.call_args_list)
            setgroups.assert_called_once_with([])
            setgid.assert_called_once_with(2345)
            setuid.assert_called_once_with(1234)

    @unittest.skipUnless(os.name == "posix", "Docker identity switching is POSIX-only")
    def test_non_root_entrypoint_does_not_change_identity(self):
        with mock.patch.object(entrypoint.os, "geteuid", return_value=1000), \
                mock.patch.object(entrypoint.os, "setuid") as setuid:
            entrypoint._drop_privileges(["run"])
        setuid.assert_not_called()

    def test_newer_persistent_release_is_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"; state = Path(temp) / "state"
            base.mkdir(); (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            release = self._release(state, "1.2.4")
            update = state / "application-update"; (update / "current").write_text("1.2.4\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANM_INSTALL_ROOT": str(base), "ANM_STATE_DIR": str(state)}, clear=False):
                selected, version = entrypoint._selected_release()
            self.assertEqual(release, selected)
            self.assertEqual("1.2.4", version)

    def test_full_image_newer_than_persistent_layer_discards_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"; state = Path(temp) / "state"
            base.mkdir(); (base / "VERSION").write_text("1.3.0\n", encoding="utf-8")
            self._release(state, "1.2.4")
            update = state / "application-update"; (update / "current").write_text("1.2.4\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANM_INSTALL_ROOT": str(base), "ANM_STATE_DIR": str(state)}, clear=False):
                selected, version = entrypoint._selected_release()
            self.assertIsNone(selected)
            self.assertEqual("1.3.0", version)
            self.assertFalse((update / "current").exists())

    def test_pending_failure_rolls_back_to_previous_layer(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"; state = Path(temp) / "state"
            base.mkdir(); (base / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            self._release(state, "1.2.4"); self._release(state, "1.2.5")
            update = state / "application-update"
            (update / "current").write_text("1.2.5\n", encoding="utf-8")
            (update / "pending.json").write_text(json.dumps({"version": "1.2.5", "previous": "1.2.4"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANM_INSTALL_ROOT": str(base), "ANM_STATE_DIR": str(state)}, clear=False):
                entrypoint._rollback({"version": "1.2.5", "previous": "1.2.4"})
            self.assertEqual("1.2.4", (update / "current").read_text(encoding="utf-8").strip())
            self.assertFalse((update / "pending.json").exists())


if __name__ == "__main__":
    unittest.main()
