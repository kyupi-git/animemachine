from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class LibraryDisplayTests(unittest.TestCase):
    def test_duplicate_paths_keep_useful_status_without_merging_distinct_folders(self):
        path = Path(__file__).parents[2] / 'src/animemachine/web/static/app.js'
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const block = source.slice(source.indexOf('function uniqueLibraryTargets('), source.indexOf('function clientVisiblePath('));
const context = vm.createContext({});
vm.runInContext(block + '\nthis.unique = uniqueLibraryTargets;', context);
const local = {path:'/Library/作品',state:'existing_downloaded',observedFiles:2};
const duplicate = {path:'/Library/作品/',state:'external',fileCount:2};
const media = {path:'/Media/作品',state:'external',fileCount:3};
const targets = [duplicate,local,media,{path:'/Library/作品',origin:'ani-rss_api'}];
const before = JSON.stringify(targets);
let rows = context.unique(targets);
assert.equal(rows.length,2);
assert.equal(rows[0],local,'Keep the existing local inventory for the same directory');
assert.equal(rows[1],media,'Different media locations stay separate');
assert.equal(JSON.stringify(targets),before,'Never change inventory or permissions');
rows = context.unique([{path:'/Library/Work',state:'absent'},
 {path:'/Library/Work',state:'external',fileCount:1}]);
assert.equal(rows[0].state,'external','A placeholder must not hide known media');
rows = context.unique([{path:'C:\\Anime\\Work',state:'external'},
 {path:'c:/anime/work/',state:'external'}]);
assert.equal(rows.length,1,'Windows spellings identify the same directory');
rows = context.unique([{path:'/Anime/Work'}, {path:'/Anime/work'},
 {path:'/Anime/A\\B'}, {path:'/Anime/A/B'}]);
assert.equal(rows.length,4,'Preserve case and literal backslashes in POSIX paths');
assert.equal(context.unique([{},{}, {path:'',origin:'ani-rss_api'}]).length,2);
'''
        result = subprocess.run(['node', '-e', script, str(path)], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
