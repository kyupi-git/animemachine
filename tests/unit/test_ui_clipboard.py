"""Exercise copying inside modal dialogs and under browser permission failures."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class ClipboardTests(unittest.TestCase):
    def run_scenario(self, scenario):
        source = Path(__file__).parents[2] / 'src/animemachine/web/static/app.js'
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const fragment = source.slice(source.indexOf('async function copyPlainText('), source.indexOf('async function handoffPlayback('));
let clipboard = 'previous clipboard', dialog = null, created = [], prompts = [], copyResult = true, copyThrows = false, focusBlocked = false;
const previous = {isConnected:true, selectionStart:2, selectionEnd:5, selectionDirection:'backward',
  focus() {document.activeElement = this;}, closest:() => dialog,
  setSelectionRange(...selection) {this.restoredSelection = selection;}};
const document = {activeElement:previous,
  body:{append(field) {field.parent = this;}},
  createElement() {
    const field = {style:{}, setAttribute() {}, focus() {if (!focusBlocked) document.activeElement = this;},
      select() {this.selectionStart = 0; this.selectionEnd = this.value.length;},
      setSelectionRange(start,end) {this.selectionStart = start; this.selectionEnd = end;},
      remove() {this.removed = true;}};
    created.push(field); return field;
  },
  execCommand(command) {
    assert.equal(command,'copy');
    if (copyThrows) throw Error('Browser blocked copying');
    const field = document.activeElement;
    // Model the real browser bug: execCommand can return true for an inert field
    // without writing its contents. The active modal owns the usable selection.
    if (copyResult && field.value && (!dialog || field.parent === dialog)) clipboard = field.value;
    return copyResult;
  }};
const navigator = {};
const context = vm.createContext({document,navigator,topModalDialog:()=>dialog,t:key=>key,
  window:{prompt(message,text) {prompts.push({message,text});return null;}}});
vm.runInContext(fragment + '\nthis.copy = copyPlainText;', context);
const text = 'powershell.exe -NoProfile -EncodedCommand ' + 'A'.repeat(6000);
const modal = {append(field) {field.parent = this;}};
'''
        script += scenario + r'''
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_http_modal_and_denied_clipboard_use_full_focused_selection(self):
        self.run_scenario(r'''
(async () => {
  dialog = modal;
  for (const api of [undefined, {writeText:async () => {throw Error('NotAllowedError');}}]) {
    navigator.clipboard = api;
    clipboard = 'unchanged';
    assert.equal(await context.copy(text), true);
    assert.equal(clipboard, text, 'Copy the entire command from the active modal');
    assert.equal(document.activeElement, previous);
    assert.deepEqual(previous.restoredSelection,[2,5,'backward']);
    assert.equal(created.at(-1).removed,true);
  }
  dialog = null;
  assert.equal(await context.copy('playlist URL'),true);
  assert.equal(clipboard,'playlist URL');
  assert.equal(created.at(-1).parent,document.body);
  assert.equal(prompts.length,0);
''')

    def test_blocked_copy_keeps_text_available_without_claiming_success(self):
        self.run_scenario(r'''
(async () => {
  dialog = modal;
  for (const mode of ['false','exception','inert']) {
    copyResult = mode !== 'false'; copyThrows = mode === 'exception'; focusBlocked = mode === 'inert';
    assert.equal(await context.copy(text,'Run in cmd'),false);
    assert.equal(clipboard,'previous clipboard');
    assert.equal(prompts.at(-1).text,text);
    assert.equal(prompts.at(-1).message,'copyManually\n\nRun in cmd');
    assert.equal(created.at(-1).removed,true);
    assert.equal(document.activeElement,previous);
  }
''')

    def test_secure_clipboard_does_not_change_focus_or_show_manual_prompt(self):
        self.run_scenario(r'''
(async () => {
  navigator.clipboard = {writeText:async value => {clipboard = value;}};
  assert.equal(await context.copy(text),true);
  assert.equal(clipboard,text);
  assert.equal(document.activeElement,previous);
  assert.equal(created.length,0);
  assert.equal(prompts.length,0);
  await assert.rejects(context.copy(''), /copyPlaylist/);
''')


if __name__ == '__main__':
    unittest.main()
