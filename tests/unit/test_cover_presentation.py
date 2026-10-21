from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for cover presentation checks')
class CoverPresentationTests(unittest.TestCase):
    def test_loading_polls_preserve_existing_paint_and_decode_before_replacing_cover(self):
        path = Path(__file__).parents[2] / 'src/animemachine/web/static/app.js'
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const created=[],revoked=[],writes=[],timers=[];
let decodeResolve,decodeReject,manualDecode=false,response;
class Image {decode(){return manualDecode ? new Promise((resolve,reject)=>{decodeResolve=resolve;decodeReject=reject;}) : Promise.resolve();}}
const context=vm.createContext({Image,URL:{createObjectURL(){const url=`blob:${created.length}`;created.push(url);return url;},revokeObjectURL(url){revoked.push(url);}},
 setTimeout(fn){timers.push(fn);return fn;},clearTimeout(){},topModalDialog:()=>null,t:key=>key,
 reportPerformanceEvent(){},fetch:()=>Promise.resolve(response),coverVersion:0,
 coverBatch:null,coverObserver:null,
});
const block=source.slice(source.indexOf('function setCoverState('),source.indexOf('function predictiveCoverWindow('));
vm.runInContext(block+'\nthis.apply=applyCoverBlob;this.pump=pumpCoverLoads;',context);
const batch={active:0,queue:[],controller:{signal:{}},objectUrls:new Map(),timers:new Set()};
context.coverBatch=batch;
const target={isConnected:true,tagName:'ARTICLE',dataset:{cover:'12'},classList:{toggle(){}},
 style:{setProperty(key,value){writes.push([key,value]);}}};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 response={ok:true,blob:async()=>({}),headers:{get:key=>key==='X-AnimeMachine-Image-Status'?'loading':null}};
 for(let count=0;count<3;count++){
   target.dataset.coverQueued='1';batch.queue.push(target);context.pump(batch);await settle();
 }
 assert.equal(created.length,0,'Polling placeholders must not create or replace background images');
 assert.equal(writes.length,0,'Loading one cover must not repaint other cards');
 response={ok:true,blob:async()=>({}),headers:{get:key=>({'X-AnimeMachine-Image-Status':'available','ETag':'cover-v1'})[key]||null}};
 batch.queue.push(target);context.pump(batch);await settle();
 assert.equal(created.length,1);assert.equal(writes.length,1);assert.equal(target.dataset.coverState,'available');
 batch.queue.push(target);context.pump(batch);await settle();
 assert.equal(created.length,1,'An unchanged ETag preserves the displayed cover');
 manualDecode=true;
 const old=target.dataset.coverObjectUrl,pending=context.apply(target,{},batch);
 await settle();
 assert.equal(target.dataset.coverObjectUrl,old,'Keep the old cover while decoding');
 assert.equal(revoked.includes(old),false);
 decodeResolve();await pending;
 assert.notEqual(target.dataset.coverObjectUrl,old);assert.equal(revoked.includes(old),true);
 const good=target.dataset.coverObjectUrl,failed=context.apply(target,{},batch);
 decodeReject(new Error('bad image'));await assert.rejects(failed,/bad image/);
 assert.equal(target.dataset.coverObjectUrl,good,'A corrupt replacement must keep the last valid image');
 const abandoned=context.apply(target,{},batch);target.isConnected=false;decodeResolve();await abandoned;
 assert.equal(target.dataset.coverObjectUrl,good,'A delayed response must not update a removed card');
 assert.equal(batch.objectUrls.size,1,'Discard unused object URLs');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node','-e',script,str(path)],capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)


if __name__ == '__main__':
    unittest.main()
