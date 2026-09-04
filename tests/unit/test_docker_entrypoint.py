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
