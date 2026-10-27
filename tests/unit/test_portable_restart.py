"""Exercise the actual Windows process API with paths that broke CRT exec."""
import json
import os
import tempfile
import unittest
import venv
from pathlib import Path
from unittest import mock

from animemachine import cli


class PortableRestartTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows process quoting')
    def test_unicode_space_executable_path_and_literal_arguments(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)/'播放器 测试 $%&';venv.create(root,with_pip=False)
            executable=root/'Scripts/python.exe'
            probe=root/'capture arguments.py';result=root/'result.json'
            probe.write_text('import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]),encoding="utf-8")\n',encoding='utf-8')
            expected=['', '中文 空格', 'trailing\\', 'a"b', '$value%PATH%&;[]']
            with mock.patch.object(cli.sys,'executable',str(executable)), \
                 mock.patch.object(cli.sys,'orig_argv',[str(executable),str(probe),str(result),*expected]), \
                 mock.patch.object(cli.credential_store,'prepare_restart'):
                child=cli._restart_portable()
                self.assertIsNotNone(child)
                try:
                    self.assertEqual(0,child.wait(timeout=20))
                finally:
                    if child.poll() is None:
                        child.kill();child.wait(timeout=5)
            self.assertEqual(expected,json.loads(result.read_text(encoding='utf-8')))
