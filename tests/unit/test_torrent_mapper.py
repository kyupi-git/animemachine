from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from animemachine.torrents import mapper as torrent_mapper

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
from animemachine.catalog.migrations import migrate


class TorrentMapperTests(unittest.TestCase):
    def test_serial_suffix_and_bilingual_separator_produce_title_queries(self):
        self.assertIn("拉拉熊", torrent_mapper.queries("[ANi] 拉拉熊 - 13 [1080P][WEB-DL][CHT].mp4"))
        values = torrent_mapper.queries("[ANi] English Title _ 中文标题 - 03 [1080P][WEB-DL].mp4")
        self.assertIn("English Title", values)
        self.assertIn("中文标题", values)
        values = torrent_mapper.queries("中文标题 _ 日本語題名 S01E20", partial=True)
        self.assertIn("中文标题", values)
        self.assertIn("日本語題名", values)
        self.assertIn("Aoharu x Kikanjuu", torrent_mapper.queries(
            "[Moozzi2] Aoharu x Kikanjuu - TV + Special SP + SP"))
        self.assertIn("青春x機関銃", torrent_mapper.queries(
            "[Snow-Raws] 青春×机关枪_Aoharu x Kikanjuu_青春x機関銃 (BD 1920x1080)"))
        self.assertEqual(torrent_mapper.norm("青春×機関銃"), torrent_mapper.norm("青春x機関銃"))
    def test_unique_exact_single_work_is_mapped_without_library_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            metadata = Path(folder) / "metadata.sqlite3"
            runtime = Path(folder) / "runtime.sqlite3"
            shutil.copy2(PROJECT / "tests" / "fixtures" / "anime-catalog.sqlite3", metadata)
            with contextlib.closing(sqlite3.connect(runtime)) as db:
                migrate(db)
                info_hash = "b" * 40
                db.execute("""INSERT INTO torrent(info_hash,torrent_path,manifest_sha256,source_class,effective_group,
                    language_hint,scan_state,info_name,title_state,file_count,total_bytes,torrent_created_at,
                    release_flags_json,collection_hint,video_height,video_scan,bit_depth,asset_kind,metadata_state)
                    VALUES(?,?,?,?,?,'CHS','candidate',?,'unmapped',26,26000,'2016-01-01T00:00:00+00:00','[]',0,1080,'p',10,'torrent','available')""",
                    (info_hash, str(Path(folder) / "eva.torrent"), "digest", "BDRip", "VCB-Studio", "[VCB-Studio] 新世紀エヴァンゲリオン [BDRip 1080p]"))
                db.executemany("INSERT INTO torrent_manifest_file VALUES(?,?,?,?)",
                               ((info_hash, i, f"Eva/Episode {i + 1:02d}.mkv", 1000) for i in range(26)))
                db.commit()
            config = json.loads((PROJECT / "config" / "config.example.json").read_text(encoding="utf-8"))
            library_root = Path(folder) / "Library"
            library_root.mkdir()
            existing_target = library_root / "『1995_10』『新世紀エヴァンゲリオン』"
            existing_target.mkdir()
            (existing_target / "Episode 01.mkv").write_bytes(b"existing")
            config["deployment"]["libraryUncRoot"] = str(library_root)
            result = torrent_mapper.auto_map(metadata, runtime, config)
            self.assertEqual(1, result["mapped"])
            with contextlib.closing(sqlite3.connect(runtime)) as db:
                self.assertEqual("verified", db.execute("SELECT mapping_state FROM torrent_work").fetchone()[0])
                self.assertEqual(26, db.execute("SELECT COUNT(*) FROM file_map WHERE selected=1").fetchone()[0])
                target = db.execute("SELECT target_unc FROM anime_work").fetchone()[0]
                self.assertEqual(str(existing_target), target)
                self.assertEqual("existing", db.execute("SELECT library_state FROM anime_work").fetchone()[0])
                db.execute("UPDATE anime_work SET library_state='absent',verified_at='2000-01-01T00:00:00+00:00'")
                db.commit()
            result = torrent_mapper.reconcile_existing_paths(metadata, runtime, config)
            self.assertEqual(1, result["occupied"])
            with contextlib.closing(sqlite3.connect(runtime)) as db:
                self.assertEqual("existing", db.execute("SELECT library_state FROM anime_work").fetchone()[0])
                self.assertNotEqual("2000-01-01T00:00:00+00:00",
                                    db.execute("SELECT verified_at FROM anime_work").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
