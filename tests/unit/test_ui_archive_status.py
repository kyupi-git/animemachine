"""Archive progress follows server state, including scheduled work and transient failures."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class ArchiveStatusUITests(unittest.TestCase):
    def test_scheduled_completion_recovery_and_close_do_not_leave_stale_progress(self):
        source = Path(__file__).resolve().parents[2] / 'src/animemachine/web/static/app.js'
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('function archiveText('),source.indexOf('async function importArchive('));
const timers=new Map(),listeners={},nodes={archiveStatus:{textContent:''},updateArchive:{disabled:false},
 settingsDialog:{open:true,addEventListener:(event,fn)=>listeners[event]=fn}};
let serial=0,calls=0,deferred,reply={state:'checking'};
const context=vm.createContext({$:id=>nodes[id],t:key=>key,fmt:String,localizedError:String,
 document:{addEventListener:()=>{}},
 clearTimeout:id=>timers.delete(id),setTimeout:(fn,delay)=>{const id=++serial;timers.set(id,{fn,delay});return id;},
 api:async()=>{calls++;if(deferred)return deferred;if(reply instanceof Error)throw reply;return reply;}});
vm.runInContext(code,context);
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
const tick=async()=>{assert.equal(timers.size,1);const [id,timer]=[...timers][0];timers.delete(id);timer.fn();await flush();};
(async()=>{
 context.startArchiveStatusPolling();await flush();
 assert.equal(nodes.archiveStatus.textContent,'archiveChecking');assert.equal(nodes.updateArchive.disabled,true);
 assert.equal([...timers.values()][0].delay,3000);
 reply={state:'unchanged'};await tick();
 assert.equal(nodes.archiveStatus.textContent,'archiveUnchanged');assert.equal(nodes.updateArchive.disabled,false);
 assert.equal([...timers.values()][0].delay,15000);
 reply={state:'building'};await tick();assert.equal(nodes.updateArchive.disabled,true);
 reply=new Error('connection lost');await tick();assert.equal(nodes.archiveStatus.textContent,'connection lost');
 assert.equal(nodes.updateArchive.disabled,false);assert.equal([...timers.values()][0].delay,10000);
 reply={state:'complete',addedWorks:2};await tick();assert.equal(nodes.archiveStatus.textContent,'archiveComplete+2');
 assert.equal(nodes.updateArchive.disabled,false);
 let resolve;deferred=new Promise(done=>resolve=done);await tick();assert.equal(timers.size,0);
 const previous=nodes.archiveStatus.textContent;nodes.settingsDialog.open=false;listeners.close();
 resolve({state:'checking'});await flush();assert.equal(nodes.archiveStatus.textContent,previous);
 assert.equal(timers.size,0);
 deferred=null;reply={state:'unchanged'};nodes.settingsDialog.open=true;
 context.startArchiveStatusPolling();await flush();assert.equal(nodes.archiveStatus.textContent,'archiveUnchanged');
 assert.equal(timers.size,1);assert(calls>=7);
 nodes.settingsDialog.open=false;listeners.close();assert.equal(timers.size,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
