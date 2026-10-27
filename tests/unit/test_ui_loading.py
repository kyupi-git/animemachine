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

    def test_catalog_prefetch_is_adopted_cancelled_and_shared_without_stale_results(self):
        start = SOURCE.index('const catalogPages =')
        fragment = SOURCE[start:SOURCE.index('const renderedContents =', start)]
        self.run_node("""const assert = require('node:assert/strict');
let authSession = {username:'first'}, calls = [];
const api = (url, options) => new Promise((resolve, reject) => {
  const request = {url, signal:options.signal, resolve, reject}; calls.push(request);
  options.signal.addEventListener('abort', () => reject(new DOMException('aborted','AbortError')), {once:true});
});
""" + fragment + """
(async () => {
  const prefetched = catalogPage('page=2', {preferCache:true});
  const rejectedPrefetch = assert.rejects(prefetched, {name:'AbortError'});
  const first = new AbortController(), second = new AbortController();
  const adopted = catalogPage('page=2', {signal:first.signal, preferCache:true});
  const adoptedRejected = assert.rejects(adopted, {name:'AbortError'});
  const shared = catalogPage('page=2', {signal:second.signal});
  const sharedRejected = assert.rejects(shared, {name:'AbortError'});
  assert.equal(calls.length,1,'identical pending queries share a request');
  first.abort(); assert.equal(calls[0].signal.aborted,false,'another visible caller still needs this page');
  second.abort(); assert.equal(calls[0].signal.aborted,true,'an adopted prefetch is cancellable');
  await Promise.all([rejectedPrefetch,adoptedRejected,sharedRejected]);
  const abandoned = catalogPage('page=3', {preferCache:true});
  const abandonedRejected = assert.rejects(abandoned,{name:'AbortError'});
  const controller = new AbortController();
  const visible = catalogPage('page=4',{signal:controller.signal});
  assert.equal(calls[1].signal.aborted,true,'obsolete prefetch never competes with a new page');
  calls[2].resolve({page:4}); assert.deepEqual(await visible,{page:4}); await abandonedRejected;
  assert.deepEqual(await catalogPage('page=4',{preferCache:true}),{page:4});
  assert.equal(calls.length,3);
  authSession = {username:'second'};
  const otherUser = catalogPage('page=4',{signal:new AbortController().signal});
  assert.equal(calls.length,4,'cached pages are private to the current account');
  invalidateCatalogPages(); calls[3].resolve({page:4,user:2}); await otherUser;
  assert.equal(catalogPages.size,0,'invalidated in-flight results never re-enter the cache');
  const failed = catalogPage('page=5',{signal:new AbortController().signal});
  const rejected = assert.rejects(failed,/offline/); calls[4].reject(Error('offline')); await rejected;
  assert.equal(catalogPages.size,0,'failed pages can be retried immediately');
})().catch(error => {console.error(error); process.exitCode = 1;});
""")

    def test_repeated_reset_and_superseded_page_cannot_leave_loading_or_replace_newer_results(self):
        start = SOURCE.index('async function search(')
        fragment = SOURCE[start:SOURCE.index('function reportPerformanceEvent(', start)]
        self.run_node("""const assert = require('node:assert/strict');
const nodes = new Map(), $ = id => {
  if (!nodes.has(id)) nodes.set(id,{disabled:false,hidden:true}); return nodes.get(id);
};
const results = {setAttribute(){},removeAttribute(){},querySelector:()=>null,insertAdjacentHTML(){}};
let catalogPrefetchTimer, catalogRefreshPending=false, searchController, items=[{page:-1}], total=120,
  page=0, displayedCatalogPage=0, pageSize='all', catalogPageRevision=0, searchExpanded=false, renderedResultsMarkup='', query='reset';
const topModalDialog=()=>null, params=()=>new URLSearchParams(query), render=()=>{}, t=x=>x, esc=String;
const calls=[],catalogPage=(query,options)=>new Promise((resolve,reject)=>calls.push({resolve,reject,signal:options.signal}));
""" + fragment + """
(async () => {
  const first=search(); await search(); assert.equal(calls.length,1,'repeated reset reuses its pending query');
  page=1;query='offset=12';const next=search();assert.equal(calls[0].signal.aborted,true);
  calls[1].resolve({items:[{page:1}],total:120});await next;
  calls[0].resolve({items:[{page:0}],total:120});await first;
  assert.deepEqual(items,[{page:1}],'late response cannot replace the requested page');
  page=2;query='offset=24';const failed=search();calls[2].reject(Error('offline'));await failed;
  assert.equal(page,1,'failed paging retains the page actually displayed');
  assert.equal($('catalogLoading').hidden,true);assert.equal(searchController,null);
  assert.equal($('prevPage').disabled,false);
  const retry=search();assert.equal(calls.length,4);calls[3].resolve({items:[],total:0});await retry;
  assert.equal($('catalogLoading').hidden,true);
})().catch(error => {console.error(error); process.exitCode = 1;});
""")

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
