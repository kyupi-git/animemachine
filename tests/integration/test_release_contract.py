import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[2]


class ReleaseContractTests(unittest.TestCase):
    def test_publication_scan_ignores_local_source_state_but_rejects_it_in_release(self):
        scanner = ROOT / "scripts" / "check_public_tree.py"
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source"
            (source / ".local").mkdir(parents=True)
            (source / ".local" / ".env.local").write_text("LOCAL_ONLY=placeholder", encoding="utf-8")
            (source / "README.md").write_text("public", encoding="utf-8")
            subprocess.run([sys.executable, str(scanner), str(source)], check=True, capture_output=True, text=True)
            release = pathlib.Path(directory) / "release.zip"
            with zipfile.ZipFile(release, "w") as archive:
                archive.writestr("AnimeMachine-test/.local/.env.local", "LOCAL_ONLY=placeholder")
            result = subprocess.run([sys.executable, str(scanner), str(release)], check=False, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("runtime state directory", result.stdout)

    def test_publication_scan_ignores_dependency_parser_fixtures_but_scans_own_app(self):
        scanner = ROOT / "scripts" / "check_public_tree.py"
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory) / "release"
            dependency = release / "app" / "dependency" / "parser.py"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("pass" + "word='fixture-value-with-more-than-20-characters'", encoding="utf-8")
            subprocess.run([sys.executable, str(scanner), str(release)], check=True, capture_output=True, text=True)
            owned = release / "app" / "animemachine" / "settings.py"
            owned.parent.mkdir(parents=True)
            owned.write_text("pass" + "word='private-value-with-more-than-20-characters'", encoding="utf-8")
            result = subprocess.run([sys.executable, str(scanner), str(release)], check=False, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("possible secret: app/animemachine/settings.py", result.stdout)

    def test_image_contains_only_product_runtime(self):
        dockerfile = (ROOT / "packaging" / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('org.opencontainers.image.title="AnimeMachine"', dockerfile)
        self.assertIn('org.opencontainers.image.source="https://github.com/kyupi-git/animemachine"', dockerfile)
        self.assertIn('ENTRYPOINT ["python", "/opt/animemachine/docker-entrypoint.py"]', dockerfile)
        self.assertIn("ANM_DOCKER_UPDATE_RUNTIME=1", dockerfile)
        self.assertIn("COPY packaging/docker/entrypoint.py", dockerfile)
        self.assertNotIn("COPY scripts/", dockerfile)
        self.assertNotIn("COPY deploy/", dockerfile)
        self.assertIn("COPY pyproject.toml VERSION", dockerfile)
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("!VERSION", dockerignore)
        self.assertIn("!packaging/docker/entrypoint.py", dockerignore)

    def test_publication_metadata_uses_the_canonical_repository(self):
        canonical_repo = "https://github.com/kyupi-git/animemachine"
        canonical_image = "ghcr.io/kyupi-git/animemachine"
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'Homepage = "{canonical_repo}"', project)
        self.assertIn(f'Repository = "{canonical_repo}"', project)
        publication_files = [
            ROOT / "scripts" / "windows" / "Build-Docker-Image.ps1",
            ROOT / "scripts" / "unix" / "build-docker-image.sh",
            ROOT / "deploy" / "compose" / "torrent-collector.yaml",
        ]
        for directory in (ROOT / "deploy" / "compose").iterdir():
            if directory.is_dir():
                publication_files.extend((directory / "compose.yaml", directory / ".env.example"))
        for path in publication_files:
            text = path.read_text(encoding="utf-8")
            self.assertIn(canonical_image, text, path)
            self.assertNotIn("ghcr.io/animemachine/animemachine", text, path)

    def test_release_workflow_publishes_release_assets_and_multiarch_image(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("packages: write", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("linux/amd64,linux/arm64", workflow)
        self.assertRegex(workflow, r"docker/build-push-action@[0-9a-f]{40} # v6")
        self.assertIn("gh release create", workflow)
        self.assertIn("animemachine-*-py3-none-any.whl", workflow)
        self.assertIn("release-app", workflow)
        self.assertIn("type=semver,pattern={{version}}", workflow)
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertTrue((ROOT / ".github" / "release-notes" / f"v{version}.md").is_file())

    def test_four_compose_modes_are_config_complete(self):
        directories = sorted(path for path in (ROOT / "deploy" / "compose").iterdir() if path.is_dir())
        self.assertEqual(4, len(directories))
        collector = (ROOT / "deploy" / "compose" / "torrent-collector.yaml").read_text(encoding="utf-8")
        for directory in directories:
            compose = (directory / "compose.yaml").read_text(encoding="utf-8")
            template = (directory / ".env.example").read_text(encoding="utf-8")
            self.assertIn("animemachine:", compose)
            self.assertIn('ANM_WEB_PORT: "8787"', compose)
            self.assertIn('ANM_BIND_ADDRESS: "0.0.0.0"', compose)
            self.assertIn('${ANM_BIND_ADDRESS:-0.0.0.0}:${ANM_WEB_PORT:-8787}:8787', compose)
            self.assertNotIn("env_file: .env", compose)
            self.assertIn("env_file: ${ANM_ENV_FILE:-.env}", compose)
            material = compose + (collector if directory.name.startswith(("03-", "04-")) else "")
            placeholders = re.findall(r"\$\{([A-Z][A-Z0-9_]*)([^}]*)\}", material)
            required = {name for name, modifier in placeholders if not modifier.startswith(":-")}
            declared = set(re.findall(r"^(?:#\s*)?([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
            self.assertEqual(set(), required - declared, directory.name)

    def test_managed_images_are_pinned_and_tuning_is_separate(self):
        root = ROOT / "deploy" / "compose"
        third = (root / "03-animemachine-managed-qbt" / "compose.yaml").read_text(encoding="utf-8")
        fourth = (root / "04-full-stack" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("lscr.io/linuxserver/qbittorrent:5.2.3", third)
        self.assertIn("lscr.io/linuxserver/qbittorrent:5.2.3", fourth)
        self.assertIn("wushuo894/ani-rss@sha256:f064a4ff7816fc5ec9cd1cea1044e5c3b790e22b7adb3ad638e49590e9b88571", fourth)
        self.assertNotIn(":latest", third + fourth)
        advanced = (root / "torrent-collector.advanced.env.example").read_text(encoding="utf-8")
        self.assertIn("TORRENT_COLLECTOR_HISTORY_ENABLED", advanced)
        for name in ("03-animemachine-managed-qbt", "04-full-stack"):
            ordinary = (root / name / ".env.example").read_text(encoding="utf-8")
            self.assertNotIn("TORRENT_COLLECTOR_HISTORY_PAGES_PER_JOB_PER_CYCLE=", ordinary)

    def test_managed_modes_and_external_only_mode_are_distinct(self):
        root = ROOT / "deploy" / "compose"
        first = (root / "01-animemachine-standalone" / "compose.yaml").read_text(encoding="utf-8")
        third = (root / "03-animemachine-managed-qbt" / "compose.yaml").read_text(encoding="utf-8")
        fourth = (root / "04-full-stack" / "compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("\n  qbittorrent:", first)
        self.assertIn("\n  qbittorrent:", third)
        self.assertNotIn("\n  ani-rss:", third)
        self.assertIn("\n  ani-rss:", fourth)
        self.assertIn("qbt-bootstrap:", third)
        self.assertIn("ani-rss-bootstrap:", fourth)
        self.assertNotIn("torrent-collector.yaml", first)
        self.assertIn("../torrent-collector.yaml", third)
        self.assertIn("../torrent-collector.yaml", fourth)

    def test_anirss_is_optional_outside_the_full_stack(self):
        root = ROOT / "deploy" / "compose"
        for name in ("01-animemachine-standalone", "02-animemachine-external-qbt", "03-animemachine-managed-qbt"):
            compose = (root / name / "compose.yaml").read_text(encoding="utf-8")
            template = (root / name / ".env.example").read_text(encoding="utf-8")
            self.assertNotIn("ANM_ANI_RSS_MEDIA_DIR:?", compose)
            self.assertNotRegex(template, r"^ANM_ANI_RSS_URL=", name)
            self.assertNotRegex(template, r"^ANM_ANI_RSS_API_KEY=", name)

    def test_torrent_collector_supplies_the_shared_pool_in_managed_modes(self):
        collector = (ROOT / "deploy" / "compose" / "torrent-collector.yaml").read_text(encoding="utf-8")
        self.assertIn("torrent-collector:", collector)
        self.assertIn('command: ["torrent-collector"]', collector)
        self.assertNotIn("python - <<", collector)
        self.assertNotIn("python -c", collector)
        self.assertIn("${ANM_TORRENT_POOL_DIR:?set ANM_TORRENT_POOL_DIR}:/torrents", collector)
        self.assertIn("TORRENT_COLLECTOR_PROXY_ENABLED:-false", collector)
        self.assertNotIn("192.168.", collector)

    def test_ci_parses_every_compose_configuration(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("docker compose --env-file", workflow)
        self.assertIn("config --quiet", workflow)

    def test_supply_chain_workflows_are_pinned_and_least_privileged(self):
        workflows = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / ".github" / "workflows").glob("*.yml")
        )
        unpinned = re.findall(r"uses:\s+[^\s@]+@(?![0-9a-f]{40})([^\s#]+)", workflows)
        self.assertEqual([], unpinned)
        security = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
        self.assertIn("dependency-review-action@", security)
        self.assertIn("javascript-typescript", security)
        self.assertIn("python -m pip_audit", security)
        self.assertTrue((ROOT / ".github" / "dependabot.yml").is_file())

    def test_local_launch_and_build_entrypoints_exist_on_all_platforms(self):
        for relative in (
            "scripts/windows/AnimeMachine.cmd",
            "scripts/windows/AnimeMachine.ps1",
            "scripts/windows/Build-Release.cmd",
            "scripts/windows/Build-Release.ps1",
            "scripts/windows/Build-Docker-Image.cmd",
            "scripts/windows/Clean-AnimeMachine.cmd",
            "scripts/unix/AnimeMachine.sh",
            "scripts/unix/AnimeMachine-Linux.sh",
            "scripts/unix/AnimeMachine-macOS.command",
            "scripts/unix/build-release.sh",
            "scripts/unix/build-docker-image.sh",
            "scripts/unix/Clean-AnimeMachine.sh",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_every_release_builder_packages_all_platform_launchers(self):
        windows = (ROOT / "scripts" / "windows" / "Build-Release.ps1").read_text(encoding="utf-8")
        unix = (ROOT / "scripts" / "unix" / "build-release.sh").read_text(encoding="utf-8")
        for launcher in ("AnimeMachine.cmd", "AnimeMachine-Linux.sh", "AnimeMachine-macOS.command"):
            self.assertIn(launcher, windows)
            self.assertIn(launcher, unix)
        self.assertIn("Get-ChildItem", windows)
        self.assertIn("find \"$root/docs\"", unix)
        self.assertIn("BUILD-INFO.json", windows)
        self.assertIn("BUILD-INFO.json", unix)
        self.assertIn("pip wheel", windows)
        self.assertIn("CHANGELOG.md", windows)
        self.assertIn("CHANGELOG.md", unix)
        self.assertNotIn("GitHub", (ROOT / "scripts" / "windows" / "Build-Release.cmd").read_text(encoding="utf-8"))

    def test_windows_release_workflow_uses_packaged_entrypoint(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('AnimeMachine-$version/AnimeMachine.ps1', workflow)
        self.assertIn("Build-Release.ps1", workflow)
        self.assertIn("build-release.sh", workflow)
        self.assertNotIn("Start-AnimeMachine-Web.ps1", workflow)
        self.assertIn("deploy/compose/[0-9][0-9]-*", workflow)

    def test_docker_builders_pin_both_compose_and_environment_bundle(self):
        windows = (ROOT / "scripts" / "windows" / "Build-Docker-Image.ps1").read_text(encoding="utf-8")
        unix = (ROOT / "scripts" / "unix" / "build-docker-image.sh").read_text(encoding="utf-8")
        for script in (windows, unix):
            self.assertIn("compose.yaml", script)
            self.assertIn(".env.example", script)
            self.assertIn("torrent-collector.yaml", script)

    def test_local_environment_template_has_shell_safe_assignment_examples(self):
        template = (ROOT / "deploy" / "local" / ".env.local.example").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^#[ \t]*[A-Z][A-Z0-9_]*=[^\r\n]*[ \t]+#[ \t]+", template, re.MULTILINE))

    def test_local_template_covers_external_connections(self):
        template = (ROOT / "deploy" / "local" / ".env.local.example").read_text(encoding="utf-8")
        for value in (
            "ANM_STATE_DIR", "ANM_TORRENT_POOL_DIR", "ANM_LIBRARY_DIR",
            "ANM_MANAGED_QBITTORRENT_URL", "ANM_QBT_API_KEY",
            "ANM_ANI_RSS_URL", "ANM_ANI_RSS_API_KEY",
        ):
            self.assertIn(value, template)


    def test_initializers_only_display_newly_generated_credentials(self):
        unix = (ROOT / "scripts" / "unix" / "initialize-animemachine.sh").read_text(encoding="utf-8")
        windows = (ROOT / "scripts" / "windows" / "Initialize-AnimeMachine.ps1").read_text(encoding="utf-8")
        for script in (unix, windows):
            self.assertIn("Docker Compose 2.20.3 or newer is required", script)
            self.assertIn("Existing credentials preserved", script)
        self.assertIn("had_admin=$(value ANM_ADMIN_PASSWORD)", unix)
        self.assertIn('[ -z "$had_admin" ]', unix)
        self.assertIn("$generated.ContainsKey('ANM_ADMIN_PASSWORD')", windows)
        self.assertIn("url=http://localhost", unix)
        self.assertIn("url=http://localhost", windows)
        self.assertIn("Protect-AnmCredentialFile", windows)
        launcher = (ROOT / "scripts" / "windows" / "AnimeMachine.ps1").read_text(encoding="utf-8")
        self.assertIn("Protect-AnmCredentialFile $environmentFile", launcher)
        self.assertIn("/inheritance:r", launcher + windows)

    def test_runtime_dependencies_are_release_pinned(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"httpx[http2]==0.28.1"', project)
        self.assertIn('"Pillow==12.3.0"', project)
        self.assertNotIn('"httpx[http2]>=', project)
        self.assertNotIn('"Pillow>=', project)

    def test_version_and_release_runtime_have_canonical_sources(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', project)
        self.assertIn('version = {file = ["VERSION"]}', project)
        self.assertNotRegex(project, r'(?m)^version\s*=\s*"\d+\.\d+\.\d+"')
        self.assertEqual("3.14", (ROOT / "RELEASE_PYTHON_VERSION").read_text(encoding="utf-8").strip())

    def test_three_language_documents_share_operational_contracts(self):
        guides = [(ROOT / name).read_text(encoding="utf-8") for name in
                  ("docs/guide.md", "docs/guide.en.md", "docs/guide.ja.md")]
        for text in guides:
            self.assertIn("http://127.0.0.1:8877", text)
            self.assertIn("2.20.3", text)
            self.assertIn("deploy/compose/torrent-collector.advanced.env.example", text)
            self.assertIn("ANM_CA_BUNDLE", text)
        for family in (("README.md", "README.en.md", "README.ja.md"),
                       ("docs/architecture.md", "docs/architecture.en.md", "docs/architecture.ja.md")):
            documents = [(ROOT / name).read_text(encoding="utf-8") for name in family]
            heading_counts = [sum(1 for line in text.splitlines() if line.startswith("#")) for text in documents]
            self.assertEqual([heading_counts[0]] * 3, heading_counts)

    def test_private_and_runtime_state_are_excluded(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for value in ("/config.json", "/AGENTS.md", "/.local/", "/deploy/private/"):
            self.assertIn(value, ignored)


if __name__ == "__main__":
    unittest.main()
