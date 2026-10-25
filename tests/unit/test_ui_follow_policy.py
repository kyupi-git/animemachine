"""Display-language changes must not change the shared release policy's groups."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from animemachine.config.loader import serial_profile_language


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class FollowPolicyUITests(unittest.TestCase):
    def test_follow_group_profile_matches_the_server_across_browser_languages(self):
        source = Path(__file__).resolve().parents[2] / 'src/animemachine/web/static/app.js'
        settings = [{'ui': {'language': locale}, 'torrentPolicy': {'serialSubtitle': {'language': choice}}}
                    for locale in ('zh-Hans', 'en', 'ja') for choice in ('auto', 'zh', 'en', 'ja')]
        script = r'''
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('function serialPolicyLanguage('),source.indexOf('function renderFollowChoices('));
const settings=JSON.parse(process.argv[2]);
for(const browserLanguage of ['zh-Hans','en','ja']) {
 const context=vm.createContext({language:browserLanguage});
 vm.runInContext(code+'\nthis.resolve=serialPolicyLanguage;',context);
 console.log(JSON.stringify(settings.map(item=>context.resolve(item))));
}
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(source), json.dumps(settings)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        expected = [serial_profile_language(value) for value in settings]
        self.assertEqual([expected] * 3, [json.loads(line) for line in result.stdout.splitlines()])
