import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("clean_runtime", Path(__file__).resolve().parents[2] / "scripts/clean_runtime.py")
cleaner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleaner)


class SafeCleanupTests(unittest.TestCase):
    def test_data_media_downloads_and_virtual_environments_are_not_walked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            preserved = []
            for directory in ("library", "data/state", ".local/state", "config", "imports", "external", ".venv"):
                for filename in ("video.part", "active.tmp", "__pycache__/important.pyc"):
                    path = root / directory / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"keep")
                    preserved.append(path)
            cache = root / "src/animemachine/__pycache__/code.pyc"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"cache")
            self.assertEqual(1, cleaner.clean(root))
            self.assertFalse(cache.exists())
            self.assertTrue(all(path.read_bytes() == b"keep" for path in preserved))
            self.assertEqual(0, cleaner.clean(root))
