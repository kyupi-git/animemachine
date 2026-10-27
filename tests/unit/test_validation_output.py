"""The canonical runner must relay Unicode logs on non-UTF-8 consoles."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ValidationOutputTests(unittest.TestCase):
    def test_real_child_transcript_survives_legacy_console_encoding(self):
        runner = Path(__file__).resolve().parents[2] / 'scripts/test_all.py'
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'src').mkdir()
            tests = root / 'tests'
            tests.mkdir()
            (tests / 'test_unicode.py').write_text(
                'import unittest\nclass LogTest(unittest.TestCase):\n'
                ' def test_log(self): print("截图・续播・日本語 ✓")\n', encoding='utf-8')
            code = '''
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('canonical_runner',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module.SRC=pathlib.Path(sys.argv[2])/'src'
module.TESTS=pathlib.Path(sys.argv[2])/'tests'
module.check_dependencies=lambda:None
module.run=lambda *args,**kwargs:None
module.shutil.which=lambda command:sys.executable
sys.argv=[sys.argv[1]]
raise SystemExit(module.main())
'''
            env = {**os.environ, 'PYTHONUTF8': '0', 'PYTHONIOENCODING': 'cp1252:strict'}
            result = subprocess.run([sys.executable, '-c', code, str(runner), str(root)], env=env,
                                    capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn('截图・续播・日本語 ✓', result.stdout.decode('utf-8'))
        self.assertIn(b'Canonical verification passed.', result.stdout)
