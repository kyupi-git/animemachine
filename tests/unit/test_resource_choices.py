from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI behavior checks')
class ResourceChoicesTests(unittest.TestCase):
    def test_variants_preserve_exact_selection_policy_and_episode_grouping(self):
        path = Path(__file__).parents[2] / 'src/animemachine/web/static/app.js'
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const block = source.slice(source.indexOf('  subtitleText = (value)'), source.indexOf('\nfunction graphTitle('));
const context = vm.createContext({});
vm.runInContext(`const torrentSelections = new Map(), resourceSelections = new Map(), policyOverrides = new Map();
  const t = (key) => key, fmt = String, esc = String, sourceClassText = String, li = () => 0;
  const ${block}
  globalThis.api = {resourceChoices, resourceChoiceHtml, torrentSelections, resourceSelections, policyOverrides};`, context);
const api = context.api;
const torrent = (hash, subtitle, episode, extra={}) => ({infoHash:hash,name:hash,resourceGroup:'Group',sourceFamily:'serial',
  sourceClass:'webrip',resolution:1080,subtitle,scan:'p',bitDepth:8,releaseUnit:'episode',episodeSequence:[episode],
  eligible:true,effectiveRank:episode,fileCount:1,...extra});
api.torrentSelections.set(1,'second-episode');
const grouped = api.resourceChoices([torrent('first-episode','CHS',1),torrent('second-episode','CHS',2),
  torrent('traditional','CHT',1)], [{resourceId:'rss-chs',resourceGroup:'Group',eligible:true,title:'CHS'},
  {resourceId:'rss-cht',resourceGroup:'Group',eligible:true,title:'CHT'}],1);
assert.equal(grouped.length,2,'Providers stay separate');
assert.equal(grouped[0].variants.length,2,'Nonoverlapping episodes stay one variant; language versions stay distinct');
assert.equal(grouped[0].variants[grouped[0].current].value,'torrent:second-episode','An exact selected member survives refresh');
assert.equal(grouped[1].variants.length,2,'Ani-RSS results share a subtitle-group row');
assert.ok(api.resourceChoiceHtml(grouped[0],1,0).includes('value="torrent:second-episode"'));
api.resourceSelections.set(2,'rss-excluded');
const policy = api.resourceChoices([], [{resourceId:'rss-blocked',resourceGroup:'G',eligible:false,title:'blocked'},
  {resourceId:'rss-excluded',resourceGroup:'G',eligible:false,canOverride:true,title:'CHT',eligibilityReason:'serial_language_mismatch'}],2)[0];
assert.equal(policy.variants[policy.current].value,'ani-rss:rss-excluded');
let html = api.resourceChoiceHtml(policy,2,0);
assert.ok(html.includes('data-policy-override="true"'));
assert.ok(!html.includes(' checked'),'Selecting an excluded resource never silently enables an override');
api.policyOverrides.set(2,'ani-rss:rss-excluded');
assert.ok(api.resourceChoiceHtml(policy,2,0).includes(' checked'));
const unknown = api.resourceChoices([], [{resourceId:'a',eligible:true},{resourceId:'b',eligible:true}],3);
assert.equal(unknown.length,2,'Unknown groups do not merge unrelated resources');
assert.ok(!api.resourceChoiceHtml(unknown[0],3,0).includes(' checked'),'No implicit resource selection');
console.log('Resource identity, variants, policy overrides and episode grouping passed');
'''
        result = subprocess.run(['node', '-e', script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
