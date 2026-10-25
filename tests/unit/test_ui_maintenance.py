"""Per-work verification must finish or recover its button after request failures."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class MaintenanceUITests(unittest.TestCase):
    def test_work_audit_waits_for_completion_and_recovers_after_failure_or_close(self):
        source = Path(__file__).resolve().parents[2] / 'src/animemachine/web/static/app.js'
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('async function verifyWorkLibrary('),source.indexOf('function interpolate('));
const dialog={open:true};let failPost=false,replies=[],rendered=0,messages=[];
const context=vm.createContext({$:()=>dialog,detailGeneration:1,t:key=>key,localizedError:String,
 document:{createElement:()=>({setAttribute:()=>{}})},
 setTimeout:fn=>{fn();},showDetail:async id=>{assert.equal(id,7);rendered++;},
 api:async(url,options)=>{if(options?.method==='POST'){if(failPost)throw new Error('offline');return {started:true};}
   return {library:replies.shift()};}});
vm.runInContext(code,context);
const button=()=>({disabled:false,textContent:'Verify',parentElement:{querySelector:()=>null},
 insertAdjacentElement:(position,message)=>messages.push(message.textContent)});
(async()=>{
 replies=[{state:'running'},{state:'complete'}];let control=button();await context.verifyWorkLibrary(control,7,1);
 assert.equal(rendered,1);assert.equal(control.disabled,false);assert.equal(control.textContent,'Verify');
 failPost=true;control=button();await context.verifyWorkLibrary(control,7,1);
 assert.equal(control.disabled,false);assert.equal(messages.at(-1),'offline');assert.equal(rendered,1);
 failPost=false;replies=[{state:'idle'}];await context.verifyWorkLibrary(button(),7,1);
 assert.equal(messages.at(-1),'maintenanceFailed');assert.equal(rendered,1);
 replies=[{state:'failed',error:'storage sleeping'}];await context.verifyWorkLibrary(button(),7,1);
 assert.equal(messages.at(-1),'storage sleeping');
 dialog.open=false;await context.verifyWorkLibrary(button(),7,1);assert.equal(rendered,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
