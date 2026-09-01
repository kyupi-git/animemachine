from __future__ import annotations

import sqlite3
import tempfile
import unittest
import contextlib
from pathlib import Path

from animemachine.catalog import relation_graph


class RelationGraphTests(unittest.TestCase):
    def test_redundant_compilation_branch_is_hidden_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "compilation-branch.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER UNIQUE,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT,original_language TEXT);
                CREATE TABLE anime_relation(anime_id INTEGER,related_bgm_id INTEGER,related_title TEXT,relation_type TEXT,relation_code TEXT,strict_group INTEGER,source TEXT);
                """)
                db.executemany("INSERT INTO anime_work VALUES(?,?,?,?,?,?,?,?)", [
                    (1, 101, "本篇", None, None, "2015-07", "tv", "ja"),
                    (2, 102, "总集篇", None, None, "2017-02", "movie", "ja"),
                    (3, 103, "衍生短篇", None, None, "2015-08", "web", "ja"),
                ])
                db.executemany("INSERT INTO anime_relation VALUES(?,?,?,?,?,?,?)", [
                    (1, 102, "总集篇", "总集篇", "summary", 1, "bangumi-archive"),
                    (1, 103, "衍生短篇", "衍生", "spin_off", 1, "bangumi-archive"),
                    (2, 103, "衍生短篇", "衍生", "spin_off", 1, "bangumi-archive"),
                ])
                relation_graph.rebuild(db, force=True)
                stored = db.execute(
                    "SELECT COUNT(*) FROM anime_relation_edge WHERE relation_code='spin_off'"
                ).fetchone()[0]
                graph = relation_graph.graph_rows(db, 1)
                self.assertTrue(graph["seriesTitle"])
                self.assertEqual(2, stored)
                self.assertEqual(
                    [(1, 3)],
                    [
                        (edge["source_anime_id"], edge["target_anime_id"])
                        for edge in graph["edges"]
                        if edge["relation_code"] == "spin_off"
                    ],
                )

    def test_series_entry_is_not_inferred_without_common_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "no-evidence.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER UNIQUE,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT,original_language TEXT);
                CREATE TABLE anime_relation(anime_id INTEGER,related_bgm_id INTEGER,related_title TEXT,relation_type TEXT,relation_code TEXT,strict_group INTEGER,source TEXT);
                """)
                db.executemany("INSERT INTO anime_work VALUES(?,?,?,?,?,?,?,?)", [
                    (1, 101, "本篇", None, None, "2014-10", "tv", "ja"),
                    (2, 102, "连续电影 第1章", None, None, "2017-03", "movie", "ja"),
                    (3, 103, "连续电影 第2章", None, None, "2017-04", "movie", "ja"),
                    (4, 104, "连续电影 第3章", None, None, "2017-07", "movie", "ja"),
                ])
                db.executemany("INSERT INTO anime_relation VALUES(?,?,?,?,?,?,?)", [
                    (1, 104, "第三章", "前传", "prequel", 1, "bangumi-archive"),
                    (2, 103, "第二章", "续集", "sequel", 1, "bangumi-archive"),
                    (3, 104, "第三章", "续集", "sequel", 1, "bangumi-archive"),
                ])
                relation_graph.rebuild(db, force=True)
                graph = relation_graph.graph_rows(db, 1)
            edges = [
                (edge["source_anime_id"], edge["target_anime_id"], edge["relation_code"])
                for edge in graph["edges"]
            ]
            self.assertIn((1, 4, "prequel"), edges)
            self.assertNotIn((1, 2, "prequel"), edges)
            self.assertFalse(any(edge.get("inferred") for edge in graph["edges"]))

    def test_multipart_subseries_bridge_is_reanchored_to_entry_for_display(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "entry.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER UNIQUE,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT,original_language TEXT);
                CREATE TABLE anime_relation(anime_id INTEGER,related_bgm_id INTEGER,related_title TEXT,relation_type TEXT,relation_code TEXT,strict_group INTEGER,source TEXT);
                """)
                db.executemany("INSERT INTO anime_work VALUES(?,?,?,?,?,?,?,?)", [
                    (1, 101, "勇者本篇", None, None, "2014-10", "tv", "ja"),
                    (2, 102, "勇者前传 第1章", None, None, "2017-03", "movie", "ja"),
                    (3, 103, "勇者前传 第2章", None, None, "2017-04", "movie", "ja"),
                    (4, 104, "勇者前传 第3章", None, None, "2017-07", "movie", "ja"),
                    (5, 105, "勇者前传 TV总集篇", None, None, "2017-10", "tv", "ja"),
                ])
                db.executemany("INSERT INTO anime_relation VALUES(?,?,?,?,?,?,?)", [
                    (1, 104, "第三章", "前传", "prequel", 1, "bangumi-archive"),
                    (4, 101, "本篇", "续集", "sequel", 1, "bangumi-archive"),
                    (2, 103, "第二章", "续集", "sequel", 1, "bangumi-archive"),
                    (3, 102, "第一章", "前传", "prequel", 1, "bangumi-archive"),
                    (3, 104, "第三章", "续集", "sequel", 1, "bangumi-archive"),
                    (4, 103, "第二章", "前传", "prequel", 1, "bangumi-archive"),
                    (5, 102, "第一章", "总集篇", "summary", 1, "bangumi-archive"),
                    (5, 103, "第二章", "总集篇", "summary", 1, "bangumi-archive"),
                    (5, 104, "第三章", "总集篇", "summary", 1, "bangumi-archive"),
                ])
                relation_graph.rebuild(db, force=True)
                stored = [tuple(row) for row in db.execute(
                    "SELECT source_anime_id,target_anime_id,relation_code FROM anime_relation_edge WHERE relation_code='prequel'"
                )]
                graph = relation_graph.graph_rows(db, 1)
            self.assertIn((1, 4, "prequel"), stored)
            self.assertNotIn((1, 2, "prequel"), stored)
            inferred = [edge for edge in graph["edges"] if edge.get("inferred")]
            self.assertEqual([(1, 2, "prequel")], [
                (edge["source_anime_id"], edge["target_anime_id"], edge["relation_code"])
                for edge in inferred
            ])
            self.assertTrue(inferred[0]["provenance"].startswith("anm-inferred-series-entry:"))
            self.assertNotIn((1, 4, "prequel"), [
                (edge["source_anime_id"], edge["target_anime_id"], edge["relation_code"])
                for edge in graph["edges"]
            ])

    def test_later_released_prequel_keeps_chronological_arrow(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prequel.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER UNIQUE,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT,original_language TEXT);
                CREATE TABLE anime_relation(anime_id INTEGER,related_bgm_id INTEGER,related_title TEXT,relation_type TEXT,relation_code TEXT,strict_group INTEGER,source TEXT);
                """)
                db.executemany("INSERT INTO anime_work VALUES(?,?,?,?,?,?,?,?)", [
                    (1, 101, "Original", None, None, "2014-10", "tv", "ja"),
                    (2, 102, "Later prequel", None, None, "2017-07", "movie", "ja"),
                ])
                db.executemany("INSERT INTO anime_relation VALUES(?,?,?,?,?,?,?)", [
                    (1, 102, "Later prequel", "前传", "prequel", 1, "bangumi-archive"),
                    (2, 101, "Original", "续集", "sequel", 1, "bangumi-archive"),
                ])
                relation_graph.rebuild(db, force=True)
                edges = db.execute(
                    "SELECT source_anime_id,target_anime_id,relation_code FROM anime_relation_edge"
                ).fetchall()
            self.assertEqual([(1, 2, "prequel")], [tuple(row) for row in edges])

    def test_direction_components_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "graph.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER UNIQUE,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT,original_language TEXT);
                CREATE TABLE anime_relation(anime_id INTEGER,related_bgm_id INTEGER,related_title TEXT,relation_type TEXT,relation_code TEXT,strict_group INTEGER,source TEXT);
                """)
                db.executemany("INSERT INTO anime_work VALUES(?,?,?,?,?,?,?,?)", [
                    (1, 101, "A", None, None, "2020-01", "tv", "ja"),
                    (2, 102, "B", None, None, "2021-01", "tv", "ja"),
                    (3, 103, "C", None, None, "2021-06", "ova", "ja"),
                    (4, 104, "D", None, None, "2022-01", "tv", "ja"),
                ])
                db.executemany("INSERT INTO anime_relation VALUES(?,?,?,?,?,?,?)", [
                    (1, 102, "B", "续集", "sequel", 1, "bangumi-archive"),
                    (2, 101, "A", "前传", "prequel", 1, "bangumi-archive"),
                    (2, 103, "C", "衍生", "spin_off", 1, "bangumi-archive"),
                    (3, 102, "B", "主线故事", "main_story", 1, "bangumi-archive"),
                    (2, 103, "C", "番外", "side_story", 1, "bangumi-archive"),
                    (2, 104, "D", "相同世界观", "same_setting", 0, "bangumi-archive"),
                ])
                result = relation_graph.rebuild(db, force=True)
                self.assertEqual(4, result["edges"])
                graph = relation_graph.graph_rows(db, 1)
            self.assertIsNotNone(graph)
            self.assertEqual(3, graph["strictMemberCount"])
            self.assertEqual({1, 2, 3, 4}, {row["id"] for row in graph["nodes"]})
            sequel = [edge for edge in graph["edges"] if edge["relation_code"] == "sequel"]
            self.assertEqual([(1, 2)], [(edge["source_anime_id"], edge["target_anime_id"]) for edge in sequel])
            spin_off = [edge for edge in graph["edges"] if edge["relation_code"] == "spin_off"]
            self.assertEqual([(2, 3)], [(edge["source_anime_id"], edge["target_anime_id"]) for edge in spin_off])
            side_story = [edge for edge in graph["edges"] if edge["relation_code"] == "side_story"]
            self.assertEqual([(2, 3)], [(edge["source_anime_id"], edge["target_anime_id"]) for edge in side_story])
            self.assertFalse(next(row for row in graph["nodes"] if row["id"] == 4)["strict_member"])


if __name__ == "__main__":
    unittest.main()

