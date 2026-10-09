"""Behavioral regression tests for release radar and its persisted episode evidence."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from animemachine.catalog import archive_update, service
from animemachine.integrations import ani_rss

ROOT = Path(__file__).resolve().parents[2]


def archive_with_rows(path, rows, episodes=None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("subject.jsonlines", "\n".join(json.dumps(row, ensure_ascii=False) for row in rows))
        for name in ("subject-persons", "subject-characters", "person-characters", "subject-relations", "person", "character"):
            archive.writestr(name + ".jsonlines", "")
        archive.writestr("episode.jsonlines", "\n".join(json.dumps(row, ensure_ascii=False) for row in (episodes or [])))


def synthetic_archive(path, count=5):
    rows = [{"id": index, "type": 2, "name": f"作品 {index}", "name_cn": f"动画 {index}",
             "date": "2026-07-01", "platform": 1, "infobox": "", "tags": [{"name": "日本动画"}], "summary": ""}
            for index in range(1, count + 1)]
    archive_with_rows(path, rows)


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "catalog.sqlite3"
        archive = Path(self.tmp.name) / "archive.zip"
        synthetic_archive(archive)
        with contextlib.redirect_stdout(None):
            service.write_database(self.db_path, service.build_items_from_archive(archive, None, {}))
        self.config = service.ConfigStore(Path(self.tmp.name) / "config.json", service.EXAMPLE_CONFIG).read()
        self.ready = {"connection_state": "ready", "credentialConfigured": True, "effective_mode": "prefer"}

    def query(self, **overrides):
        params = {"sort": ["recent_episode"], "radar": ["1"], "start_from": ["2026-06"], "start_to": ["2026-08"],
                  "limit": ["50"], **overrides}
        with mock.patch.object(ani_rss, "state", return_value=self.ready):
            return service.query_catalog(self.db_path, params, self.config)

    def test_radar_keeps_all_tv_before_movies_even_without_updates_or_connection(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie', start_month='2026-08' WHERE id=1")
        for ready in (self.ready, {"connection_state": "failed", "credentialConfigured": False}):
            with self.subTest(ready=ready), mock.patch.object(ani_rss, "state", return_value=ready):
                result = service.query_catalog(self.db_path, {"radar": ["1"], "sort": ["recent_episode"]}, self.config)
                self.assertEqual("movie", result["items"][-1]["media_code"])

    def test_radar_groups_translated_media_then_untranslated_media(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie' WHERE id IN (3,4)")
            db.execute("UPDATE anime_work SET media_code='ova' WHERE id=5")
            db.execute("UPDATE anime_work SET title_zh_hans=NULL,title_en=NULL WHERE id IN (2,4)")
            db.execute("DELETE FROM anime_title WHERE anime_id IN (2,4) AND language<>'ja'")
        for ready in (self.ready, {"connection_state": "failed", "credentialConfigured": False}):
            for order in ("recent_episode", "date", "random"):
                with self.subTest(ready=ready, sort=order), mock.patch.object(ani_rss, "state", return_value=ready):
                    rows = service.query_catalog(self.db_path, {"radar": ["1"], "sort": [order], "limit": ["all"]}, self.config)["items"]
                    self.assertEqual(1, rows[0]["id"])
                    self.assertEqual({3, 5}, {row["id"] for row in rows[1:3]})
                    self.assertEqual([2, 4], [row["id"] for row in rows[-2:]])
                    first = service.query_catalog(self.db_path, {"radar": ["1"], "sort": [order], "limit": ["2"]}, self.config)["items"]
                    self.assertEqual([row["id"] for row in rows[:2]], [row["id"] for row in first])

    def test_radar_premiere_sort_orders_the_whole_season_before_pagination(self):
        archive = Path(self.tmp.name) / "many.zip"
        synthetic_archive(archive, count=65)
        self.db_path.unlink()
        with contextlib.redirect_stdout(None):
            service.write_database(self.db_path, service.build_items_from_archive(archive, None, {}))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            for anime_id in range(1, 66):
                date = dt.date(2026, 6, 1) + dt.timedelta(days=anime_id - 1)
                db.execute("UPDATE anime_work SET raw_date=?,start_month=? WHERE id=?",
                           (date.isoformat(), date.strftime("%Y-%m"), anime_id))
            db.execute("UPDATE anime_work SET media_code='movie',title_zh_hans=NULL,title_en=NULL WHERE id=1")
            db.execute("DELETE FROM anime_title WHERE anime_id=1 AND language<>'ja'")
        for ready in (self.ready, {"connection_state": "unconfigured", "credentialConfigured": False}):
            for direction in ("asc", "desc"):
                with self.subTest(ready=ready, direction=direction):
                    self.ready = ready
                    params = {"sort": ["premiere"], "radar_grouped": ["0"], "direction": [direction]}
                    first = self.query(**params)["items"]
                    second = self.query(**params, offset=["50"])["items"]
                    expected = list(range(1, 66))
                    if direction == "desc":
                        expected.reverse()
                    self.assertEqual(expected, [row["id"] for row in first + second])
                    self.assertEqual(expected, [row["id"] for row in self.query(**params, limit=["all"])["items"]])

    def test_radar_headers_sort_progress_subscription_and_update_evidence(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.executemany("""INSERT INTO ani_rss_subscription
                (remote_id,anime_id,title,enabled,subscription_kind,current_episode,remote_state,
                 first_seen_at,last_seen_at,evidence_json)
                VALUES(?,?,?,?,'follow',?,'enabled','now','now','{}')""", [
                ('active', 1, 'Active', 1, 76), ('paused', 2, 'Paused', 0, 3),
            ])
            db.execute("UPDATE anime_work SET episode_count=12,episode_start_number=73 WHERE id=1")
            ani_rss.record_release_progress(db, 3, 1, "2026-07-02T00:00:00Z",
                                            published_at="2026-07-01T00:00:00Z")
            ani_rss.record_release_progress(db, 4, 2, "2026-07-03T00:00:00Z",
                                            published_at="2026-07-02T00:00:00Z")
        rows = self.query(sort=["progress"], direction=["desc"], radar_grouped=["0"])["items"]
        self.assertEqual([1, 2, 4, 3, 5], [row["id"] for row in rows])
        self.assertEqual(4, rows[0]["episode_progress"]["current"])
        rows = self.query(sort=["subscription"], radar_grouped=["0"])["items"]
        self.assertEqual([1, 2], [row["id"] for row in rows[:2]])
        rows = self.query(sort=["updated"], direction=["desc"], radar_grouped=["0"])["items"]
        self.assertEqual([4, 3], [row["id"] for row in rows[:2]])
        self.ready = {"connection_state": "unconfigured", "credentialConfigured": False}
        for column in ("progress", "subscription", "updated", "title", "type"):
            with self.subTest(column=column):
                self.assertEqual(5, len(self.query(sort=[column], radar_grouped=["0"])["items"]))

    def add_subscription(self, anime_id, *, state="submitted", kind="follow"):
        remote_id = f"subscription-{anime_id}"
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""INSERT INTO ani_rss_subscription
                (remote_id,anime_id,title,enabled,subscription_kind,current_episode,remote_state,
                 first_seen_at,last_seen_at,evidence_json)
                VALUES(?,?,?,1,'follow',1,'enabled','now','now','{}')""", (remote_id, anime_id, remote_id))
            db.execute("INSERT INTO ani_rss_action VALUES(?,?,?,?,?,?,?,'same-second','same-second',NULL,'{}')",
                       (remote_id, remote_id, anime_id, remote_id, remote_id, kind, state))

    def test_new_subscriptions_stay_first_across_seeds_pages_and_connection_loss(self):
        self.add_subscription(2)
        self.add_subscription(3, state="failed")
        self.add_subscription(4, kind="collection")
        self.add_subscription(5)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET title_zh_hans=NULL,title_en=NULL WHERE id=5")
            db.execute("DELETE FROM anime_title WHERE anime_id=5 AND language<>'ja'")
            ani_rss.record_release_progress(db, 1, 9, "2026-07-04T00:00:00Z",
                                            published_at="2026-07-04T00:00:00Z")
        for ready in (self.ready, {"connection_state": "error", "credentialConfigured": True}):
            self.ready = ready
            for sort in ("recent_episode", "random"):
                for seed in ("first-seed", "second-seed"):
                    with self.subTest(ready=ready, sort=sort, seed=seed):
                        params = {"radar": ["0"], "sort": [sort], "seed": [seed]}
                        first = self.query(**params, limit=["2"])["items"]
                        remaining = self.query(**params, offset=["2"])["items"]
                        self.assertEqual([5, 2], [row["id"] for row in first])
                        self.assertEqual({1, 3, 4}, {row["id"] for row in remaining})
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET start_month='2026-08' WHERE id=1")
        for sort in ("recent_episode", "random"):
            rows = self.query(radar=["0"], sort=[sort], start_from=["2026-08"], start_to=["2026-08"])["items"]
            self.assertEqual([1], [row["id"] for row in rows])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE ani_rss_subscription SET deleted_at='deleted' WHERE anime_id=5")
        self.assertNotIn(5, {row["id"] for row in self.query(radar=["0"])["items"]})

    def test_archive_translation_moves_a_work_automatically_and_preserves_subscription_order(self):
        self.add_subscription(5)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie' WHERE id=3")
            db.execute("UPDATE anime_work SET title_zh_hans=NULL,title_en=NULL WHERE id=2")
            db.execute("DELETE FROM anime_title WHERE anime_id=2 AND language<>'ja'")
        before = [row["id"] for row in self.query()["items"]]
        self.assertGreater(before.index(2), before.index(3))
        self.assertNotIn(2, {row["id"] for row in self.query(radar=["0"])["items"]})
        archive = Path(self.tmp.name) / "translated.zip"
        incoming = Path(self.tmp.name) / "incoming.sqlite3"
        synthetic_archive(archive)
        with contextlib.redirect_stdout(None):
            service.write_database(incoming, service.build_items_from_archive(archive, None, {}),
                                   {"name": "new.zip", "digest": "sha256:new", "created_at": "2026-10-02T00:00:00Z"})
        with contextlib.closing(sqlite3.connect(incoming)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie' WHERE bgm_id=3")
        archive_update.merge_metadata(self.db_path, incoming, service)
        after = [row["id"] for row in self.query()["items"]]
        self.assertLess(after.index(2), after.index(3))
        self.assertEqual(5, self.query(radar=["0"])["items"][0]["id"])
        self.assertIn(2, {row["id"] for row in self.query(radar=["0"])["items"]})

    def test_image_progress_hides_pre_1980_dates_in_all_languages(self):
        source = (ROOT / "src/animemachine/web/static/app.js").read_text(encoding="utf-8")
        def function(name):
            start = source.index("function " + name + "(")
            return source[start:source.index("\n}", start) + 2]
        script = """const assert = require('node:assert/strict');
let language = 'zh-Hans', startupState = null;
const elements = new Map(), $ = (id) => {
  if (!elements.has(id)) elements.set(id, {classList: {toggle() {}}, removeAttribute() {}});
  return elements.get(id);
};
const t = (key) => key === 'imagesPreparedThrough' ? 'through {month}' : key;
const localizedMonth = String;
""" + function("monthParts") + function("renderScanProgress") + """
for (language of ['zh-Hans', 'en', 'ja']) {
  for (const month of ['2026-10', '1980-01']) {
    renderScanProgress({}, {state:'Warming', preload:{state:'warming', preparedThroughMonth:month}});
    assert.ok($('scanProgressText').textContent.includes(month));
  }
  for (const month of ['1979-12', '1892-10']) {
    renderScanProgress({}, {state:'Warming', preload:{state:'warming', preparedThroughMonth:month}});
    assert.equal($('scanProgressText').textContent, 'backgroundImagesComplete');
  }
  renderScanProgress({}, {state:'Warm', preload:{state:'warm', preparedThroughMonth:'1980-01'}});
  assert.equal($('scanProgressText').textContent, 'backgroundImagesComplete');
  assert.equal($('scanProgressBar').value, 100);
}
"""
        subprocess.run([shutil.which("node") or "node", "-e", script], check=True, capture_output=True, text=True)

    def test_catalog_poll_refreshes_after_archive_merge_and_random_subscription_sync(self):
        source = (ROOT / "src/animemachine/web/static/app.js").read_text(encoding="utf-8")
        start = source.index("let syncSummaryTimer,")
        fragment = source[start:source.index("function setRecentDates(", start)]
        script = """const assert = require('node:assert/strict');
let stats = {record_count:5, archive_name:'same.zip', archive_digest:'first', built_at:'same-time'};
let ani = {successful_generation:1}, startupState = null, lastAniRssGeneration = null, sort = 'random';
let searches = [], radarLoads = 0;
let options = {};
const filters = {era:{value:'2026'}, studio:{value:'Unsubmitted selection'}};
const fill = (id) => { if (filters[id]) filters[id].value = ''; };
const personSuggestions = () => {}, renderMediaChecks = () => {};
const dialog = {open:false}, $ = (id) => id === 'radarDialog' ? dialog : {};
const window = {location:{reload:() => {throw Error('unexpected page reload');}}};
const api = async (path) => path === '/api/stats' ? stats : path === '/api/ani-rss/status' ? ani : {};
const search = async (options) => {searches.push(options);};
const loadRadar = () => {radarLoads++;};
const applyRecentEpisodeSortAvailability = () => {}, archiveSummary = () => '', renderScanProgress = () => {};
const setTimeout = () => 1, clearTimeout = () => {};
""" + fragment + """
(async () => {
  await pollSyncSummary();
  assert.equal(searches.length, 0);
  stats.archive_digest = 'second';
  await pollSyncSummary();
  assert.deepEqual(searches, [{background:true}]);
  assert.equal(filters.era.value, '2026');
  assert.equal(filters.studio.value, 'Unsubmitted selection');
  await pollSyncSummary();
  assert.equal(searches.length, 1);
  dialog.open = true;
  stats.archive_digest = 'third';
  await pollSyncSummary();
  assert.equal(searches.length, 2);
  assert.equal(radarLoads, 1);
  ani.successful_generation = 2;
  await pollSyncSummary();
  assert.equal(searches.length, 3);
})().catch(error => {console.error(error);process.exitCode=1;});
"""
        subprocess.run([shutil.which("node") or "node", "-e", script], check=True, capture_output=True, text=True)

    def test_untranslated_chinese_originals_are_excluded_without_title_blacklists(self):
        titles = ("新大头儿子和小头爸爸之天眼系列：失控的第七天", "小猪佩奇·完美假期", "未来新增的中文作品")
        for title in titles:
            with self.subTest(title=title), contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute("DELETE FROM anime_country WHERE anime_id=5")
                db.execute("INSERT INTO anime_country VALUES(5,'OTHER','insufficient_country_evidence')")
                db.execute("UPDATE anime_work SET title_ja=?,title_zh_hans=?,title_en=NULL,original_language='ja' WHERE id=5", (title, title + " 别名"))
                db.execute("DELETE FROM anime_title WHERE anime_id=5")
                db.executemany("INSERT INTO anime_title VALUES(?,?,?,?,?)", [
                    (5, "ja", title, "primary", "bangumi-archive"),
                    (5, "zh-Hans", title + " 别名", "alias", "bangumi-archive"),
                    (5, "en", title + " 错标英文", "alias", "bangumi-archive"),
                ])
            self.assertNotIn(5, {row["id"] for row in self.query()["items"]})
            self.assertNotIn(5, {row["id"] for row in self.query(q=[title])["items"]})
            self.assertIn(5, {row["id"] for row in self.query(radar=["0"], q=[title])["items"]})
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM anime_country WHERE anime_id=5")
            db.execute("INSERT INTO anime_country VALUES(5,'JP','verified')")
            db.execute("UPDATE anime_work SET title_ja='漢字',title_zh_hans=NULL,title_en=NULL WHERE id=5")
            db.execute("DELETE FROM anime_title WHERE anime_id=5")
        self.assertIn(5, {row["id"] for row in self.query()["items"]})

    def test_first_episode_uses_ani_rss_report_even_when_resource_policy_disables_it(self):
        client = mock.Mock()
        client.call.side_effect = lambda path, **_: ({"weeks": [{"items": [{"url": "https://mikan.test/work", "title": "Work", "bgmId": "1"}]}]}
            if path == "mikan" else [{"label": "Disabled group", "rss": "https://mikan.test/feed",
                                     "items": [{"title": "Unparseable release title", "episode": 1.0,
                                                "length": 100, "pubDate": "2026-07-01T10:00:00Z"}]}])
        with mock.patch.dict(os.environ, {"ANM_ANI_RSS_API_KEY": "test-key"}), mock.patch.object(ani_rss, "_client", return_value=client), mock.patch.object(ani_rss, "_policy_eligibility_and_rank", return_value=(False, 0)):
            result = ani_rss.search(self.db_path, 1, self.config)
        row = next(row for row in self.query()["items"] if row["id"] == 1)
        self.assertEqual(0, result["eligible"])
        self.assertEqual(1, row["episode_progress"]["current"])
        self.assertEqual("2026-07-01T10:00:00+00:00", row["last_episode_update_at"])

    def test_followup_defaults_leave_hidden_regions_and_untranslated_works_searchable(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM anime_country")
            db.execute("UPDATE anime_work SET title_ja='シンサク3' WHERE id=3")
            db.executemany("INSERT INTO anime_country VALUES(?,?,?)", [(1, "JP", "test"), (2, "CN", "test"),
                                                                       (4, "OTHER", "test"), (5, "JP", "test")])
            db.execute("UPDATE anime_work SET title_zh_hans=title_ja,title_en=NULL WHERE id=5")
            db.execute("DELETE FROM anime_title WHERE anime_id=5 AND language<>'ja'")
        result = self.query(radar=["0"])
        self.assertEqual({1, 3}, {row["id"] for row in result["items"]})
        for anime_id in (2, 4, 5):
            found = self.query(radar=["0"], q=[f"作品 {anime_id}"])
            self.assertIn(anime_id, {row["id"] for row in found["items"]})
        self.assertEqual(5, self.query(radar=["0"], sort=["date"])["total"])

    def test_card_update_and_premiere_date_render_verified_evidence_only(self):
        source = (ROOT / "src/animemachine/web/static/app.js").read_text(encoding="utf-8")
        fragment = source[source.index("function episodeUpdate("):source.index("function completeBadge(")]
        script = '''const assert = require('node:assert/strict');
const esc = (value) => String(value), t = () => '第{episode}话', localMonth = () => 'month';
''' + fragment + '''
const item = {episode_progress: {current: 4}, last_episode_update_at: '2026-10-02T08:15:00Z'};
assert.ok(episodeUpdate(item).includes('2026-10-02 08:15'));
assert.ok(episodeUpdate(item).includes('第4话'));
assert.equal(episodeUpdate({...item, last_episode_update_at: null}), '');
assert.equal(episodeUpdate({...item, last_episode_update_at: 'invalid'}), '');
assert.equal(episodeUpdate({...item, episode_progress: {current: null}}), '');
assert.equal(radarStartDate({raw_date: '2026-10-02'}), '2026-10-02');
assert.equal(radarStartDate({raw_date: '2026-02-30'}), 'month');
assert.equal(radarStartDate({raw_date: '2026-10'}), 'month');
'''
        subprocess.run([shutil.which("node") or "node", "-e", script],
                       env={**os.environ, "TZ": "UTC"}, check=True, capture_output=True, text=True)

    def test_unsubscribed_frontier_advances_once_and_promotes_latest_work(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.record_release_progress(db, 1, 8, "2026-09-01T00:00:00+00:00")
            self.assertIsNone(db.execute("SELECT last_episode_update_at FROM ani_rss_release_state").fetchone()[0])
            ani_rss.record_release_progress(db, 1, 9, "2026-09-02T00:00:00+00:00")
            ani_rss.record_release_progress(db, 2, 5, "2026-09-02T00:00:00+00:00")
            ani_rss.record_release_progress(db, 2, 6, "2026-09-03T00:00:00+00:00")
            ani_rss.record_release_progress(db, 1, 9, "2026-09-04T00:00:00+00:00")
            ani_rss.record_release_progress(db, 1, 4, "2026-09-05T00:00:00+00:00")
            db.execute("UPDATE anime_work SET episode_count=12 WHERE id=1")
        result = self.query()
        self.assertEqual([2, 1], [row["id"] for row in result["items"][:2]])
        row = result["items"][1]
        self.assertEqual({"current": 9, "total": 12}, row["episode_progress"])
        self.assertEqual("none", row["subscription_state"])
        self.assertEqual("2026-09-02T00:00:00+00:00", row["last_episode_update_at"])
        self.assertEqual(result["items"], self.query(direction=["desc"])["items"])
        with mock.patch.object(ani_rss, "state", return_value=self.ready):
            detail = service.catalog_detail(self.db_path, 1, self.config)
        self.assertEqual(row["episode_progress"], detail["episode_progress"])

    def test_dates_pagination_paused_subscription_and_unknown_totals(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET start_month='2026-09' WHERE id=5")
            db.execute("""INSERT INTO ani_rss_subscription
                (remote_id,anime_id,title,enabled,subscription_kind,current_episode,total_episode,
                 remote_state,first_seen_at,last_seen_at,evidence_json)
                VALUES('paused',1,'Paused',0,'follow',9,6,'paused','now','now','{}')""")
        rows = self.query()["items"]
        self.assertEqual(4, len(rows))
        paused = next(row for row in rows if row["id"] == 1)
        self.assertEqual("paused", paused["subscription_state"])
        self.assertEqual({"current": 9, "total": None}, paused["episode_progress"])
        first = self.query(limit=["2"])["items"]
        second = self.query(limit=["2"], offset=["2"])["items"]
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in first + second])

    def test_physical_owner_does_not_leak_episode_progress_between_logical_works(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET episode_count=12 WHERE id IN (1,2)")
            db.execute("UPDATE anime_work SET physical_role='split_cour',physical_owner_anime_id=1 WHERE id=2")
            db.executemany("""INSERT INTO ani_rss_subscription
                (remote_id,anime_id,title,enabled,subscription_kind,current_episode,total_episode,
                 remote_state,first_seen_at,last_seen_at,evidence_json)
                VALUES(?,?,?,1,'follow',?,12,'enabled','now','now','{}')""", [
                ('owner-sub', 1, 'Owner cour', 12),
                ('child-sub', 2, 'Child cour', 2),
            ])
            db.executemany("INSERT INTO ani_rss_episode_state VALUES(?,?,?)", [
                ('owner-sub', '2026-09-10T00:00:00+00:00', '2026-09-10T00:00:00+00:00'),
                ('child-sub', '2026-09-02T00:00:00+00:00', '2026-09-02T00:00:00+00:00'),
            ])
            db.execute("""INSERT INTO ani_rss_resource
                (resource_id,anime_id,provider,resource_kind,title,resource_group,source_class,resolution,subtitle,
                 sequence_first,sequence_last,item_count,total_bytes,eligible,rank_key,payload_json,discovered_at,expires_at)
                VALUES('child-resource',2,'test','torrent','Child resource',NULL,'bdrip',1080,NULL,
                       1,12,12,1000,1,'a','{}','2026-09-01T00:00:00+00:00','2099-01-01T00:00:00+00:00')""")
        child = next(row for row in self.query()["items"] if row["id"] == 2)
        self.assertEqual({"current": 2, "total": 12}, child["episode_progress"])
        self.assertEqual("2026-09-02T00:00:00+00:00", child["last_episode_update_at"])
        self.assertTrue(child["ani_rss_managed"])
        self.assertEqual(1, child["ani_rss_resource_count"])
        with mock.patch.object(ani_rss, "state", return_value=self.ready):
            detail = service.catalog_detail(self.db_path, 2, self.config)
        self.assertEqual(child["episode_progress"], detail["episode_progress"])

    def test_cumulative_episode_numbers_are_normalized_to_the_current_work(self):
        cases = [
            (99, "第四季 夺还篇", "2026-08-12", 78, 8, 81, None, {"current": 4, "total": 8}),
            (97, "第四季 夺还篇（当前）", "2026-08-12", 78, 8, 82, None, {"current": 5, "total": 8}),
            (96, "第四季 夺还篇（系列总数污染）", "2026-08-12", 78, 8, 81, 99, {"current": 4, "total": 8}),
            (98, "第四季 丧失篇", "2026-04-08", 67, 11, 77, None, {"current": 11, "total": 11}),
        ]
        for bgm_id, title, air_date, first_episode, count, current, total, expected in cases:
            with self.subTest(title=title):
                archive = Path(self.tmp.name) / f"cumulative-{bgm_id}.zip"
                db_path = Path(self.tmp.name) / f"cumulative-{bgm_id}.sqlite3"
                archive_with_rows(archive, [{
                    "id": bgm_id, "type": 2, "name": title, "name_cn": title,
                    "date": air_date, "platform": 1, "infobox": "", "tags": [{"name": "日本动画"}], "summary": "",
                }], [
                    {"id": bgm_id * 100 + index, "subject_id": bgm_id, "type": 0,
                     "sort": first_episode + index - 1}
                    for index in range(1, count + 1)
                ])
                with contextlib.redirect_stdout(None):
                    service.write_database(db_path, service.build_items_from_archive(archive, None, {}))
                with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                    self.assertEqual((count, first_episode), db.execute(
                        "SELECT episode_count,episode_start_number FROM anime_work WHERE bgm_id=?",
                        (bgm_id,),
                    ).fetchone())
                    db.execute("""INSERT INTO ani_rss_subscription
                        (remote_id,anime_id,title,bgm_id,enabled,subscription_kind,current_episode,total_episode,
                         remote_media_path,remote_state,first_seen_at,last_seen_at,missed_successful_syncs,deleted_at,evidence_json)
                        VALUES('absolute',1,'Absolute',?,1,'follow',?,?,NULL,'enabled','now','now',0,NULL,'{}')""",
                        (bgm_id, current, total))
                params = {"sort": ["recent_episode"], "radar": ["1"], "start_from": [air_date[:7]],
                          "start_to": [air_date[:7]], "limit": ["50"]}
                with mock.patch.object(ani_rss, "state", return_value=self.ready):
                    row = service.query_catalog(db_path, params, self.config)["items"][0]
                    detail = service.catalog_detail(db_path, 1, self.config)
                self.assertEqual(expected, row["episode_progress"])
                self.assertEqual(row["episode_progress"], detail["episode_progress"])
                self.assertEqual(expected, detail["ani_rss"]["subscriptions"][0]["episodeProgress"])

    def test_legacy_catalog_uses_only_an_unambiguous_same_media_sequel_chain(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.executemany("UPDATE anime_work SET episode_count=?,episode_start_number=NULL WHERE id=?", [
                (25, 1), (25, 2), (16, 3), (11, 4),
            ])
            db.executemany("INSERT OR REPLACE INTO anime_relation_edge VALUES(?,?,?,?,?)", [
                (1, 2, "sequel", 1, "test"), (2, 3, "sequel", 1, "test"), (3, 4, "sequel", 1, "test"),
            ])
            ani_rss.record_release_progress(db, 4, 76, "2026-09-01T00:00:00+00:00")
            ani_rss.record_release_progress(db, 4, 77, "2026-09-02T00:00:00+00:00")
        row = next(item for item in self.query()["items"] if item["id"] == 4)
        self.assertEqual({"current": 11, "total": 11}, row["episode_progress"])


    def test_radar_limits_results_to_japan_or_unknown_region(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie' WHERE id=1")
            db.execute("DELETE FROM anime_country WHERE anime_id IN (2,3,4,5)")
            db.execute("INSERT INTO anime_country VALUES(2,'CN','test')")
            db.execute("INSERT INTO anime_country VALUES(3,'JP','test')")
            db.execute("INSERT INTO anime_country VALUES(3,'US','test')")
            db.execute("INSERT INTO anime_country VALUES(4,'OTHER','insufficient_country_evidence')")
            db.execute("UPDATE anime_work SET title_ja='シンサク4' WHERE id=4")
            db.execute("INSERT INTO anime_country VALUES(5,'BR','test')")
            db.execute("INSERT INTO anime_country VALUES(5,'OTHER','insufficient_country_evidence')")
        ids = {row["id"] for row in self.query()["items"]}
        self.assertEqual({1, 3, 4}, ids)

    def test_radar_search_cannot_bypass_media_region_or_quarter_constraints(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-01' WHERE id=1")
            db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(1,'bd','2026-07-15','bangumi-archive:infobox:BD発売日')")
            db.execute("UPDATE anime_work SET media_code='web' WHERE id=2")
            db.execute("UPDATE anime_work SET start_month='2026-09' WHERE id=3")
            db.execute("DELETE FROM anime_country WHERE anime_id=4")
            db.execute("INSERT INTO anime_country VALUES(4,'CN','test')")
        ids = {row["id"] for row in self.query(q=["作品"], media_type=["web"])["items"]}
        self.assertEqual({1, 2, 5}, ids)

    def test_movie_reenters_quarter_by_explicit_bd_event_without_duplication(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-01' WHERE id=1")
            db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(1,'bd','2026-07-15','bangumi-archive:infobox:BD発売日')")
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-07' WHERE id=2")
            db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(2,'bd','2026-08-20','bangumi-archive:infobox:BD发售日期')")
        rows = self.query()["items"]
        ids = [row["id"] for row in rows]
        self.assertIn(1, ids)
        self.assertEqual(1, ids.count(1))
        self.assertEqual(1, ids.count(2))

    def test_radar_orders_all_eight_categories_and_excludes_other_media(self):
        archive = Path(self.tmp.name) / "categories.zip"
        rows = [{"id": index, "type": 2, "name": f"アニメ {index}",
                 "name_cn": f"译名 {index}" if index <= 4 else "", "date": "2026-07-01",
                 "platform": platform, "infobox": "", "tags": [{"name": "日本动画"}]}
                for index, platform in enumerate([1, 3, 2, 5, 1, 3, 2, 5, 4, 6, 0], 1)]
        archive_with_rows(archive, rows)
        self.db_path.unlink()
        with contextlib.redirect_stdout(None):
            service.write_database(self.db_path, service.build_items_from_archive(archive, None, {}))
        for ready in (self.ready, {"connection_state": "failed", "credentialConfigured": False}):
            self.ready = ready
            result = self.query()
            self.assertEqual(list(range(1, 9)), [item["bgm_id"] for item in result["items"]])
            self.assertEqual(0, self.query(q=["アニメ 9"])["total"])
            self.assertEqual(1, self.query(radar=["0"], q=["アニメ 9"])["total"])

    def test_default_order_keeps_premiere_primary_and_updates_same_day_ties(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET raw_date='2026-07-02' WHERE id=1")
            db.execute("UPDATE anime_work SET raw_date='2026-07-01' WHERE id IN (2,3)")
            ani_rss.record_release_progress(db, 1, 1, "2026-07-12T00:00:00+00:00", published_at="2026-07-12T00:00:00+00:00")
            ani_rss.record_release_progress(db, 2, 1, "2026-07-10T00:00:00+00:00", published_at="2026-07-10T00:00:00+00:00")
            ani_rss.record_release_progress(db, 3, 1, "2026-07-11T00:00:00+00:00", published_at="2026-07-11T00:00:00+00:00")
        ids = [item["id"] for item in self.query()["items"]]
        self.assertLess(ids.index(3), ids.index(2))
        self.assertLess(ids.index(2), ids.index(1))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.record_release_progress(db, 2, 2, "2026-07-13T00:00:00+00:00", published_at="2026-07-13T00:00:00+00:00")
        ids = [item["id"] for item in self.query()["items"]]
        self.assertLess(ids.index(2), ids.index(3))
        self.assertLess(ids.index(3), ids.index(1))

    def test_movie_resource_date_uses_earliest_release_and_keeps_original_premiere(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-01',raw_date='2026-01-15' WHERE id=1")
            ani_rss._record_movie_releases(db, 1, [
                {"createdAt": "2026-07-01T00:30:00+08:00"},
                {"createdAt": "2026-08-01T00:30:00+08:00"},
                {"createdAt": "2025-12-01T00:30:00+08:00"},
                {"createdAt": "2099-12-01T00:30:00+08:00"},
            ])
        item = next(row for row in self.query()["items"] if row["id"] == 1)
        self.assertEqual("2026-01-15", item["raw_date"])
        self.assertEqual("2026-07-01", item["radar_release_date"])
        self.assertEqual("resource", item["radar_release_kind"])
        self.assertEqual("2026-07-31T16:30:00+00:00", item["last_episode_update_at"])
        self.assertNotIn(1, {row["id"] for row in self.query(start_from=["2026-04"], start_to=["2026-06"])["items"]})
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss._record_movie_releases(db, 1, [{"createdAt": "2026-09-02T00:30:00+08:00"}])
        self.assertEqual("2026-07-01", next(row for row in self.query()["items"] if row["id"] == 1)["radar_release_date"])
        self.assertNotIn(1, {row["id"] for row in self.query(start_from=["2026-09"], start_to=["2026-11"])["items"]})
        self.ready = {"connection_state": "failed", "credentialConfigured": False}
        item = next(row for row in self.query()["items"] if row["id"] == 1)
        self.assertEqual("2026-07-01", item["radar_release_date"])
        self.assertIsNone(item["last_episode_update_at"])

    def test_explicit_bd_dates_replace_resource_inference_and_work_changes_invalidate_it(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-01',raw_date='2026-01-15' WHERE id=1")
            ani_rss._record_movie_releases(db, 1, [{"createdAt": "2026-07-01T00:30:00+08:00"}])
            db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(1,'bd','2026-09-15','bangumi-archive:infobox:BD発売日')")
        self.assertNotIn(1, {row["id"] for row in self.query()["items"]})

        item = next(row for row in self.query(start_from=["2026-09"], start_to=["2026-11"])["items"] if row["id"] == 1)
        self.assertEqual(("bd", "2026-09-15"), (item["radar_release_kind"], item["radar_release_date"]))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM anime_release_event WHERE anime_id=1")
            db.execute("UPDATE anime_work SET start_month='2027-01',raw_date='2027-01-15' WHERE id=1")
        self.assertNotIn(1, {row["id"] for row in self.query()["items"]})

    def test_archive_new_translation_reorders_movie_and_keeps_resource_evidence(self):
        archive = Path(self.tmp.name) / "translations.zip"
        incoming = Path(self.tmp.name) / "new.sqlite3"
        rows = [{"id": index, "type": 2, "name": name, "name_cn": translation,
                 "date": "2026-01-15", "platform": 3, "infobox": "", "tags": [{"name": "日本动画"}]}
                for index, name, translation in [(1, "映画あ", ""), (2, "映画い", "译名二")]]
        archive_with_rows(archive, rows)
        self.db_path.unlink()
        with contextlib.redirect_stdout(None):
            service.write_database(self.db_path, service.build_items_from_archive(archive, None, {}))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            for anime_id in (1, 2):
                ani_rss._record_movie_releases(db, anime_id, [{"createdAt": "2026-07-01T00:30:00+08:00"}])
        self.assertEqual([2, 1], [row["bgm_id"] for row in self.query()["items"]])
        rows[0]["name_cn"] = "译名一"
        archive_with_rows(archive, rows)
        with contextlib.redirect_stdout(None):
            service.write_database(incoming, service.build_items_from_archive(archive, None, {}))
        archive_update.merge_metadata(self.db_path, incoming, service)
        result = self.query()["items"]
        self.assertEqual([1, 2], [row["bgm_id"] for row in result])
        self.assertTrue(all(row["radar_release_date"] == "2026-07-01" for row in result))

    def test_movie_sort_uses_premiere_before_episode_update(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-01' WHERE id IN (1,2)")
            db.execute("UPDATE anime_work SET raw_date='2026-01-15' WHERE id=1")
            db.execute("UPDATE anime_work SET raw_date='2026-01-16' WHERE id=2")
            db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(1,'bd','2026-08-20','bangumi-archive:infobox:BD発売日')")
            db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(2,'bd','2026-07-10','bangumi-archive:infobox:BD発売日')")
            ani_rss.record_release_progress(db, 1, 99, "2026-09-10T00:00:00+00:00")
            ani_rss.record_release_progress(db, 3, 2, "2026-09-09T00:00:00+00:00")
            ani_rss.record_release_progress(db, 3, 3, "2026-09-10T00:00:00+00:00")
        ids = [row["id"] for row in self.query()["items"]]
        self.assertEqual(3, ids[0])
        self.assertLess(ids.index(1), ids.index(2))

    def test_archive_release_event_extraction_requires_explicit_semantics(self):
        subject = {
            "platform": 3,
            "date": "2026-01-01",
            "infobox": """{{Infobox\n|上映日期=2026/07/02\n|BD/DVD発売日=2026年8月27日 / 2026-11-01\n|Blu-ray BOX 発売予定日=2026.09.30\n|发售日=2026-09-01\n|发行日期=2026-09-02\n|BD发售日期=2026-02-30\n}}""",
        }
        self.assertEqual(
            [
                {"event_type": "bd", "release_date": "2026-08-27", "source": "bangumi-archive:infobox:BD/DVD発売日"},
                {"event_type": "bd", "release_date": "2026-09-30", "source": "bangumi-archive:infobox:Blu-ray BOX 発売予定日"},
                {"event_type": "bd", "release_date": "2026-11-01", "source": "bangumi-archive:infobox:BD/DVD発売日"},
                {"event_type": "theatrical", "release_date": "2026-07-02", "source": "bangumi-archive:infobox:上映日期"},
            ],
            service.archive_release_events(subject),
        )

    def test_archive_explicit_date_parser_rejects_trailing_digits(self):
        self.assertEqual([], service.parse_archive_explicit_dates("2026-08-271"))
        self.assertEqual(["2026-08-27"], service.parse_archive_explicit_dates("2026-08-27"))

    def test_archive_build_persists_bd_event_without_mutating_work_date(self):
        archive = Path(self.tmp.name) / "movie-archive.zip"
        db_path = Path(self.tmp.name) / "movie.sqlite3"
        archive_with_rows(archive, [{
            "id": 99, "type": 2, "name": "映画", "name_cn": "电影", "date": "2026-01-15", "platform": 3,
            "infobox": "{{Infobox\n|BD発売日=2026年8月27日\n}}", "tags": [], "summary": "",
        }])
        with contextlib.redirect_stdout(None):
            service.write_database(db_path, service.build_items_from_archive(archive, None, {}))
        with contextlib.closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(
                db.execute("SELECT start_month,raw_date FROM anime_work WHERE bgm_id=99").fetchone(),
                ("2026-01", "2026-01-15"),
            )
            self.assertEqual(
                db.execute("SELECT event_type,release_date FROM anime_release_event WHERE anime_id=(SELECT id FROM anime_work WHERE bgm_id=99)").fetchall(),
                [("bd", "2026-08-27")],
            )

    def test_connection_loss_hides_stale_progress_and_falls_back(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.record_release_progress(db, 1, 10, "2026-09-01T00:00:00+00:00")
        self.ready = {"connection_state": "error", "credentialConfigured": True}
        result = self.query()
        self.assertEqual("random", result["effectiveSort"])
        self.assertTrue(all(row["subscription_state"] == "unknown" for row in result["items"]))
        self.assertTrue(all(row["episode_progress"]["current"] is None for row in result["items"]))

    def test_release_schema_upgrade_is_idempotent_and_preserves_subscription(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DROP TABLE ani_rss_release_state")
            ani_rss.migrate(db)
            ani_rss.record_release_progress(db, 1, 3, "2026-09-01T00:00:00+00:00")
            ani_rss.migrate(db)
            self.assertEqual(3, db.execute("SELECT current_episode FROM ani_rss_release_state").fetchone()[0])

    def test_release_episode_parser_handles_common_names_without_using_resolution_or_year(self):
        for title, expected in (
            ("[SubsPlease] Work - 09 (1080p) [WEB-DL]", 9),
            ("[Group] Work - 10v2 [720p]", 10),
            ("Work S02E13 [1080p]", 13),
            ("Work 第12話 [1080p]", 12),
            ("Work [2026] [1080p]", None),
            ("Work - 2026 [BDRip]", None),
            ("Work - 01-12 [Batch] [1080p]", None),
        ):
            with self.subTest(title=title):
                self.assertEqual(expected, ani_rss._resource_fields(title)[2])

    def test_all_quarter_boundaries_roll_exactly_seven_days_early(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required by the canonical verification")
        source = (ROOT / "src/animemachine/web/static/app.js").read_text(encoding="utf-8")
        def function(name):
            start = source.index("function " + name + "(")
            return source[start:source.index("\n}", start) + 2]
        script = "\n".join(function(name) for name in ("exactYear", "seasonDateRange", "radarSeasons"))
        dates = []
        expected = []
        for year in (2026, 2027, 2028):
            for month, season in ((1, "winter"), (4, "spring"), (7, "summer"), (10, "autumn")):
                boundary = dt.date(year, month, 1)
                for days in (-8, -7, -1, 0, 1):
                    day = boundary + dt.timedelta(days=days)
                    dates.append([day.year, day.month, day.day])
                    ordinal = year * 4 + (month - 1) // 3 - (days == -8)
                    expected.append([ordinal // 4, ["winter", "spring", "summer", "autumn"][ordinal % 4]])
        script += "\nconsole.log(JSON.stringify(" + json.dumps(dates) + ".map(([y,m,d]) => radarSeasons(new Date(y,m-1,d)).map(s => [s.year,s.season,s.from,s.to]))));"
        output = subprocess.check_output([node, "-e", script], text=True, encoding="utf-8")
        actual = json.loads(output)
        self.assertEqual(expected, [row[1][:2] for row in actual])
        self.assertEqual(["2025-12", "2026-02"], actual[1][1][2:])

    def test_seen_history_keeps_recent_clicks_even_for_small_numeric_ids(self):
        source = (ROOT / "src/animemachine/web/static/app.js").read_text(encoding="utf-8")
        start = source.index('const radarSeenStorageKey =')
        fragment = source[start:source.index('function radarSeasons(', start)]
        script = """
const assert = require('node:assert/strict');
let stored = JSON.stringify({'2': 'old'});
const localStorage = {getItem: () => stored, setItem: (key, value) => { stored = value; }};
const $ = () => null;
""" + fragment + """
assert.equal(radarIsHighlighted({bgm_id: 2, last_episode_update_at: 'old'}), false);
for (let id = 1000; id < 2000; id++) markRadarSeen(String(id), id, 'new');
markRadarSeen('2', 2, 'new');
assert.equal(radarIsHighlighted({bgm_id: 2, last_episode_update_at: 'new'}), false);
assert.equal(radarIsHighlighted({bgm_id: 2, last_episode_update_at: 'later'}), true);
const restored = new Map(JSON.parse(stored));
assert.equal(restored.size, 1000);
assert.equal(restored.get('2'), 'new');
localStorage.setItem = () => { throw new Error('storage unavailable'); };
markRadarSeen('3', 3, 'new');
assert.equal(radarIsHighlighted({bgm_id: 3, last_episode_update_at: 'new'}), false);
"""
        subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)

    def test_subscription_management_is_available_before_first_media_arrives(self):
        source = (ROOT / "src/animemachine/web/static/app.js").read_text(encoding="utf-8")
        start = source.index('function aniSubscriptionManagementHtml(')
        fragment = source[start:source.index('\n}', start) + 2]
        script = """
const assert = require('node:assert/strict');
const t = (value) => value, fmt = String;
const esc = (value) => String(value).replaceAll('<', '&lt;').replaceAll('"', '&quot;');
""" + fragment + """
for (const enabled of [true, false]) {
  const html = aniSubscriptionManagementHtml({remoteId: 'empty', title: '<synthetic>', enabled,
    playableCount: 0, episodeProgress: {current: null, total: 12}});
  assert.ok(html.includes('data-ani-rss-delete="empty"'));
  assert.ok(html.includes(enabled ? 'radarSubscribed' : 'radarPaused'));
  assert.ok(html.includes('? / 12'));
  assert.ok(!html.includes('<synthetic>'));
}
"""
        subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)

    def test_user_creation_fields_do_not_participate_in_settings_validation(self):
        from html.parser import HTMLParser
        inputs = {}
        class Controls(HTMLParser):
            def handle_starttag(self, tag, attrs):
                fields = dict(attrs)
                if "id" in fields:
                    inputs[fields["id"]] = fields
        Controls().feed((ROOT / "src/animemachine/web/static/index.html").read_text(encoding="utf-8"))
        for name in ("newUsername", "newUserPassword", "newUserRole"):
            self.assertEqual("userCreateForm", inputs[name]["form"])
        self.assertNotIn("playbackEnabled", inputs)


if __name__ == "__main__":
    unittest.main()
