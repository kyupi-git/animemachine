"""Exercise browser loading behavior with controlled asynchronous responses."""
import shutil
import subprocess
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[2] / 'src/animemachine/web/static/app.js').read_text(encoding='utf-8')


class UiLoadingTests(unittest.TestCase):
    def run_node(self, script):
        subprocess.run([shutil.which('node') or 'node', '-e', script], check=True,
                       capture_output=True, text=True, timeout=10)

    def test_api_timeout_covers_response_body_and_external_cancel_signal(self):
        start = SOURCE.index('const api = async')
        fragment = SOURCE[start:SOURCE.index('  languageBase =', start)].rstrip().removesuffix(',') + ';'
        script = """const assert = require('node:assert/strict');
const localizedError = x => x, csrfToken = '', showLogin = () => {};
let fetch = async (url, options) => ({ok:true, text:() => new Promise((resolve,reject) => {
  options.signal.addEventListener('abort', () => reject(new DOMException('aborted','AbortError')), {once:true});
})});
""" + fragment + """
(async () => {
  const external = new AbortController();
  for (const options of [{timeoutMs:15}, {timeoutMs:15, signal:external.signal}]) {
    let watchdog;
    try {
      await assert.rejects(Promise.race([api('/api/slow', options), new Promise((resolve,reject) => {
        watchdog = setTimeout(() => reject(Error('body read never timed out')), 1000);
      })]), error => error.code === 'request_timeout');
    } finally { clearTimeout(watchdog); }
  }
  assert.equal(external.signal.aborted, false);
  fetch = async (url, options) => {
    assert.ok(options.signal, 'GET requests have a default deadline');
    return {ok:true, text:async () => '{}'};
  };
  assert.deepEqual(await api('/api/ready'), {});
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        self.run_node(script)

    def test_diagnostics_render_without_waiting_for_slow_optional_panel(self):
        start = SOURCE.index('let diagnosticTimer')
        fragment = SOURCE[start:SOURCE.index('function renderScanProgress', start)]
        script = """const assert = require('node:assert/strict');
const nodes = new Map(), rendered = [], timers = [];
const $ = id => {if (!nodes.has(id)) nodes.set(id,{open:true,innerHTML:''}); return nodes.get(id);};
const document = {querySelector:() => ({classList:{contains:() => true}})};
const esc = x => String(x), clearTimeout = () => {}, setTimeout = fn => timers.push(fn);
const renderStartupDiagnostics = x => rendered.push('startup');
const renderSystemHealthDiagnostics = x => rendered.push('health');
const renderNetworkDiagnostics = x => rendered.push('network');
const renderImagePreload = x => rendered.push('images');
const renderPlaybackDiagnostics = x => rendered.push('playback');
let rejectSlow;
const api = (url, options) => {
  assert.equal(options.timeoutMs,10000);
  return url === '/api/diagnostics/network' ? new Promise((resolve,reject) => {rejectSlow=reject;}) : Promise.resolve({});
};
""" + fragment + """
(async () => {
  const loading = loadDiagnostics();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(rendered.sort(), ['health','images','playback','startup']);
  $('settingsDialog').open = false;
  rejectSlow(Error('request_timeout'));
  await loading;
  assert.ok($('networkDiagnostics').innerHTML.includes('request_timeout'));
  assert.equal(timers.length,0,'closed diagnostics never schedule another poll');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        self.run_node(script)
