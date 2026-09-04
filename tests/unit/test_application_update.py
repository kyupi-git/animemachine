import datetime as dt
import hashlib
import io
import os
import tempfile
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from animemachine import application_update


class ApplicationUpdateTests(unittest.TestCase):
    def setUp(self):
        application_update._CACHE = None
        application_update._LAST_RELEASE_SOURCE = ""

    def _release(self, version="1.2.4"):
        portable = application_update._portable_asset_name(version)
        docker = application_update._docker_asset_name(version)
        assets = [
            {"name": docker, "browser_download_url": "https://example.invalid/docker", "size": 900,
             "digest": "sha256:" + "b" * 64},
            {"name": docker + ".sha256", "browser_download_url": "https://example.invalid/docker.sha256", "size": 100},
        ]
        if portable:
            assets.extend([
                {"name": portable, "browser_download_url": "https://example.invalid/release", "size": 1234,
                 "digest": "sha256:" + "a" * 64},
                {"name": portable + ".sha256", "browser_download_url": "https://example.invalid/checksum", "size": 100},
            ])
        return {"tag_name": f"v{version}", "html_url": "https://example.invalid/untrusted", "assets": assets}

    @staticmethod
    def _wheel(path: Path, version="1.2.4", *, dependency="httpx[http2]==0.28.1") -> bytes:
        metadata = "\n".join([
            "Metadata-Version: 2.4", "Name: animemachine", f"Version: {version}",
            f"Requires-Dist: {dependency}", 'Requires-Dist: ruff==0.13.2; extra == "test"', "",
        ])
        with zipfile.ZipFile(path, "w") as target:
            target.writestr("animemachine/__init__.py", "")
            target.writestr("animemachine/__main__.py", "")
            target.writestr("animemachine/web/static/index.html", "<html></html>")
            target.writestr(f"animemachine-{version}.dist-info/METADATA", metadata)
        return path.read_bytes()

    def test_update_health_probe_uses_actual_bind_interface(self):
        self.assertEqual("127.0.0.1", application_update._health_probe_host("0.0.0.0"))
        self.assertEqual("192.168.1.50", application_update._health_probe_host("192.168.1.50"))
        self.assertEqual("[::1]", application_update._health_probe_host("::"))
        self.assertEqual("[2001:db8::10]", application_update._health_probe_host("[2001:db8::10]"))
        self.assertEqual("[fe80::1%252]", application_update._health_probe_host("fe80::1%2"))

    def test_portable_update_workers_probe_supplied_health_host(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            new_root = Path(temp) / "stage" / "AnimeMachine-1.2.4"
            windows_worker = Path(temp) / "update.ps1"
            unix_worker = Path(temp) / "update.sh"
            application_update._write_windows_worker(
                windows_worker, root=root, new_root=new_root, old_pid=123, host="192.168.1.50", port=8877
            )
            application_update._write_unix_worker(
                unix_worker, root=root, new_root=new_root, old_pid=123, host="192.168.1.50", port=8877
            )
            windows_text = windows_worker.read_text(encoding="utf-8-sig")
            unix_text = unix_worker.read_text(encoding="utf-8")
        self.assertIn("[string]$HealthHost", windows_text)
        self.assertIn('"http://${HealthHost}:$Port/api/health/live"', windows_text)
        self.assertIn("health_host=$6", unix_text)
        self.assertIn('f"http://{sys.argv[3]}:{sys.argv[1]}/api/health/live"', unix_text)

    def test_release_discovery_tries_direct_and_application_proxies(self):
        payload = self._release()
        with mock.patch.object(application_update.network_sources, "fetch_json", return_value=(payload, "proxy")) as fetch:
            self.assertIs(application_update._release_payload(), payload)
        urls = list(fetch.call_args.args[0])
        self.assertEqual(application_update._LATEST_RELEASE_API, urls[0])
        self.assertTrue(any(url.startswith("https://gh-proxy.com/") for url in urls[1:]))
        self.assertTrue(any(url.startswith("https://ghfast.top/") for url in urls[1:]))

    def test_release_discovery_uses_update_health_and_cooldown(self):
        payload = self._release()
        with mock.patch.object(application_update.network_sources, "fetch_json", return_value=(payload, "https://api.github.com/result")) as fetch:
            application_update._release_payload()
        self.assertEqual("application_update_api", fetch.call_args.kwargs["service"])
        self.assertEqual("update_json", fetch.call_args.kwargs["capability"])
        self.assertTrue(fetch.call_args.kwargs["honor_cooldown"])
        self.assertEqual("https://api.github.com/result", application_update._LAST_RELEASE_SOURCE)

    def test_automatic_check_defaults_off_and_runs_once_per_local_day(self):
        self.assertEqual(
            {"enabled": False, "mode": "notify", "time": "04:35"},
            application_update._automatic_settings({}),
        )
        config = {"applicationUpdate": {"automaticCheck": {"enabled": True, "mode": "install", "time": "04:35"}}}
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"ANM_STATE_DIR": temp}, clear=False):
            before = dt.datetime(2026, 9, 2, 4, 34, tzinfo=dt.timezone(dt.timedelta(hours=8)))
            due = before.replace(minute=35)
            self.assertFalse(application_update.automatic_check_due(config, before))
            self.assertTrue(application_update.automatic_check_due(config, due))
            application_update.record_automatic_result(date="2026-09-02", mode="install", status_value="latest")
            self.assertFalse(application_update.automatic_check_due(config, due.replace(hour=23)))
            self.assertTrue(application_update.automatic_check_due(config, due + dt.timedelta(days=1)))

    def test_update_status_exposes_detail_fields(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(os.environ, {"ANM_INSTALL_MODE": "docker", "ANM_STATE_DIR": temp, "ANM_DOCKER_UPDATE_RUNTIME": "1"}, clear=False), \
             mock.patch.object(application_update, "__version__", "1.2.3"), \
             mock.patch.object(application_update, "_release_payload", return_value=self._release()), \
             mock.patch.object(application_update, "_probe_update_sources", return_value={
                 "items": [], "selectedDownloadSource": "https://ghfast.top/release",
                 "selectedApiSource": "https://api.github.com/release", "checkedAt": "2026-09-02T20:35:00Z",
             }):
            payload = application_update.status(force=True)
        self.assertEqual("1.2.3", payload["currentVersion"])
        self.assertEqual("1.2.4", payload["latestVersion"])
        self.assertEqual("https://ghfast.top/release", payload["downloadSource"])
        self.assertEqual("pending", payload["sha256Status"])
        self.assertIn("upgradeResult", payload)
        self.assertIn("sourceDiagnostics", payload)

    def test_preferred_download_routes_use_cached_selection_and_skip_cooldown(self):
        release = {
            "asset": {"url": "https://github.com/kyupi-git/animemachine/releases/download/v1.2.4/a.zip"},
            "sourceDiagnostics": {"items": [
                {"kind": "release", "baseUrl": "https://github.com/kyupi-git/animemachine/releases/download/v1.2.4/a.zip", "coolingDown": True, "selection": {"selected": False}},
                {"kind": "release", "baseUrl": "https://gh-proxy.com/https://github.com/kyupi-git/animemachine/releases/download/v1.2.4/a.zip", "latencyMs": 250, "recentSuccessRate": 1.0, "selection": {"selected": False}},
                {"kind": "release", "baseUrl": "https://ghfast.top/https://github.com/kyupi-git/animemachine/releases/download/v1.2.4/a.zip", "latencyMs": 80, "recentSuccessRate": 1.0, "selection": {"selected": True}},
            ]},
        }
        with mock.patch.object(application_update, "_proxy_templates", return_value=[
            "https://gh-proxy.com/{url}", "https://ghfast.top/{url}"
        ]):
            values = application_update._preferred_download_urls(release)
        self.assertIn("ghfast.top", values[0])
        self.assertFalse(any(url.startswith("https://github.com/") for url in values))

    def test_reconcile_marks_restarted_target_as_installed(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(os.environ, {"ANM_STATE_DIR": temp}, clear=False), \
             mock.patch.object(application_update, "__version__", "1.2.4"):
            application_update._record_state(lastUpgrade={"status": "restarting", "version": "1.2.4", "mode": "docker"})
            result = application_update.reconcile_upgrade_state()
            persisted = application_update.update_state()["lastUpgrade"]
        self.assertEqual("installed", result["status"])
        self.assertEqual("installed", persisted["status"])
        self.assertIn("completedAt", persisted)

    def test_portable_release_can_offer_verified_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "AnimeMachine"
            root.mkdir()
            (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANM_INSTALL_MODE": "portable", "ANM_INSTALL_ROOT": str(root)}, clear=False), \
                 mock.patch.object(application_update, "__version__", "1.2.3"):
                status = application_update._build_status(self._release())
            self.assertTrue(status["updateAvailable"])
            self.assertTrue(status["canUpdate"])
            self.assertEqual(status["latestVersion"], "1.2.4")
            self.assertEqual(status["asset"]["sha256"], "a" * 64)
            self.assertIn("/kyupi-git/animemachine/releases/download/v1.2.4/", status["asset"]["url"])
            self.assertNotIn("example.invalid", status["asset"]["url"])

    def test_docker_release_can_offer_online_update_with_supported_runtime(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(os.environ, {
                 "ANM_INSTALL_MODE": "docker", "ANM_INSTALL_ROOT": "/opt/animemachine",
                 "ANM_STATE_DIR": temp, "ANM_DOCKER_UPDATE_RUNTIME": "1"}, clear=False), \
             mock.patch.object(application_update, "__version__", "1.2.3"):
            status = application_update._build_status(self._release())
        self.assertTrue(status["updateAvailable"])
        self.assertTrue(status["canUpdate"])
        self.assertEqual("animemachine-1.2.4-py3-none-any.whl", status["asset"]["name"])

    def test_old_docker_image_requires_one_full_image_update(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(os.environ, {
                 "ANM_INSTALL_MODE": "docker", "ANM_STATE_DIR": temp,
                 "ANM_DOCKER_UPDATE_RUNTIME": "0"}, clear=False), \
             mock.patch.object(application_update, "__version__", "1.2.3"):
            status = application_update._build_status(self._release())
        self.assertFalse(status["canUpdate"])
        self.assertEqual("docker_update_runtime_unavailable", status["reason"])

    def test_zip_release_extraction_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "release.zip"
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr("AnimeMachine-1.2.4/VERSION", "1.2.4\n")
                target.writestr("../outside.txt", "bad")
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                application_update._extract_release(archive, Path(temp) / "stage", "1.2.4")

    def test_tar_release_extraction_rejects_special_file_types(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "release.tar.gz"
            launcher = "AnimeMachine.ps1" if os.name == "nt" else "AnimeMachine.sh"
            with tarfile.open(archive, "w:gz") as target:
                for name, data in (("AnimeMachine-1.2.4/VERSION", b"1.2.4\n"),
                                   (f"AnimeMachine-1.2.4/{launcher}", b"echo ok\n")):
                    info = tarfile.TarInfo(name); info.size = len(data)
                    target.addfile(info, io.BytesIO(data))
                special = tarfile.TarInfo("AnimeMachine-1.2.4/pipe")
                special.type = tarfile.FIFOTYPE
                target.addfile(special)
            with self.assertRaisesRegex(ValueError, "unsupported entry type"):
                application_update._extract_release(archive, Path(temp) / "stage", "1.2.4")

    def test_zip_release_extraction_requires_matching_version_and_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "release.zip"
            launcher = "AnimeMachine.ps1" if os.name == "nt" else "AnimeMachine.sh"
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr("AnimeMachine-1.2.4/VERSION", "1.2.4\n")
                target.writestr(f"AnimeMachine-1.2.4/{launcher}", "echo ok\n")
            root = application_update._extract_release(archive, Path(temp) / "stage", "1.2.4")
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), "1.2.4")
            self.assertTrue((root / launcher).is_file())

    def test_docker_wheel_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "bad.whl"
            with zipfile.ZipFile(wheel, "w") as target:
                target.writestr("../outside", "bad")
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                application_update._extract_docker_wheel(wheel, Path(temp) / "stage", "1.2.4")

    def test_docker_wheel_validates_runtime_files_version_and_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "update.whl"
            self._wheel(wheel)
            root = application_update._extract_docker_wheel(wheel, Path(temp) / "stage", "1.2.4")
            with mock.patch("importlib.metadata.version", return_value="0.28.1"):
                application_update._validate_docker_dependencies(root)
            with mock.patch("importlib.metadata.version", return_value="0.27.0"):
                with self.assertRaisesRegex(ValueError, "base image update required"):
                    application_update._validate_docker_dependencies(root)

    def test_docker_activation_persists_current_and_pending_for_supervisor(self):
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "update.whl"
            self._wheel(wheel)
            state = Path(temp) / "state"
            with mock.patch.dict(os.environ, {"ANM_STATE_DIR": str(state)}, clear=False), \
                 mock.patch.object(application_update, "_validate_docker_dependencies"):
                application_update._activate_docker_release(wheel, "1.2.4")
            update_root = state / "application-update"
            self.assertEqual("1.2.4", (update_root / "current").read_text(encoding="utf-8").strip())
            self.assertIn('"version":"1.2.4"', (update_root / "pending.json").read_text(encoding="utf-8"))
            self.assertTrue((update_root / "releases" / "1.2.4" / "animemachine" / "__main__.py").is_file())

    def test_docker_activation_never_switches_current_before_pending_is_durable(self):
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "update.whl"
            self._wheel(wheel)
            state = Path(temp) / "state"
            update_root = state / "application-update"
            update_root.mkdir(parents=True)
            (update_root / "current").write_text("1.2.3\n", encoding="utf-8")
            calls = []
            real_atomic_text = application_update._atomic_text

            def fail_current(path, value):
                calls.append(Path(path).name)
                if Path(path).name == "current":
                    raise OSError("simulated pointer write failure")
                return real_atomic_text(path, value)

            with mock.patch.dict(os.environ, {"ANM_STATE_DIR": str(state)}, clear=False), \
                 mock.patch.object(application_update, "_validate_docker_dependencies"), \
                 mock.patch.object(application_update, "_atomic_text", side_effect=fail_current):
                with self.assertRaisesRegex(OSError, "simulated pointer write failure"):
                    application_update._activate_docker_release(wheel, "1.2.4")

            self.assertEqual(["pending.json", "current"], calls)
            self.assertEqual("1.2.3", (update_root / "current").read_text(encoding="utf-8").strip())
            self.assertFalse((update_root / "pending.json").exists())

    def test_docker_apply_uses_verified_direct_and_proxy_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "source.whl"
            content = self._wheel(wheel)
            sha = hashlib.sha256(content).hexdigest()
            release = {
                "updateAvailable": True, "canUpdate": True, "latestVersion": "1.2.4",
                "asset": {"name": "animemachine-1.2.4-py3-none-any.whl",
                          "url": "https://github.com/kyupi-git/animemachine/releases/download/v1.2.4/animemachine-1.2.4-py3-none-any.whl",
                          "size": len(content), "sha256": sha, "checksumUrl": ""},
            }
            def fake_download(urls, destination, **kwargs):
                self.assertTrue(any(url.startswith("https://gh-proxy.com/") for url in urls))
                self.assertTrue(any(url.startswith("https://ghfast.top/") for url in urls))
                destination.write_bytes(content)
                return {"size": len(content), "sha256": sha, "urls": [urls[0]]}
            with mock.patch.dict(os.environ, {
                    "ANM_INSTALL_MODE": "docker", "ANM_STATE_DIR": str(Path(temp) / "state"),
                    "ANM_DOCKER_UPDATE_RUNTIME": "1"}, clear=False), \
                 mock.patch.object(application_update, "status", return_value=release), \
                 mock.patch.object(application_update.downloads, "download_verified", side_effect=fake_download), \
                 mock.patch.object(application_update, "_activate_docker_release") as activate:
                result = application_update.apply(host="0.0.0.0", port=8787)
            self.assertEqual("docker", result["mode"])
            activate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
