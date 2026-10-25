"""Keep runtime connection values visible without persisting environment overrides."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

import httpx

from animemachine.catalog import service
from animemachine.config.policy import ConfigStore
from animemachine.config.runtime import apply_runtime_overrides, override_values


ROOT = Path(__file__).resolve().parents[2]


class DeploymentSettingsTests(unittest.TestCase):
    def test_provenance_includes_same_values_and_excludes_credentials(self):
        config = json.loads(service.EXAMPLE_CONFIG.read_text(encoding='utf-8'))
        original = json.dumps(config, sort_keys=True)
        with mock.patch.dict(os.environ, {'ANM_LIBRARY_DIR':config['deployment']['libraryUncRoot'],
                'ANM_MANAGED_QBITTORRENT_URL':'http://qbittorrent:8080',
                'ANM_ANI_RSS_URL':'http://ani-rss:7789','ANM_ANI_RSS_API_KEY':'never-expose',
                'ANM_ARCHIVE_MANIFEST_ENDPOINTS':'https://one.example; https://two.example'}, clear=True):
            values = override_values(config)
            self.assertEqual('http://qbittorrent:8080', values['components.downloadClient.endpoint'])
            self.assertEqual(config['deployment']['libraryUncRoot'], values['deployment.libraryUncRoot'])
            self.assertEqual(['https://one.example','https://two.example'], values['metadata.network.archiveManifestEndpoints'])
            self.assertNotIn('never-expose',json.dumps(values))
            self.assertEqual('http://ani-rss:7789',apply_runtime_overrides(config)['components']['aniRss']['endpoint'])
        self.assertEqual(original,json.dumps(config,sort_keys=True))

    def test_http_capabilities_limit_deployment_paths_to_admin(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {
                'ANM_AUTH_ENABLED':'false','ANM_AUTH_DB':str(Path(raw)/'auth.sqlite3'),
                'ANM_MANAGED_QBITTORRENT_URL':'http://qbittorrent:8080'}):
            root=Path(raw)
            database=root/'catalog.sqlite3'
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
                db.commit()
            store=ConfigStore(root/'config.json',service.EXAMPLE_CONFIG)
            server=ThreadingHTTPServer(('127.0.0.1',0),service.make_handler(database,store,start_warmup=False))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
                    data=client.get('/api/capabilities').raise_for_status().json()
                    self.assertEqual('http://qbittorrent:8080',data['configurationOverrides']['components.downloadClient.endpoint'])
                    from types import SimpleNamespace
                    with mock.patch.object(service.auth.Store,'from_cookie',return_value=SimpleNamespace(role='viewer',username='viewer',user_id=2)):
                        data=client.get('/api/capabilities').raise_for_status().json()
                    self.assertEqual({},data['configurationOverrides'])
                    original_connect = sqlite3.connect
                    def unavailable_catalog(path, *args, **kwargs):
                        if path == database:
                            raise sqlite3.OperationalError('disk I/O error')
                        return original_connect(path, *args, **kwargs)
                    with mock.patch.dict(os.environ, {'ANM_QBT_API_KEY': 'test-key'}), mock.patch.object(
                            service.sqlite3, 'connect', side_effect=unavailable_catalog):
                        response = client.post('/api/anime/1/torrents/search', json={})
                    self.assertEqual(503, response.status_code)
                    self.assertEqual({'error': 'catalog_storage_unavailable'}, response.json())
                    self.assertEqual(200, client.get('/api/auth/session').status_code)
            finally:
                server.shutdown();server.server_close();thread.join(2)

    @unittest.skipUnless(shutil.which('node'),'Node.js is required for UI behavior checks')
    def test_ui_displays_effective_addresses_and_keeps_persistent_fallbacks(self):
        source=ROOT/'src/animemachine/web/static/app.js'
        script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const fragment=source.slice(source.indexOf('const deploymentControls ='),source.indexOf('function openSettings()'));
const fields=new Map(), $=id=>{if(!fields.has(id))fields.set(id,{tagName:id==='aniRssMode'?'SELECT':'INPUT',
 value:'old',closest:()=>null,removeAttribute(){}});return fields.get(id);};
const config={components:{downloadClient:{endpoint:'http://127.0.0.1:8080'}},library:{sleep:{enabled:false}}};
const capabilities={configurationOverrides:{'components.downloadClient.endpoint':'http://qbittorrent:8080',
 'components.aniRss.mode':'prefer'}};
const context=vm.createContext({$,config,capabilities,t:x=>x});
vm.runInContext(fragment+'\nthis.apply=applyDeploymentSettings;this.retain=retainPersistentOverrides;',context);
context.apply();
assert.equal($('qbtEndpoint').value,'http://qbittorrent:8080');assert.equal($('qbtEndpoint').readOnly,true);
assert.equal($('aniRssMode').disabled,true);assert.equal($('aniRssEndpoint').readOnly,false);
const next={components:{downloadClient:{endpoint:$('qbtEndpoint').value},aniRss:{mode:'prefer'}},library:{sleep:{enabled:true}}};
context.retain(next);
assert.equal(next.components.downloadClient.endpoint,'http://127.0.0.1:8080');
assert.ok(!Object.hasOwn(next.components.aniRss,'mode'),'Environment defaults never become persistent settings');
assert.equal(next.library.sleep.enabled,true,'An unrelated user change is kept');
assert.equal(config.library.sleep.enabled,false,'The original configuration is not changed');
'''
        result=subprocess.run([shutil.which('node'),'-e',script,str(source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
