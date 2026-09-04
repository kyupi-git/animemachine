import sqlite3
import contextlib
import datetime as dt
import hashlib
import io
import os
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from animemachine.catalog import service as catalog
from animemachine.integrations import ani_rss


DB = Path(__file__).resolve().parents[1] / "fixtures" / "anime-catalog.sqlite3"


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DB.exists():
            raise unittest.SkipTest("build the sample database first")

    def test_expected_sample_and_archive_provenance(self):
        with contextlib.closing(sqlite3.connect(DB)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM anime_work").fetchone()[0], 10)
            self.assertEqual(db.execute("SELECT title_ja FROM anime_work WHERE bgm_id=265").fetchone()[0], "新世紀エヴァンゲリオン")
            self.assertIn("ENGI", db.execute("SELECT studio FROM anime_work WHERE bgm_id=430699").fetchone()[0])
            self.assertIn("Bangumi Archive", db.execute("SELECT value FROM metadata WHERE key='sources'").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM anime_title WHERE source='bangumi'").fetchone()[0], 0)

    def test_instance_random_seed_persists_until_explicit_reshuffle(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
                db.execute("INSERT INTO metadata VALUES('record_count','10')")
            with mock.patch.object(catalog.secrets, "token_urlsafe", side_effect=["seed-first", "seed-next"]):
                first, created = catalog.ensure_instance_random_seed(db_path)
                second, created_again = catalog.ensure_instance_random_seed(db_path)
                rotated = catalog.rotate_instance_random_seed(db_path)
                after, created_after = catalog.ensure_instance_random_seed(db_path)
        self.assertEqual("seed-first", first)
        self.assertTrue(created)
        self.assertEqual("seed-first", second)
        self.assertFalse(created_again)
        self.assertEqual("seed-next", rotated)
        self.assertEqual("seed-next", after)
        self.assertFalse(created_after)

    def test_director_filter_excludes_specialized_supervisors(self):
        with contextlib.closing(sqlite3.connect(DB)) as db:
            directors = [x[0] for x in db.execute("SELECT DISTINCT role FROM anime_staff WHERE role_type='director'")]
            self.assertTrue(set(directors).issubset({"导演", "总导演", "联合导演"}))
            self.assertGreater(len(directors), 0)

    def test_catalog_filters_and_detail(self):
        result = catalog.query_catalog(DB, {"decade": ["2020s"]})
        self.assertGreaterEqual(result["total"], 1)
        self.assertTrue(all(item["start_month"].startswith("202") for item in result["items"]))
        eva = catalog.query_catalog(DB, {"q": ["エヴァンゲリオン"]})
        self.assertEqual(eva["total"], 1)
        detail = catalog.catalog_detail(DB, eva["items"][0]["id"])
        self.assertTrue(detail and detail["relations"] and detail["cast"])
        self.assertTrue(all("relation_code" in relation and "strict_group" in relation for relation in detail["relations"]))
        strict_codes = {relation["relation_code"] for relation in detail["relations"] if relation["strict_group"]}
        self.assertTrue(strict_codes.issubset(catalog.STRICT_SERIES_RELATIONS))

    def test_infobox_parser_handles_lists(self):
        parsed = catalog.parse_archive_infobox("{{Infobox\n|别名={\n[英文名|Example]\n[简称|EX]\n}\n|动画制作=Studio A\n}}")
        self.assertEqual(parsed["别名"], ["Example", "EX"])
        self.assertEqual(parsed["动画制作"], ["Studio A"])

    def test_non_japanese_archive_title_language_and_labelled_aliases(self):
        raw = """{{Infobox animanga/TVAnime
|别名={
[Dogulwang]
[日文版|盗掘王]
[英文版|Tomb Raider King]
}
|国家/地区=韩国
|动画制作=Studio EEK
}}"""
        entries = catalog.parse_archive_alias_entries(raw)
        self.assertIn(("盗掘王", "日文版"), entries)
        self.assertIn(("Tomb Raider King", "英文版"), entries)
        self.assertEqual(catalog.alias_language("盗掘王", "日文版"), "ja")
        self.assertEqual(catalog.alias_language("Tomb Raider King", "英文版"), "en")
        self.assertEqual(catalog.infer_original_language("도굴왕"), "ko")
        self.assertEqual(catalog.infer_original_language("氷菓"), "ja")
        jp = catalog.infer_country_codes(["日本动画"], "BLEACH", [])
        self.assertEqual(catalog.infer_original_language("BLEACH", jp), "ja")
        cn = catalog.infer_country_codes(["中国动画"], "熊出没", [])
        self.assertEqual(catalog.infer_original_language("熊出没", cn), "zh")
        self.assertEqual(catalog.infer_original_language("麥兜故事", [("HK", "archive_infobox")]), "zh")
        self.assertEqual(catalog.infer_original_language("魔法阿媽", [("TW", "archive_infobox")]), "zh")
        self.assertIn(("HK", "archive_infobox"), catalog.infer_country_codes([], "作品", [], {"国家/地区": ["香港"]}))
        self.assertIn(("TW", "archive_infobox"), catalog.infer_country_codes([], "作品", [], {"国家/地区": ["台湾"]}))
        self.assertIn(("PL", "archive_infobox"), catalog.infer_country_codes([], "Work", [], {"国家/地区": ["Poland"]}))
        self.assertEqual(catalog.infer_original_language("呪術廻戦", [("CN", "studio"), ("JP", "archive_tag")]), "ja")

    def test_korean_archive_build_keeps_animation_and_source_original_titles_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "archive.zip"
            anime = {
                "id": 621835, "type": 2, "name": "도굴왕", "name_cn": "盗墓王", "platform": 1,
                "date": "2026-01-01", "summary": "", "tags": [{"name": "韩国动画", "count": 10}],
                "infobox": "{{Infobox animanga/TVAnime\n|别名={\n[Dogulwang]\n[日文版|盗掘王]\n[英文版|Tomb Raider King]\n}\n|国家/地区=韩国\n|动画制作=Studio EEK\n}}",
            }
            manga = {
                "id": 341101, "type": 1, "name": "도굴왕", "name_cn": "我独自盗墓", "platform": 1001,
                "date": "2019-06-30", "summary": "", "tags": [{"name": "漫画", "count": 5}],
                "infobox": "{{Infobox animanga/Manga\n|别名={\n[日版|盗掘王]\n}\n}}",
            }
            files = {
                "subject.jsonlines": [anime, manga],
                "subject-persons.jsonlines": [], "subject-characters.jsonlines": [], "person-characters.jsonlines": [],
                "subject-relations.jsonlines": [{"subject_id": 621835, "related_subject_id": 341101, "relation_type": 1}],
                "episode.jsonlines": [{"subject_id": 621835, "type": 0}],
                "person.jsonlines": [], "character.jsonlines": [],
            }
            with __import__("zipfile").ZipFile(archive_path, "w") as archive:
                for name, rows in files.items():
                    archive.writestr(name, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            item = catalog.build_items_from_archive(archive_path, [{"bgm_id": 621835}], {})[0]
        self.assertEqual(item.work["title_ja"], "도굴왕")
        self.assertEqual(item.work["title_zh_hans"], "盗墓王")
        self.assertEqual(item.work["original_language"], "ko")
        self.assertEqual(item.work["title_en"], "Tomb Raider King")
        self.assertIn(("盗掘王", "ja"), {(x["title"], x["language"]) for x in item.titles})
        self.assertEqual(item.relations[0]["related_title"], "도굴왕")

    def test_non_japanese_studio_alias_groups_cluster_without_changing_japanese_rules(self):
        raw = "{{Infobox\n|动画制作=方特动漫〖华强方特（深圳）动漫有限公司、华强方特（芜湖）动漫有限公司〗\n}}"
        self.assertEqual(catalog.parse_archive_infobox(raw)["动画制作"],
                         ["方特动漫〖华强方特（深圳）动漫有限公司、华强方特（芜湖）动漫有限公司〗"] )
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.executescript("""
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY,original_language TEXT);
                CREATE TABLE anime_studio(anime_id INTEGER,studio TEXT,UNIQUE(anime_id,studio));
                CREATE TABLE anime_studio_cluster(anime_id INTEGER,cluster_key TEXT,cluster_name TEXT,studio_name TEXT,UNIQUE(anime_id,cluster_key,studio_name));
            """)
            db.executemany("INSERT INTO anime_work VALUES(?,?)", [(1, "zh"), (2, "zh"), (3, "zh"), (4, "ja")])
            db.executemany("INSERT INTO anime_studio VALUES(?,?)", [
                (1, "方特动漫〖华强方特（深圳）动漫有限公司、华强方特（芜湖）动漫有限公司〗"),
                (2, "深圳华强数字动漫有限公司（方特动漫）"), (3, "方特动漫"),
                (4, "日本動畫（別スタジオ）"),
            ])
            catalog.rebuild_studio_clusters(db)
            chinese = list(db.execute("SELECT DISTINCT cluster_key,cluster_name FROM anime_studio_cluster WHERE anime_id IN (1,2,3)"))
            japanese = list(db.execute("SELECT DISTINCT cluster_name FROM anime_studio_cluster WHERE anime_id=4 ORDER BY cluster_name"))
        self.assertEqual({name for _, name in chinese}, {"方特动漫"})
        self.assertEqual(len({key for key, _ in chinese}), 1)
        self.assertGreaterEqual(len(japanese), 1)

    def test_non_japanese_staff_studio_can_supply_country_language_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "archive.zip"
            subject = {
                "id": 700001, "type": 2, "name": "熊出没", "name_cn": "熊出没", "platform": 1,
                "date": "2026-01-01", "summary": "", "tags": [], "infobox": "{{Infobox animanga/TVAnime}}",
            }
            files = {
                "subject.jsonlines": [subject],
                "subject-persons.jsonlines": [{"subject_id": 700001, "person_id": 900001, "position": 67}],
                "subject-characters.jsonlines": [], "person-characters.jsonlines": [],
                "subject-relations.jsonlines": [], "episode.jsonlines": [{"subject_id": 700001, "type": 0}],
                "person.jsonlines": [{"id": 900001, "name": "方特动漫"}], "character.jsonlines": [],
            }
            with __import__("zipfile").ZipFile(archive_path, "w") as archive:
                for name, rows in files.items():
                    archive.writestr(name, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            item = catalog.build_items_from_archive(archive_path, [{"bgm_id": 700001}], {})[0]
        self.assertEqual(item.work["original_language"], "zh")
        self.assertIn(("CN", "studio"), item.manifest["_inferred_countries"])
        self.assertIn(("熊出没", "zh"), {(x["title"], x["language"]) for x in item.titles})

    def test_localized_archive_title_keeps_non_japanese_primary_separate(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE anime_title(anime_id INTEGER,language TEXT,title TEXT,title_type TEXT,source TEXT)")
            db.executemany("INSERT INTO anime_title VALUES(?,?,?,?,?)", [
                (1, "ko", "도굴왕", "primary", "bangumi-archive"),
                (1, "ja", "盗掘王", "alias", "bangumi-archive"),
                (1, "ja", "別タイトル", "label", "wikidata"),
            ])
            localized = catalog.localized_archive_title(db, 1, "ja", "도굴왕")
        self.assertEqual(localized, "盗掘王")

    def test_migration_reinfers_non_japanese_cast_without_relabelling_japanese_dub(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.executescript(catalog.SCHEMA)
            db.executemany(
                """INSERT INTO anime_work(
                    id,bgm_id,title_ja,start_month,directory_date,original_language,source_url,fetched_at
                ) VALUES(?,?,?,?,?,'ja',?,?)""",
                [
                    (1, 1, "熊出没", "2020-01", "2020-01", "https://bgm.tv/subject/1", "2026-01-01T00:00:00Z"),
                    (2, 2, "도굴왕", "2026-07", "2026-07", "https://bgm.tv/subject/2", "2026-01-01T00:00:00Z"),
                ],
            )
            db.executemany(
                "INSERT INTO anime_tag(anime_id,tag,vote_count,tag_rank) VALUES(?,?,?,?)",
                [(1, "中国动画", 10, 1), (2, "韩国动画", 10, 1)],
            )
            db.executemany(
                """INSERT INTO anime_cast(
                    anime_id,character_name,person_name,character_role,language,source
                ) VALUES(?,?,?,?,?,?)""",
                [
                    (1, "熊大", "张伟", "主角", "ja", "bangumi-archive"),
                    (2, "A", "김철수", "主角", "ja", "bangumi-archive"),
                    (2, "B", "山田太郎", "主角", "ja", "bangumi-archive"),
                ],
            )
            catalog.migrate_catalog_features(db)
            languages = dict(db.execute("SELECT id,original_language FROM anime_work"))
            cast = {(anime_id, name): language for anime_id, name, language in db.execute(
                "SELECT anime_id,person_name,language FROM anime_cast"
            )}
        self.assertEqual(languages, {1: "zh", 2: "ko"})
        self.assertEqual(cast[(1, "张伟")], "zh")
        self.assertEqual(cast[(2, "김철수")], "ko")
        self.assertEqual(cast[(2, "山田太郎")], "ja")

    def test_english_display_title_rejects_truncation_and_short_codes(self):
        self.assertEqual(
            catalog.choose_display_english_title([
                "ushoku Tensei: Jobless Reincarnation Season 3",
                "Mushoku Tensei III: Isekai Ittara Honki Dasu",
            ]),
            "Mushoku Tensei III: Isekai Ittara Honki Dasu",
        )
        self.assertEqual(
            catalog.choose_display_english_title(["SAO", "Sword Art Online"]),
            "Sword Art Online",
        )
        self.assertEqual(catalog.choose_display_english_title(["xxxHOLiC"]), "xxxHOLiC")

    def test_related_subject_kind_uses_archive_infobox(self):
        self.assertEqual(catalog.related_subject_kind({"type": 1, "infobox": "{{Infobox animanga/Manga}}"}), "manga")
        self.assertEqual(catalog.related_subject_kind({"type": 1, "tags": [{"name": "轻小说"}]}), "light_novel")
        self.assertEqual(catalog.related_subject_kind({"type": 1, "platform": 1001}), "manga")
        self.assertEqual(catalog.related_subject_kind({"type": 1, "platform": 1002}), "novel")
        self.assertEqual(catalog.related_subject_kind({"type": 3}), "music")

    def test_related_subject_metadata_keeps_original_credit_and_music_role(self):
        manga = catalog.related_subject_metadata({
            "id": 1, "type": 1, "name": "原題", "name_cn": "译名",
            "infobox": "{{Infobox animanga/Manga\n|作者=著者\n|出版社=出版社A\n}}",
        })
        music = catalog.related_subject_metadata({
            "id": 2, "type": 3, "name": "主題歌", "tags": [{"name": "OP"}],
            "infobox": "{{Infobox Album\n|艺术家=歌手A\n}}",
        })
        self.assertEqual(manga["title"], "原題")
        self.assertEqual(manga["authors"], ["著者"])
        self.assertEqual(manga["publishers"], ["出版社A"])
        self.assertEqual(music["role"], "opening")
        self.assertEqual(music["artists"], ["歌手A"])
        collection = catalog.related_subject_metadata({
            "id": 3, "type": 3,
            "name": "『無職転生 ～異世界行ったら本気だす～』Theme Song Collection",
            "tags": [{"name": "OP"}, {"name": "ED"}],
        })
        self.assertEqual(collection["role"], "theme_collection")

    def test_archive_staff_source_and_display_theme_evidence_are_conservative(self):
        self.assertEqual(catalog.STAFF_POSITIONS[6], ("音乐", "music"))
        self.assertEqual(catalog.STAFF_POSITIONS[8], ("角色设计", "character_design"))
        self.assertEqual(catalog.STAFF_POSITIONS[10], ("系列构成", "series_composition"))
        self.assertEqual(catalog.choose_source_type([], {"原作": ["漫画"]}), "漫画改")
        self.assertEqual(catalog.choose_source_type(["漫画改"], {}), "漫画改")
        self.assertIsNone(catalog.choose_source_type(["漫画"], {}))
        self.assertEqual(catalog.source_type_from_relations([
            {"relation_code": "adaptation", "related_subject_kind": "manga"}
        ]), "漫画改")
        self.assertIsNone(catalog.source_type_from_relations([
            {"relation_code": "adaptation", "related_subject_kind": "manga"},
            {"relation_code": "adaptation", "related_subject_kind": "novel"},
        ]))
        evidence = [
            {"theme_code": "romance", "accepted": True, "evidence": {"tags": [
                {"name": "恋爱", "count": 35, "rank": 4}, {"name": "爱情", "count": 50, "rank": 7},
            ]}},
            {"theme_code": "fantasy", "accepted": True, "evidence": {"tags": [
                {"name": "奇幻", "count": 100, "rank": 1},
            ]}},
            {"theme_code": "horror", "accepted": True, "evidence": {"tags": [
                {"name": "恐怖", "count": 2, "rank": 20},
            ]}},
            {"theme_code": "comedy", "accepted": False, "evidence": {"tags": [
                {"name": "搞笑", "count": 200, "rank": 1},
            ]}},
        ]
        self.assertEqual(catalog.ranked_display_themes(evidence), ["fantasy", "romance"])

    def test_original_source_summary_rejects_ambiguous_derivatives(self):
        source = {"title": "原作", "authors": ["作者A"]}
        derivative = {"title": "衍生漫画", "authors": ["作者B"]}
        relations = [
            {"relation_code": "adaptation", "related_subject_kind": "manga", "related_title": "原作",
             "related_subject_meta_json": json.dumps(source, ensure_ascii=False)},
            {"relation_code": "adaptation", "related_subject_kind": "manga", "related_title": "衍生漫画",
             "related_subject_meta_json": json.dumps(derivative, ensure_ascii=False)},
        ]
        self.assertEqual(catalog.original_source_summary(relations, "manga", ["原作"]), ("原作", ["作者A"]))
        self.assertEqual(catalog.original_source_summary(relations, "manga", ["动画标题"]), ("", []))
        self.assertEqual(catalog.original_source_summary(relations, "original", ["原作"]), ("", []))
        korean_relations = [
            {"relation_code": "adaptation", "related_subject_kind": "manga", "related_title": "도굴왕",
             "related_subject_meta_json": json.dumps({"title": "도굴왕", "authors": ["산지직송"]}, ensure_ascii=False)},
            {"relation_code": "adaptation", "related_subject_kind": "manga", "related_title": "다른 작품",
             "related_subject_meta_json": json.dumps({"title": "다른 작품", "authors": ["다른 작가"]}, ensure_ascii=False)},
        ]
        self.assertEqual(catalog.original_source_summary(korean_relations, "manga", ["도굴왕"]),
                         ("도굴왕", ["산지직송"]))


    def test_connection_summary_keeps_exactly_one_version_prefix(self):
        self.assertEqual("ready (v5.2.3)", catalog._connection_summary({"authenticated": True, "version": "v5.2.3"}))
        self.assertEqual("ready (v5.2.3)", catalog._connection_summary({"authenticated": True, "version": "5.2.3"}))
        self.assertNotIn("vv", catalog._connection_summary({"authenticated": True, "version": "v5.2.3"}))

    def test_access_banner_includes_archive_seed_and_blank_padding(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output), mock.patch.dict("os.environ", {
                "ANM_PUBLIC_URL": "", "ANM_QBT_API_KEY": "", "ANM_ANI_RSS_API_KEY": "",
                "ANM_ADMIN_USERNAME": ""}, clear=False):
            catalog._print_access_info(
                "127.0.0.1", 8787, {"components": {}}, Path("catalog.sqlite3"),
                instance_seed="seed-1", archive_meta={"name": "dump.zip", "created_at": "2026-08-30"},
                record_count=1234,
                self_check={
                    "proxy": {"mode": "environment_proxy", "proxy": "http://127.0.0.1:7890"},
                    "aniRss": {"authenticated": True, "version": "1.2.3"},
                    "qbittorrent": {"reachable": False, "authenticated": False},
                    "storage": {"library": {"name": "Library", "state": "available", "path": "/Library"}},
                    "images": {"subjectOk": 2, "subjectTotal": 3, "imageOk": 1, "imageTotal": 2},
                },
                preload={"recentWorks": 100},
            )
        text = output.getvalue()
        self.assertTrue(text.startswith("\n========== AnimeMachine access =========="))
        self.assertTrue(text.endswith("=========================================\n\n"))
        self.assertIn("Random seed", text)
        self.assertIn("seed-1", text)
        self.assertIn("Bangumi Archive", text)
        self.assertIn("dump.zip", text)
        self.assertIn("Catalog works", text)
        self.assertIn("1,234", text)
        self.assertIn("Environment proxy (http://127.0.0.1:7890)", text)
        self.assertIn("Ani-RSS", text)
        self.assertIn("ready (v1.2.3)", text)
        self.assertIn("Library /Library [ready]", text)
        self.assertIn("Image preload", text)
        self.assertNotIn("API key", text)

    def test_catalog_ready_progress_runs_after_database_write(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "catalog.sqlite3"
            args = SimpleNamespace(
                archive_dir=root / "archive", archive=None, network_config={}, all_anime=True,
                manifest=None, ids=None, cache=root / "cache", cache_days=30, refresh=False, request_interval=0.0,
                db=db_path, progress_callback=lambda value: events.append((value["phase"], db_path.exists())),
            )

            def fake_write(path, *_args):
                events.append(("write", False))
                path.write_bytes(b"catalog")

            with mock.patch.object(catalog, "ensure_archive", return_value=(root / "archive.zip", {"name": "dump.zip"})), \
                    mock.patch.object(catalog, "wikidata_titles", return_value={}), \
                    mock.patch.object(catalog, "build_items_from_archive", return_value=[]), \
                    mock.patch.object(catalog, "instance_random_seed", return_value="seed-1"), \
                    mock.patch.object(catalog, "write_database", side_effect=fake_write):
                catalog.build(args)
        self.assertIn(("write", False), events)
        self.assertEqual(("catalog_ready", True), events[-1])

    def test_warmup_reports_start_even_when_landing_page_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,start_month TEXT)")
            callbacks = []
            store = SimpleNamespace(read=lambda: {
                "ui": {"pageSize": 12},
                "metadata": {"network": {}},
                "components": {"aniRss": {"mode": "manual"}},
            })
            with mock.patch.dict(os.environ, {"ANM_IMAGE_PRELOAD_STATE": str(root / "preload.json")}, clear=False):
                warmup = catalog.CatalogWarmup(
                    db_path, store, None, interactive=lambda: False, started_callback=callbacks.append)
                with mock.patch.object(catalog, "query_catalog", return_value={"items": []}):
                    warmup._run("seed", 0)
            self.assertEqual(1, len(callbacks))
            self.assertEqual(12, callbacks[0]["pageSize"])

    def test_image_refresh_window_uses_72_24_72_hour_schedule(self):
        now = dt.datetime(2026, 9, 2, 12, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,raw_date TEXT,start_month TEXT)")
                db.execute("CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,fetched_at TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?,?,?)", [
                    (1, "2026-08-20", "2026-08"),
                    (2, "2026-08-20", "2026-08"),
                    (3, "2026-06-01", "2026-06"),
                    (4, "", "2026-07"),
                    (5, "2026-07-02", "2026-07"),
                    (6, "2027-02-20", "2027-02"),
                    (7, "2027-04-01", "2027-04"),
                    (8, "2026-10-15", "2026-10"),
                ])
                db.executemany("INSERT INTO anime_image VALUES(?,?)", [
                    (1, "2026-08-31T00:00:00+00:00"),
                    (2, "2026-09-02T06:00:00+00:00"),
                    (3, "2026-08-31T00:00:00+00:00"),
                    (4, "2026-08-29T00:00:00+00:00"),
                    (5, "2026-08-31T00:00:00+00:00"),
                    (6, "2026-08-29T00:00:00+00:00"),
                    (7, "2026-08-20T00:00:00+00:00"),
                    (8, "2026-08-31T00:00:00+00:00"),
                ])
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                rows = db.execute(
                    "SELECT w.id,w.raw_date,w.start_month,i.fetched_at FROM anime_work w "
                    "JOIN anime_image i ON i.anime_id=w.id ORDER BY w.id"
                ).fetchall()
            due = {
                int(anime_id) for anime_id, raw_date, start_month, fetched_at in rows
                if catalog._image_refresh_due_values(raw_date, start_month, fetched_at, now=now)
            }
            self.assertEqual({1, 4, 6, 8}, due)

    def test_image_maintenance_waits_for_warm_state_and_is_due_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SimpleNamespace(read=lambda: {"metadata": {"network": {}}})
            with mock.patch.dict(os.environ, {"ANM_IMAGE_PRELOAD_STATE": str(root / "preload.json")}, clear=False):
                warmup = catalog.CatalogWarmup(
                    root / "catalog.sqlite3", store, None, interactive=lambda: False)
            self.assertLessEqual(warmup.next_image_maintenance, time.monotonic())

            warmup.state["state"] = "warming"
            with mock.patch.object(warmup.stop_event, "wait", side_effect=[False, True]), \
                    mock.patch.object(warmup, "_catalog_marker", return_value=None), \
                    mock.patch.object(warmup, "_retry_due_ids", return_value=[]) as retry, \
                    mock.patch.object(warmup, "_maintenance_due_ids", return_value=[]) as maintenance:
                warmup._watch()
            retry.assert_not_called()
            maintenance.assert_not_called()

            warmup.state["state"] = "warm"
            warmup.next_image_maintenance = 0
            with mock.patch.object(warmup.stop_event, "wait", side_effect=[False, True]), \
                    mock.patch.object(warmup, "_catalog_marker", return_value=None), \
                    mock.patch.object(warmup, "_retry_due_ids", return_value=[]) as retry, \
                    mock.patch.object(warmup, "_maintenance_due_ids", return_value=[]) as maintenance:
                warmup._watch()
            retry.assert_called_once_with()
            maintenance.assert_called_once_with()
            self.assertGreater(warmup.next_image_maintenance, time.monotonic())

    def test_image_maintenance_scan_finds_due_recent_work_without_rewarming_catalog(self):
        now = dt.datetime.now(dt.timezone.utc)
        recent_date = (now.date() - dt.timedelta(days=10)).isoformat()
        old_date = (now.date() - dt.timedelta(days=100)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,raw_date TEXT,start_month TEXT)")
                db.execute("CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,image_blob BLOB,fetched_at TEXT,error TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?,?,?)", [
                    (1, recent_date, recent_date[:7]),
                    (2, old_date, old_date[:7]),
                    (3, recent_date, recent_date[:7]),
                    (4, recent_date, recent_date[:7]),
                    (5, recent_date, recent_date[:7]),
                ])
                db.executemany("INSERT INTO anime_image VALUES(?,?,?,?)", [
                    (1, b"cover", (now - dt.timedelta(days=2)).isoformat(), None),
                    (2, b"cover", (now - dt.timedelta(days=4)).isoformat(), None),
                    (3, None, (now - dt.timedelta(days=2)).isoformat(), None),
                    (4, b"cover", (now - dt.timedelta(days=2)).isoformat(), "ReadTimeout"),
                    (5, None, (now - dt.timedelta(days=2)).isoformat(), "no_cover"),
                ])
            warmup = object.__new__(catalog.CatalogWarmup)
            warmup.db_path = db_path
            self.assertEqual([1, 5], warmup._maintenance_due_ids())

    def test_image_maintenance_limit_is_applied_after_due_filtering(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,raw_date TEXT,start_month TEXT)")
                db.execute("CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,image_blob BLOB,fetched_at TEXT,error TEXT)")
                for anime_id in range(1, 601):
                    db.execute("INSERT INTO anime_work VALUES(?,?,?)", (anime_id, "2026-07-01", "2026-07"))
                    db.execute("INSERT INTO anime_image VALUES(?,?,?,NULL)",
                               (anime_id, b"cover", "2026-01-01T00:00:00+00:00"))
                db.execute("INSERT INTO anime_work VALUES(?,?,?)", (999, "2026-08-20", "2026-08"))
                db.execute("INSERT INTO anime_image VALUES(?,?,?,NULL)",
                           (999, b"cover", "2026-08-29T00:00:00+00:00"))
            warmup = object.__new__(catalog.CatalogWarmup)
            warmup.db_path = db_path
            with mock.patch.object(catalog.dt, "datetime", wraps=catalog.dt.datetime) as mocked_datetime:
                mocked_datetime.now.return_value = dt.datetime(2026, 9, 2, 12, tzinfo=dt.timezone.utc)
                self.assertEqual([999], warmup._maintenance_due_ids(limit=512))

    def test_image_retry_scan_revisits_transient_missing_without_rechecking_no_cover_outside_maintenance(self):
        now = dt.datetime.now(dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY)")
                db.execute("CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,image_blob BLOB,fetched_at TEXT,error TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?)", [(1,), (2,), (3,), (4,), (5,)])
                db.executemany("INSERT INTO anime_image VALUES(?,?,?,?)", [
                    (1, b"cover", now.isoformat(), None),
                    (2, None, now.isoformat(), "ReadTimeout"),
                    (3, None, (now - dt.timedelta(days=2)).isoformat(), "no_cover"),
                    (4, None, now.isoformat(), "no_cover"),
                ])
            warmup = object.__new__(catalog.CatalogWarmup)
            warmup.db_path = db_path
            self.assertEqual([5, 2], warmup._retry_due_ids())

    def test_background_budget_pauses_noninteractive_work_in_confirmed_offline_mode(self):
        budget = catalog.BackgroundTaskBudget(None, interactive=lambda: False)
        with mock.patch.object(catalog.network_connectivity, "is_offline", return_value=True):
            self.assertEqual(0, budget._capacity())
            self.assertEqual(0, budget.snapshot()["externalCapacity"])

    def test_background_budget_reserves_image_capacity_for_other_modules(self):
        class FakeFetcher:
            def __init__(self):
                self.reserves = []
            def snapshot(self):
                return {"budget": {"adaptiveCapacity": 9}}
            def set_background_reserve(self, slots):
                self.reserves.append(slots)

        fetcher = FakeFetcher()
        budget = catalog.BackgroundTaskBudget(fetcher, interactive=lambda: False)
        with budget.lease("aniRss") as first:
            self.assertTrue(first)
            self.assertEqual(1, fetcher.reserves[-1])
            with budget.lease("metadata") as second:
                self.assertTrue(second)
                self.assertEqual(2, fetcher.reserves[-1])
        self.assertEqual(0, fetcher.reserves[-1])
        self.assertEqual({}, budget.snapshot()["active"])

    def test_performance_baseline_records_each_event_once(self):
        baseline = catalog.PerformanceBaseline(100.0)
        with mock.patch.object(catalog.time, "monotonic", side_effect=[101.25, 103.0]):
            self.assertTrue(baseline.mark("catalogReady"))
            self.assertFalse(baseline.mark("catalogReady"))
            self.assertTrue(baseline.mark("firstScreen"))
        snapshot = baseline.snapshot()
        self.assertEqual(1250, snapshot["catalogReadyMs"])
        self.assertEqual(3000, snapshot["firstScreenMs"])
        self.assertIsNone(snapshot["warmCompleteMs"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "performance.json"
            first = catalog.PerformanceBaseline(100.0, path)
            with mock.patch.object(catalog.time, "monotonic", return_value=101.0):
                first.mark("catalogReady")
            second = catalog.PerformanceBaseline(200.0, path)
            self.assertEqual(1000, second.snapshot()["previous"]["catalogReadyMs"])

    def test_image_preload_controls_and_cached_progress_persist(self):
        class FakeFetcher:
            workers = 8
            def __init__(self):
                self.controls = []
            def set_background_limits(self, **values):
                self.controls.append(values)
                return {}
            def snapshot(self):
                return {"workers": self.workers}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ANM_IMAGE_PRELOAD_STATE": str(Path(directory) / "preload.json")}):
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,start_month TEXT)")
                db.execute("CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,image_blob BLOB,error TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?,?)", [(1,"2026-08"),(2,"2026-07"),(3,"2026-06"),(4,"2026-05")])
                db.executemany("INSERT INTO anime_image VALUES(?,?,?)", [(1,b"image",None),(2,None,"no_cover"),(3,None,"ReadTimeout")])
            fetcher = FakeFetcher()
            warmup = catalog.CatalogWarmup(db_path, object(), fetcher, interactive=lambda: False)
            state = warmup.control(paused=True, concurrency=3, bandwidth_kib=256)
            self.assertTrue(state["controls"]["paused"])
            self.assertEqual(3, state["controls"]["concurrency"])
            self.assertEqual(256, state["controls"]["bandwidthKiBps"])
            self.assertEqual(2, warmup._cached_count("start_month>=?", ("2026-01",)))
            self.assertEqual([3, 4], warmup._needed_ids([1, 2, 3, 4]))
            restored = catalog.CatalogWarmup(db_path, object(), FakeFetcher(), interactive=lambda: False)
            self.assertEqual(state["controls"], restored.snapshot()["controls"])

    def test_image_preload_restores_controls_with_current_worker_limits(self):
        class FakeFetcher:
            workers = 4
            def set_background_limits(self, **_values):
                return {}
            def snapshot(self):
                return {"workers": self.workers}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ANM_IMAGE_PRELOAD_STATE": str(Path(directory) / "preload.json")}):
            state_path = Path(directory) / "preload.json"
            state_path.write_text(json.dumps({
                "schemaVersion": 1,
                "state": "idle",
                "controls": {"paused": "invalid", "concurrency": 32, "bandwidthKiBps": "invalid"},
            }), encoding="utf-8")
            warmup = catalog.CatalogWarmup(Path(directory) / "catalog.sqlite3", object(), FakeFetcher(), interactive=lambda: False)
            self.assertEqual({"paused": False, "concurrency": 4, "bandwidthKiBps": 0}, warmup.snapshot()["controls"])


    def test_image_warmup_waits_for_confirmed_offline_recovery_before_enqueueing(self):
        class FakeFetcher:
            workers = 2
            def __init__(self):
                self.calls = []
            def set_background_limits(self, **_values):
                return {}
            def set_foreground_pressure(self, _active):
                return None
            def enqueue(self, anime_id, _network, **_values):
                self.calls.append(int(anime_id))
                return True
            def snapshot(self):
                return {"workers": self.workers}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"ANM_IMAGE_PRELOAD_STATE": str(Path(directory) / "preload.json")}, clear=False):
            fetcher = FakeFetcher()
            warmup = catalog.CatalogWarmup(Path(directory) / "catalog.sqlite3", object(), fetcher, interactive=lambda: False)
            catalog.network_connectivity.reset()
            catalog.network_connectivity.set_forced_offline(True)
            result = []
            thread = threading.Thread(
                target=lambda: result.append(warmup._enqueue([7], {}, generation=warmup.generation, stage="recent")))
            thread.start()
            time.sleep(.08)
            self.assertEqual([], fetcher.calls)
            self.assertEqual("waiting_network", warmup.snapshot()["state"])
            catalog.network_connectivity.set_forced_offline(False)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual([True], result)
            self.assertEqual([7], fetcher.calls)
            self.assertEqual("warming", warmup.snapshot()["state"])
            catalog.network_connectivity.reset()

    def test_image_preload_snapshot_reports_overall_remaining_and_current_item(self):
        class FakeFetcher:
            workers = 8
            def set_background_limits(self, **_values):
                return {}
            def snapshot(self):
                return {"workers": self.workers}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ANM_IMAGE_PRELOAD_STATE": str(Path(directory) / "preload.json")}):
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,start_month TEXT,title_zh_hans TEXT,title_en TEXT,title_ja TEXT)")
                db.execute("CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,image_blob BLOB,error TEXT)")
                db.execute("INSERT INTO anime_work VALUES(1,'2026-08','新作','','')")
            warmup = catalog.CatalogWarmup(db_path, object(), FakeFetcher(), interactive=lambda: False)
            with warmup.lock:
                warmup.state["stages"] = {
                    "recent": {"done": 6, "total": 10, "failed": 0},
                    "history": {"done": 2, "total": 20, "failed": 0},
                    "retry": {"done": 0, "total": 3, "failed": 3},
                }
                warmup.state["retryPending"] = 3
                warmup.state["stage"] = "history"
            warmup._set_current([1], "history")
            warmup.throughput_ewma = 2.5
            snapshot = warmup.snapshot()
            self.assertEqual(30, snapshot["overallTotal"])
            self.assertEqual(8, snapshot["overallDone"])
            self.assertEqual(25, snapshot["estimatedRemaining"])
            self.assertEqual(2.5, snapshot["throughputItemsPerSecond"])
            self.assertEqual(10, snapshot["estimatedSeconds"])
            self.assertEqual("新作", snapshot["current"]["title"])

    def test_priority_preload_query_matches_random_landing_pages(self):
        warmup = object.__new__(catalog.CatalogWarmup)
        warmup.db_path = DB
        params = warmup._params({"ui": {"filterDefaults": {}}, "metadata": {}}, "seed", 0, 12)
        self.assertEqual(["random"], params["sort"])
        self.assertEqual(["asc"], params["direction"])
        self.assertEqual(["seed"], params["seed"])
        self.assertNotIn("start_from", params)
        self.assertNotIn("start_to", params)
        self.assertNotIn("country", params)
        self.assertEqual(["local", "external", "submitted", "not_in_library"], params["library_state"])

    def test_prepared_through_month_only_moves_backward(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,start_month TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?,?)", [
                    (1, "2026-08"), (2, "2026-07"), (3, "2026-05"), (4, "2027-01"),
                ])
            warmup = object.__new__(catalog.CatalogWarmup)
            warmup.db_path = db_path
            warmup.lock = threading.RLock()
            warmup.state = {"preparedThroughMonth": ""}
            warmup._persist = lambda: None
            warmup._mark_prepared_through([1, 2])
            self.assertEqual("2026-07", warmup.state["preparedThroughMonth"])
            warmup._mark_prepared_through([3])
            self.assertEqual("2026-05", warmup.state["preparedThroughMonth"])
            warmup._mark_prepared_through([4])
            self.assertEqual("2026-05", warmup.state["preparedThroughMonth"])

    def test_exact_year_display_does_not_double_filter_explicit_month_range(self):
        base = catalog.query_catalog(DB, {
            "start_from": ["1995-10"], "start_to": ["1995-10"], "limit": ["all"],
        })
        derived_year = catalog.query_catalog(DB, {
            "start_from": ["1995-10"], "start_to": ["1995-10"], "era": ["2026"], "limit": ["all"],
        })
        self.assertGreater(base["total"], 0)
        self.assertEqual([item["id"] for item in base["items"]], [item["id"] for item in derived_year["items"]])

    def test_recent_range_is_current_month_plus_previous_five_calendar_months(self):
        class FixedDate(dt.date):
            @classmethod
            def today(cls):
                return cls(2026, 9, 3)

        with mock.patch.object(catalog.dt, "date", FixedDate):
            self.assertEqual(("2026-04", "2026-09"), catalog.CatalogWarmup._recent_range())

    def test_history_preload_groups_months_strictly_newest_to_oldest(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,start_month TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?,?)", [
                    (1, "2026-03"), (2, "2026-02"), (3, "2026-03"), (4, None), (5, ""),
                ])
            warmup = object.__new__(catalog.CatalogWarmup)
            warmup.db_path = db_path
            groups = list(warmup._history_batches_by_month("2026-04", 10))
            self.assertEqual([(0, [3, 1]), (1, [2]), (2, [5, 4])], groups)

    def test_future_preload_groups_months_nearest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,start_month TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?,?)", [
                    (1, "2026-11"), (2, "2026-10"), (3, "2027-01"), (4, None), (5, ""),
                ])
            warmup = object.__new__(catalog.CatalogWarmup)
            warmup.db_path = db_path
            groups = list(warmup._future_batches_by_month("2026-09", 10))
            self.assertEqual([(0, [2]), (1, [1]), (2, [3])], groups)

    def test_cover_history_batches_walk_backward_and_put_unknown_dates_last(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,start_month TEXT)")
                db.executemany("INSERT INTO anime_work VALUES(?,?)", [
                    (1, "2026-03"), (2, "2025-12"), (3, "2026-02"), (4, None), (5, ""),
                ])
            warmup = object.__new__(catalog.CatalogWarmup)
            warmup.db_path = db_path
            total, batches = warmup._direct_batches(
                "start_month<? OR start_month IS NULL OR start_month=''", ("2026-04",), 2)
            self.assertEqual(5, total)
            self.assertEqual([[1, 3], [2, 5], [4]], list(batches))

    def test_product_filters_random_paging_and_normalized_values(self):
        options = catalog.catalog_options(DB)
        self.assertEqual(len(options["tags"]), 23)
        self.assertEqual(options["tags"], list(catalog.THEME_DISPLAY_ORDER))
        self.assertEqual(set(catalog.THEME_DISPLAY_ORDER), set(catalog.THEME_CLUSTERS))
        self.assertEqual(options["tags"][:5], ["fantasy", "comedy", "action", "scifi", "romance"])
        self.assertEqual(options["tags"][-3:], ["horror", "josei", "workplace"])
        self.assertNotIn("slice_of_life", options["tags"])
        self.assertIn("workplace", options["tags"])
        self.assertIn("time_travel", options["tags"])
        self.assertIn("galgame", options["tags"])
        self.assertIn("yuri", options["tags"])
        self.assertIn("magical_girl", options["tags"])
        self.assertIn("harem", options["tags"])
        self.assertIn("children", options["tags"])
        self.assertIn("avant_garde", options["tags"])
        self.assertNotIn("shounen", options["tags"])
        self.assertNotIn("seinen", options["tags"])
        self.assertNotIn("shoujo", options["tags"])
        self.assertNotIn("drama", options["tags"])
        self.assertEqual(options["media_types"], ["tv", "movie", "web", "ova", "other"])
        self.assertEqual(options["source_types"], ["light_novel", "manga", "game", "novel", "original", "other"])
        self.assertEqual(options["eras"][0], "future_or_unknown")
        self.assertEqual(options["eras"][-1], "before1980")

        light = catalog.query_catalog(DB, {"source_type": ["light_novel"], "limit": ["all"]})
        novel = catalog.query_catalog(DB, {"source_type": ["novel"], "limit": ["all"]})
        self.assertTrue(all(item["source_code"] == "light_novel" for item in light["items"]))
        self.assertTrue(all(item["source_code"] == "novel" for item in novel["items"]))
        first = catalog.query_catalog(DB, {"limit": ["30"], "sort": ["random"], "seed": ["stable"]})
        second = catalog.query_catalog(DB, {"limit": ["30"], "sort": ["random"], "seed": ["stable"]})
        self.assertEqual([x["id"] for x in first["items"]], [x["id"] for x in second["items"]])
        self.assertTrue(all("media_code" in x and isinstance(x["studios"], list) for x in first["items"]))
        studio = next((x for x in options["studios"] if x != "__other__"), "__other__")
        matches = catalog.query_catalog(DB, {"studio": [studio]})
        self.assertTrue(matches["items"])
        if studio != "__other__":
            self.assertTrue(all(studio in x["studios"] for x in matches["items"]))
        self.assertNotIn("countries", options)
        baseline = catalog.query_catalog(DB, {"limit": ["all"]})
        ignored_region = catalog.query_catalog(DB, {"country": ["JP"], "limit": ["all"]})
        self.assertEqual([item["id"] for item in baseline["items"]], [item["id"] for item in ignored_region["items"]])
        series = catalog.query_catalog(DB, {"series": ["yes"], "limit": ["all"]})
        standalone = catalog.query_catalog(DB, {"series": ["no"], "limit": ["all"]})
        self.assertTrue(all(item["series_member_count"] > 1 for item in series["items"]))
        self.assertTrue(all(item["series_member_count"] == 1 for item in standalone["items"]))
        self.assertEqual(series["total"] + standalone["total"], 10)

    def test_theme_taxonomy_uses_content_tags_not_removed_demographics(self):
        self.assertEqual(catalog.theme_codes(["穿越", "GALGAME科普"]), ["time_travel", "galgame"])
        self.assertEqual(catalog.theme_codes(["GAL改", "萝卜"]), ["mecha", "galgame"])
        self.assertEqual(catalog.theme_codes(["百合", "魔法少女"]), ["yuri", "magical_girl"])
        self.assertEqual(catalog.theme_codes(["纯爱"]), ["romance"])
        self.assertEqual(catalog.theme_codes(["后宫", "子供向", "意识流"]), ["harem", "children", "avant_garde"])
        self.assertFalse({"shounen", "seinen", "shoujo"} & set(catalog.THEME_CLUSTERS))
        self.assertNotIn("drama", catalog.THEME_CLUSTERS)

    def test_broad_comedy_tag_requires_prominence_or_corroboration(self):
        self.assertNotIn("comedy", catalog.theme_codes(["搞笑"]))
        self.assertIn("comedy", catalog.theme_codes([{"name": "搞笑", "count": 60, "rank": 2}, {"name": "日常", "count": 100, "rank": 1}]))
        self.assertIn("comedy", catalog.theme_codes(["搞笑", "吐槽"]))
        self.assertIn("comedy", catalog.theme_codes(["喜剧"]))

    def test_studio_collaboration_is_split_and_rename_family_is_clustered(self):
        self.assertEqual(catalog.split_studio_credit("SynergySP&スタジオコメット"), ["SynergySP", "スタジオコメット"])
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.executescript("""
                CREATE TABLE anime_studio(anime_id INTEGER,studio TEXT,UNIQUE(anime_id,studio));
                CREATE TABLE anime_studio_cluster(anime_id INTEGER,cluster_key TEXT,cluster_name TEXT,studio_name TEXT,UNIQUE(anime_id,cluster_key,studio_name));
            """)
            db.executemany("INSERT INTO anime_studio VALUES(?,?)", [
                (1, "SynergySP"), (1, "SynergySP&スタジオコメット"), (1, "スタジオコメット"),
                (2, "サンライズ"), (2, "バンダイナムコフィルムワークス"),
            ])
            catalog.rebuild_studio_clusters(db)
            first = [x[0] for x in db.execute("SELECT DISTINCT cluster_name FROM anime_studio_cluster WHERE anime_id=1 ORDER BY cluster_name")]
            second = [x[0] for x in db.execute("SELECT DISTINCT cluster_name FROM anime_studio_cluster WHERE anime_id=2")]
            self.assertEqual(first, ["SynergySP", "スタジオコメット"])
            self.assertEqual(second, ["サンライズ／バンダイナムコフィルムワークス"])

    def test_new_database_persists_tag_evidence(self):
        work = {
            "bgm_id": 99999999, "wikidata_id": None, "title_ja": "試験", "title_zh_hans": "测试", "title_en": "Test",
            "media_type": "TV", "media_code": "tv", "start_month": "2024-01", "directory_date": "2024_01",
            "raw_date": "2024-01-01", "episode_count": 1, "source_type": "原创", "source_code": "original",
            "original_language": "ja", "country_code": "JP", "studio": "Studio A&Studio B", "summary": "", "source_url": "https://example.invalid",
        }
        row = catalog.BuildItem({}, work, [], [{"name": "搞笑", "count": 80, "rank": 2}], [], [], [])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            catalog.write_database(path, [row])
            self.assertFalse(path.with_suffix(path.suffix + ".next").exists())
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT vote_count,tag_rank FROM anime_tag").fetchone(), (80, 2))
                self.assertEqual(db.execute("SELECT accepted FROM anime_theme_evidence WHERE theme_code='comedy'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(DISTINCT cluster_key) FROM anime_studio_cluster").fetchone()[0], 2)

    def test_verified_external_media_remains_library_state_not_download_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                anime_id = int(db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0])
                db.execute("INSERT INTO external_library_source VALUES('ani-rss','ani-rss','/external',1,'ready','now','{}')")
                db.execute("""INSERT INTO external_media_file VALUES(
                    'ani-rss','/external/example/E01.mkv',1,1,?,'verified','Example',2024,'tv',1,1,'{}','now')""", (anime_id,))
            torrent = catalog.query_catalog(path, {"availability": ["torrent"], "limit": ["all"]})
            ani = catalog.query_catalog(path, {"availability": ["ani-rss"], "limit": ["all"]})
            unavailable = catalog.query_catalog(path, {"availability": ["unavailable"], "library_state": ["external"], "limit": ["all"]})
            torrent_ids = {int(row["id"]) for row in torrent["items"]}
            ani_ids = {int(row["id"]) for row in ani["items"]}
            unavailable_ids = {int(row["id"]) for row in unavailable["items"]}
            self.assertNotIn(anime_id, torrent_ids)
            self.assertNotIn(anime_id, ani_ids)
            self.assertIn(anime_id, unavailable_ids)
            item = next(row for row in unavailable["items"] if int(row["id"]) == anime_id)
            self.assertTrue(item["has_external_media"])
            self.assertEqual(item["external_media_count"], 1)
            self.assertEqual(item["library_state"], "external")

    def test_ani_rss_http_media_is_external_read_only_library_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                ani_rss.migrate(db)
                anime_id = int(db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0])
                db.execute("""INSERT INTO ani_rss_subscription VALUES(
                    'http-media',?,'HTTP Media',NULL,1,'follow',2,12,NULL,'enabled','now','now',0,NULL,'{}')""", (anime_id,))
                db.execute("""INSERT INTO ani_rss_media VALUES(
                    'http-media',?,'/remote/E02.mkv',2,'E02','E02.mkv',1234,'mkv','now')""", (anime_id,))
                db.execute("""INSERT INTO ani_rss_state VALUES(
                    1,'http://127.0.0.1:7789','test','ready','prefer','prefer','now','now',NULL,1,?)""",
                    (ani_rss._credential_fingerprint("test-key", "http://127.0.0.1:7789"),))
            config = json.loads(Path(catalog.EXAMPLE_CONFIG).read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"ANM_ANI_RSS_API_KEY": "test-key"}, clear=False):
                external = catalog.query_catalog(path, {"library_state": ["external"], "limit": ["all"]}, config)
                self.assertIn(anime_id, {int(item["id"]) for item in external["items"]})
                item = next(item for item in external["items"] if int(item["id"]) == anime_id)
                self.assertEqual(item["library_state"], "external")
                detail = catalog.catalog_detail(path, anime_id, config)
            targets = detail["library"]["targets"]
            remote = next(target for target in targets if target.get("origin") == "ani-rss_api")
            self.assertEqual(remote["path"], "ani-rss:http-media")
            self.assertEqual(remote["state"], "external")
            self.assertEqual(remote["fileCount"], 1)

    def test_ani_rss_http_media_sets_external_media_card_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                ani_rss.migrate(db)
                anime_id = int(db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0])
                db.execute("""INSERT INTO ani_rss_subscription VALUES(
                    'http-media-card',?,'HTTP Media',NULL,1,'follow',2,12,NULL,'enabled','now','now',0,NULL,'{}')""", (anime_id,))
                db.execute("""INSERT INTO ani_rss_media VALUES(
                    'http-media-card',?,'/remote/E02.mkv',2,'E02','E02.mkv',1234,'mkv','now')""", (anime_id,))
                db.execute("""INSERT INTO ani_rss_state VALUES(
                    1,'http://127.0.0.1:7789','test','ready','prefer','prefer','now','now',NULL,1,?)""",
                    (ani_rss._credential_fingerprint("test-key", "http://127.0.0.1:7789"),))
            config = json.loads(Path(catalog.EXAMPLE_CONFIG).read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"ANM_ANI_RSS_API_KEY": "test-key"}, clear=False):
                external = catalog.query_catalog(path, {"library_state": ["external"], "limit": ["all"]}, config)
            item = next(item for item in external["items"] if int(item["id"]) == anime_id)
            self.assertTrue(item["has_external_media"])
            self.assertEqual(1, item["ani_rss_media_count"])

    def test_stale_ani_rss_http_media_is_not_external_when_connection_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                ani_rss.migrate(db)
                anime_id = int(db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0])
                db.execute("""INSERT INTO ani_rss_subscription VALUES(
                    'stale-http-media',?,'Stale HTTP Media',NULL,1,'follow',2,12,NULL,'enabled','now','now',0,NULL,'{}')""", (anime_id,))
                db.execute("""INSERT INTO ani_rss_media VALUES(
                    'stale-http-media',?,'/remote/E02.mkv',2,'E02','E02.mkv',1234,'mkv','now')""", (anime_id,))
                db.execute("""INSERT INTO ani_rss_state VALUES(
                    1,'http://127.0.0.1:7789','test','error','prefer','manual','now','now','RuntimeError: unavailable',1,?)""",
                    (ani_rss._credential_fingerprint("test-key", "http://127.0.0.1:7789"),))
            config = json.loads(Path(catalog.EXAMPLE_CONFIG).read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"ANM_ANI_RSS_API_KEY": "test-key"}, clear=False):
                all_items = catalog.query_catalog(path, {"limit": ["all"]}, config)
                external = catalog.query_catalog(path, {"library_state": ["external"], "limit": ["all"]}, config)
                detail = catalog.catalog_detail(path, anime_id, config)
            item = next(item for item in all_items["items"] if int(item["id"]) == anime_id)
            self.assertFalse(item["has_external_media"])
            self.assertEqual(0, item["ani_rss_media_count"])
            self.assertNotEqual("external", item["library_state"])
            self.assertNotIn(anime_id, {int(entry["id"]) for entry in external["items"]})
            self.assertFalse(any(target.get("origin") == "ani-rss_api" for target in detail["library"]["targets"]))

    def test_ani_rss_availability_requires_current_healthy_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                ani_rss.migrate(db)
                anime_id = int(db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0])
                db.execute("""INSERT INTO ani_rss_subscription VALUES(
                    'availability-test',?,'Availability',NULL,1,'follow',1,12,NULL,'enabled','now','now',0,NULL,'{}')""", (anime_id,))
                db.execute("""INSERT INTO ani_rss_state VALUES(
                    1,'http://127.0.0.1:7789','test','ready','prefer','prefer','now','now',NULL,1,?)""",
                    (ani_rss._credential_fingerprint("test-key", "http://127.0.0.1:7789"),))
            config = json.loads(Path(catalog.EXAMPLE_CONFIG).read_text(encoding="utf-8"))
            config.setdefault("components", {}).setdefault("aniRss", {}).update({
                "endpoint": "http://127.0.0.1:7789", "mode": "prefer"})
            with mock.patch.dict(os.environ, {"ANM_ANI_RSS_API_KEY": "test-key"}, clear=False):
                ready = catalog.query_catalog(path, {"availability": ["ani-rss"], "limit": ["all"]}, config)
                self.assertIn(anime_id, {int(item["id"]) for item in ready["items"]})
                ready_all = catalog.query_catalog(path, {"limit": ["all"]}, config)
                ready_item = next(item for item in ready_all["items"] if int(item["id"]) == anime_id)
                self.assertTrue(ready_item["ani_rss_managed"])
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANM_ANI_RSS_API_KEY", None)
                unavailable = catalog.query_catalog(path, {"availability": ["ani-rss"], "limit": ["all"]}, config)
                self.assertNotIn(anime_id, {int(item["id"]) for item in unavailable["items"]})
                unavailable_all = catalog.query_catalog(path, {"limit": ["all"]}, config)
                unavailable_item = next(item for item in unavailable_all["items"] if int(item["id"]) == anime_id)
                self.assertFalse(unavailable_item["ani_rss_managed"])
                self.assertEqual(0, unavailable_item["ani_rss_resource_count"])

    def test_four_way_library_state_priority_and_legacy_filter_compatibility(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                ani_rss.migrate(db)
                ids = [int(row[0]) for row in db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 4")]
                now = "now"
                db.execute("""INSERT INTO runtime_work VALUES(1,?,'/local',NULL,'local','Local','2026_01',NULL,'existing','active','standalone','preexisting_local','test','{}',?)""", (ids[0], now))
                db.execute("""INSERT INTO runtime_work VALUES(2,?,'/queued',NULL,'queued','Queued','2026_01',NULL,'queued','active','standalone','managed_submission','test','{}',?)""", (ids[1], now))
                db.execute("INSERT INTO external_library_source VALUES('ext','generic','/external',1,'ready','now','{}')")
                db.execute("""INSERT INTO external_media_file VALUES(
                    'ext','/external/E01.mkv',1,1,?,'verified','External',2026,'tv',1,1,'{}','now')""", (ids[2],))
            expected = {ids[0]: "local", ids[1]: "submitted", ids[2]: "external", ids[3]: "not_in_library"}
            result = catalog.query_catalog(path, {"library_state": ["local", "external", "submitted", "not_in_library"], "limit": ["all"]})
            states = {int(item["id"]): item["library_state"] for item in result["items"] if int(item["id"]) in expected}
            self.assertEqual(states, expected)
            legacy = catalog.query_catalog(path, {"library_state": ["queued"], "limit": ["all"]})
            self.assertIn(ids[1], {int(item["id"]) for item in legacy["items"]})

    def test_four_way_library_state_conflicts_keep_exclusive_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                ani_rss.migrate(db)
                ids = [int(row[0]) for row in db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 2")]
                now = "now"
                # Local + external + submitted must stay local.
                db.execute("""INSERT INTO runtime_work VALUES(1,?,'/local',NULL,'local','Local','2026_01',NULL,'existing','active','standalone','preexisting_local','test','{}',?)""", (ids[0], now))
                db.execute("""INSERT INTO runtime_work VALUES(2,?,'/queued-local',NULL,'queued-local','Queued','2026_01',NULL,'queued','active','standalone','managed_submission','test','{}',?)""", (ids[0], now))
                # External + submitted must stay external.
                db.execute("""INSERT INTO runtime_work VALUES(3,?,'/queued-external',NULL,'queued-external','Queued','2026_01',NULL,'downloading','active','standalone','managed_submission','test','{}',?)""", (ids[1], now))
                db.execute("INSERT INTO external_library_source VALUES('ext-priority','generic','/external',1,'ready','now','{}')")
                db.executemany("""INSERT INTO external_media_file VALUES(
                    'ext-priority',?,1,1,?,'verified','Priority',2026,'tv',1,1,'{}','now')""", [
                        ('/external/local/E01.mkv', ids[0]), ('/external/external/E01.mkv', ids[1])])
                self.assertEqual("local", catalog.runtime_catalog.collection_state(db, ids[0]))
                self.assertEqual("external", catalog.runtime_catalog.collection_state(db, ids[1]))
            result = catalog.query_catalog(
                path, {"library_state": ["local", "external", "submitted", "not_in_library"], "limit": ["all"]})
            states = {int(item["id"]): item["library_state"] for item in result["items"] if int(item["id"]) in ids}
            self.assertEqual({ids[0]: "local", ids[1]: "external"}, states)

    def test_disabled_region_removes_ani_rss_source_from_catalog_availability(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                ani_rss.migrate(db)
                row = db.execute("""SELECT w.id FROM anime_work w
                    WHERE EXISTS(SELECT 1 FROM anime_country c WHERE c.anime_id=w.id AND c.country_code='JP')
                    ORDER BY w.id LIMIT 1""").fetchone()
                self.assertIsNotNone(row)
                anime_id = int(row[0])
                db.execute("""INSERT INTO ani_rss_subscription VALUES(
                    'region-test',?,'Region Test',NULL,1,'follow',1,1,NULL,'enabled','now','now',0,NULL,'{}')""", (anime_id,))
            config = json.loads(Path(catalog.EXAMPLE_CONFIG).read_text(encoding="utf-8"))
            config["torrentPolicy"]["regions"]["japan"] = False
            ani = catalog.query_catalog(path, {"availability": ["ani-rss"], "limit": ["all"]}, config)
            unavailable = catalog.query_catalog(path, {"availability": ["unavailable"], "limit": ["all"]}, config)
            unfiltered = catalog.query_catalog(path, {"limit": ["all"]}, config)
            self.assertNotIn(anime_id, {int(item["id"]) for item in ani["items"]})
            self.assertNotIn(anime_id, {int(item["id"]) for item in unavailable["items"]})
            self.assertNotIn(anime_id, {int(item["id"]) for item in unfiltered["items"]})

    def test_archive_download_resumes_an_interrupted_response(self):
        payload = b"abcdef"
        descriptor = {
            "name": "archive.zip",
            "size": len(payload),
            "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "browser_download_url": "https://example.invalid/archive.zip",
        }

        class Response:
            def __init__(self, status, chunks, content_range=""):
                self.status = status
                self.headers = {"Content-Range": content_range}
                self.chunks = iter(chunks)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _size):
                value = next(self.chunks, b"")
                if isinstance(value, Exception):
                    raise value
                return value

        responses = [
            Response(200, [b"abc", urllib.error.URLError("interrupted")]),
            Response(206, [b"def", b""], "bytes 3-5/6"),
        ]
        def verified(_urls, destination, **_kwargs):
            destination.write_bytes(payload)
            return {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "urls": [descriptor["browser_download_url"]]}
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(catalog.network_sources, "fetch_json", return_value=(dict(descriptor), "manifest")), \
                mock.patch.object(catalog.network_sources, "asset_urls", return_value=[descriptor["browser_download_url"]]), \
                mock.patch.object(catalog.network_downloads, "download_verified", side_effect=verified), \
                mock.patch.object(catalog.time, "sleep"):
            path, returned = catalog.ensure_archive(Path(folder), network={"maximumAttemptsPerEndpoint": 2})
            self.assertEqual(payload, path.read_bytes())
            self.assertEqual(descriptor["digest"], returned["digest"])
            receipt = json.loads(path.with_suffix(".zip.verified.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(payload).hexdigest(), receipt["sha256"])

    def test_split_cour_physical_owner_requires_strict_relation_and_same_component(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.executescript("""
                CREATE TABLE anime_work(
                    id INTEGER PRIMARY KEY,bgm_id INTEGER,title_ja TEXT,start_month TEXT,directory_date TEXT,
                    physical_role TEXT DEFAULT 'work',physical_owner_anime_id INTEGER
                );
                CREATE TABLE anime_relation(anime_id INTEGER,related_bgm_id INTEGER,relation_code TEXT);
                CREATE TABLE anime_series_component(anime_id INTEGER,component_id INTEGER);
            """)
            db.executemany("INSERT INTO anime_work(id,bgm_id,title_ja,start_month,directory_date) VALUES(?,?,?,?,?)", [
                (1, 101, "作品", "2023-01", "2023_01"),
                (2, 102, "作品 第2クール", "2023-07", "2023_07"),
                (3, 103, "別作品 第2クール", "2024-01", "2024_01"),
            ])
            db.executemany("INSERT INTO anime_series_component VALUES(?,?)", [(1, 10), (2, 10), (3, 20)])
            db.executemany("INSERT INTO anime_relation VALUES(?,?,?)", [(2, 101, "sequel"), (3, 101, "sequel")])
            catalog.rebuild_physical_layout(db)
            self.assertEqual(("split_cour", 1), db.execute(
                "SELECT physical_role,physical_owner_anime_id FROM anime_work WHERE id=2").fetchone())
            self.assertEqual(("work", None), db.execute(
                "SELECT physical_role,physical_owner_anime_id FROM anime_work WHERE id=3").fetchone())


if __name__ == "__main__":
    unittest.main(verbosity=2)
