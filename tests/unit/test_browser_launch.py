import base64
from pathlib import Path
import subprocess
import tempfile
import unittest

from animemachine.integrations.browser_launch import permission_command


class BrowserLaunchTests(unittest.TestCase):
    def test_command_contains_only_the_selected_origin_and_two_players(self):
        command = permission_command('http://192.0.2.4:8787')
        script = base64.b64decode(command.split()[-1]).decode('utf-16-le')
        self.assertIn("-Origin 'http://192.0.2.4:8787'", script)
        self.assertIn("@('potplayer','vlc')", script)
        self.assertIn('HKCU:', script)
        self.assertNotIn('ExecutionPolicy', command)
        self.assertNotIn('allowed_origins = @(' + "'*'" + ')', script)
        self.assertTrue(command.startswith('powershell.exe '))
        self.assertLessEqual(len(command),8000)

    def test_invalid_origins_cannot_inject_powershell_or_authorize_other_scopes(self):
        for value in ("http://host';calc", 'http://host/path', 'http://host?x=y', 'file:///x',
                      'http://user:pass@host', 'http://*.example.com', 'http://host:invalid',
                      'http://host/#fragment', 'http://host\n;calc'):
            with self.subTest(origin=value), self.assertRaises(ValueError):
                permission_command(value)

    @unittest.skipUnless(Path('C:/Program Files/PowerShell/7/pwsh.exe').is_file(), 'PowerShell 7 is required')
    def test_powershell_preserves_existing_rules_and_is_idempotent_without_registry_writes(self):
        source = Path(__file__).resolve().parents[2] / 'src/animemachine/resources/windows/Allow-BrowserPlayback.ps1'
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/browser-permission-test.ps1'
        with tempfile.TemporaryDirectory() as raw:
            command = permission_command('http://192.0.2.4:8787')
            decoded = Path(raw) / 'generated-command.ps1'
            decoded.write_text(base64.b64decode(command.split()[-1]).decode('utf-16-le'), encoding='utf-8')
            interpreters = ['C:/Program Files/PowerShell/7/pwsh.exe',
                            'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe']
            for interpreter in interpreters:
                if Path(interpreter).is_file():
                    with self.subTest(interpreter=interpreter):
                        subprocess.run([interpreter, '-NoProfile', '-File', str(fixture),
                                        '-SourcePath', str(source), '-CommandPath', str(decoded)], check=True,
                                       capture_output=True, text=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
