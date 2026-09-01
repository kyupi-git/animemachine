import sqlite3
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from animemachine.catalog import service as catalog


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

    def test_access_banner_includes_archive_seed_and_blank_padding(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output), mock.patch.dict("os.environ", {
                "ANM_PUBLIC_URL": "", "ANM_QBT_API_KEY": "", "ANM_ANI_RSS_API_KEY": "",
                "ANM_ADMIN_USERNAME": ""}, clear=False):
            catalog._print_access_info(
                "127.0.0.1", 8787, {"components": {}}, Path("catalog.sqlite3"),
                instance_seed="seed-1", archive_meta={"name": "dump.zip", "created_at": "2026-08-30"},
                record_count=1234,
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
        self.assertNotIn("API key", text)

    def test_archive_parsed_callback_runs_before_database_write(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "catalog.sqlite3"
            args = SimpleNamespace(
                archive_dir=root / "archive", archive=None, network_config={}, all_anime=True,
                manifest=None, ids=None, cache=root / "cache", cache_days=30, refresh=False, request_interval=0.0,
                db=db_path, progress_callback=None,
                archive_parsed_callback=lambda seed, meta, count: events.append(("parsed", seed, meta["name"], count)),
            )

            def fake_write(path, *_args):
                events.append(("write",))
                path.write_bytes(b"catalog")

            with mock.patch.object(catalog, "ensure_archive", return_value=(root / "archive.zip", {"name": "dump.zip"})), \
                    mock.patch.object(catalog, "wikidata_titles", return_value={}), \
                    mock.patch.object(catalog, "build_items_from_archive", return_value=[]), \
                    mock.patch.object(catalog, "instance_random_seed", return_value="seed-1"), \
                    mock.patch.object(catalog, "write_database", side_effect=fake_write):
                catalog.build(args)
        self.assertEqual(events[0], ("parsed", "seed-1", "dump.zip", 0))
        self.assertEqual(events[1], ("write",))

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
        country = options["countries"][0]
        regional = catalog.query_catalog(DB, {"country": [country]})
        self.assertTrue(regional["items"] and all(country in x["countries"] for x in regional["items"]))
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

    def test_verified_external_media_is_an_available_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copy2(DB, path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                catalog.runtime_catalog.migrate_overlay(db)
                anime_id = int(db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0])
                db.execute("INSERT INTO external_library_source VALUES('ani-rss','ani-rss','/external',1,'ready','now','{}')")
                db.execute("""INSERT INTO external_media_file VALUES(
                    'ani-rss','/external/example/E01.mkv',1,1,?,'verified','Example',2024,'tv',1,1,'{}','now')""", (anime_id,))
            available = catalog.query_catalog(path, {"availability": ["available"], "limit": ["all"]})
            unavailable = catalog.query_catalog(path, {"availability": ["unavailable"], "limit": ["all"]})
            available_ids = {int(row["id"]) for row in available["items"]}
            unavailable_ids = {int(row["id"]) for row in unavailable["items"]}
            self.assertIn(anime_id, available_ids)
            self.assertNotIn(anime_id, unavailable_ids)
            item = next(row for row in available["items"] if int(row["id"]) == anime_id)
            self.assertTrue(item["has_external_media"])
            self.assertEqual(item["external_media_count"], 1)

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
