"""An explicit year selection covers the whole year, including after a season."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class YearFilterTests(unittest.TestCase):
    def test_year_change_clears_season_and_replaces_partial_or_cross_year_range(self):
        source = Path(__file__).resolve().parents[2] / 'src/animemachine/web/static/app.js'
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const fields={season:{value:'winter'},start_from:{value:'2025-12'},start_to:{value:'2026-02'}};
const context=vm.createContext({$:id=>fields[id],exactYear:value=>/^\d{4}$/.test(value)?Number(value):null,
 ensureEraYearOption:()=>{},updateSeasonControl:()=>{},eraDateRange:()=>({from:'2000-01',to:'2009-12'})});
vm.runInContext(source.slice(source.indexOf('function applyEraDateRange('),source.indexOf('function applySeasonDateRange('))+'\nthis.apply=applyEraDateRange;',context);
context.apply('2012');
assert.equal(fields.season.value,'');assert.equal(fields.start_from.value,'2012-01');assert.equal(fields.start_to.value,'2012-12');
fields.start_from.value='2012-06';fields.start_to.value='2012-08';context.apply('2013');
assert.equal(fields.start_from.value,'2013-01');assert.equal(fields.start_to.value,'2013-12');
context.apply('2000s');assert.equal(fields.start_from.value,'2000-01');assert.equal(fields.start_to.value,'2009-12');
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
