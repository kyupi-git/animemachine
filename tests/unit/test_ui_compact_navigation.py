"""Compact work actions and connection feedback must preserve their existing meaning."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class CompactNavigationUITests(unittest.TestCase):
    def run_script(self, script):
        source = Path(__file__).resolve().parents[2] / 'src/animemachine/web/static/app.js'
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_paths_distinguish_unsaved_disabled_sleeping_and_inaccessible_locations(self):
        self.run_script(r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('const connectionPathKeys ='),source.indexOf('function systemHealthExtra('));
const fields=new Map(), $=id=>{if(!fields.has(id))fields.set(id,{value:'',checked:false,dataset:{},addEventListener(){}});return fields.get(id);};
const config={deployment:{libraryUncRoot:'\\\\NAS\\Anime',torrentPoolRoot:'/Torrents'},components:{aniRss:{mediaPath:'/Media'}},
 externalLibraries:[{path:'/External',enabled:false}]};
const deploymentControls={'deployment.libraryUncRoot':'libraryPath','deployment.torrentPoolRoot':'torrentPoolPath',
 'components.aniRss.mediaPath':'aniRssMediaPath','externalLibraries.0.path':'externalReadOnlyPath'};
const capabilities={configurationOverrides:{'deployment.torrentPoolRoot':'/ManagedTorrents'}};
$('libraryPath').value=config.deployment.libraryUncRoot;$('torrentPoolPath').value='/ManagedTorrents';
$('aniRssMediaPath').value='/Media';$('externalReadOnlyPath').value='/External';
const context=vm.createContext({$,config,capabilities,deploymentControls,t:key=>key});
vm.runInContext(code+'\nthis.render=renderConnectionPaths;',context);
const storage={library:{path:config.deployment.libraryUncRoot,state:'available'},
 torrentPool:{path:'/ManagedTorrents',state:'sleeping'},aniRssMedia:{path:'/Media',state:'permission_denied'}};
context.render(storage);
assert.equal($('libraryPathStatus').textContent,'pathReadable');
assert.equal($('torrentPoolPathStatus').textContent,'librarySleeping');
assert.equal($('aniRssMediaPathStatus').textContent,'pathPermissionDenied');
assert.equal($('externalReadOnlyPathStatus').dataset.state,'disabled');
$('libraryPath').value='D:\\NewAnime';context.render();
assert.equal($('libraryPathStatus').dataset.state,'pending');
$('externalReadOnlyEnabled').checked=true;context.render();
assert.equal($('externalReadOnlyPathStatus').dataset.state,'pending');
$('libraryPath').value=config.deployment.libraryUncRoot;
context.render({...storage,library:{path:'D:\\OldAnime',state:'available'}});
assert.equal($('libraryPathStatus').textContent,'pathNotChecked');
context.render(null);assert.equal($('aniRssMediaPathStatus').textContent,'pathNotChecked');
config.deployment.libraryUncRoot='';$('libraryPath').value='';context.render();
assert.equal($('libraryPathStatus').dataset.state,'not_configured');
''')

    def test_combined_follow_entry_opens_existing_mode_and_keeps_radar_mode_actions(self):
        self.run_script(r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('function followButton('),source.indexOf('const followStateLabel'));
const context=vm.createContext({t:key=>key,esc:String});
vm.runInContext(code+'\nthis.button=followButton;',context);
for(const state of ['pending','available','subscribed']) {
 const item={id:12,follows:[{mode:'serial',state:'cancelled'},{mode:'archive',state}]};
 const html=context.button(item,'all');
 assert.ok(html.includes('data-follow-mode="archive"'));assert.ok(html.includes('>followWork</button>'));
 assert.ok(context.button(item).includes('data-follow-mode="serial"'));
}
assert.ok(context.button({id:12},'all').includes('data-follow-mode="serial"'));
assert.ok(context.button({id:12},'archive').includes('>followArchive</button>'));
''')

    def test_mobile_sort_uses_the_same_global_sort_and_can_restore_default(self):
        self.run_script(r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('let radarSort ='),source.indexOf('const radarPendingSubscriptions'));
const fields=new Map(), $=id=>{if(!fields.has(id))fields.set(id,{value:''});return fields.get(id);};
let loads=0;const context=vm.createContext({$,loadRadar:()=>loads++});
vm.runInContext(code+'\nthis.update=updateRadarSortControls;this.state=()=>[radarSort,radarDirection];',context);
context.update();assert.equal($('radarMobileDirection').disabled,true);
$('radarMobileDirection').onclick();assert.equal(loads,0);
$('radarMobileSort').value='updated';$('radarMobileSort').onchange();
assert.equal(JSON.stringify(context.state()),JSON.stringify(['updated','asc']));
$('radarMobileDirection').onclick();assert.equal(JSON.stringify(context.state()),JSON.stringify(['updated','desc']));
$('radarMobileSort').value='';$('radarMobileSort').onchange();
assert.equal($('radarMobileDirection').disabled,true);assert.equal(loads,3);
''')
