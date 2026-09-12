"""Behavioral regression tests for release radar and its persisted episode evidence."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from animemachine.catalog import service
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
             "date": "2026-07-01", "platform": 1, "infobox": "", "tags": [], "summary": ""}
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
                    "date": air_date, "platform": 1, "infobox": "", "tags": [], "summary": "",
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


    def test_radar_limits_results_to_tv_or_movie_and_japan_or_unknown_region(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie' WHERE id=1")
            db.execute("DELETE FROM anime_country WHERE anime_id IN (2,3,4,5)")
            db.execute("INSERT INTO anime_country VALUES(2,'CN','test')")
            db.execute("INSERT INTO anime_country VALUES(3,'JP','test')")
            db.execute("INSERT INTO anime_country VALUES(3,'US','test')")
            db.execute("INSERT INTO anime_country VALUES(4,'OTHER','insufficient_country_evidence')")
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
            db.execute("INSERT INTO anime_country VALUES(4,'CN','test')")
        ids = {row["id"] for row in self.query(q=["作品"], media_type=["web"])["items"]}
        self.assertEqual({1, 5}, ids)

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

    def test_movie_sort_uses_quarter_release_event_not_episode_update(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-01' WHERE id IN (1,2)")
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
