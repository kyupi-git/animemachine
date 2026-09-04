from __future__ import annotations

import os
import unittest

from animemachine.torrents import scanner


class TorrentScannerPathTests(unittest.TestCase):
    def test_source_identity_preserves_case_on_case_sensitive_platforms(self) -> None:
        upper = scanner._source_identity(os.path.join(os.sep, "Pool", "A.torrent"))
        lower = scanner._source_identity(os.path.join(os.sep, "Pool", "a.torrent"))
        if os.path.normcase("A") == os.path.normcase("a"):
            self.assertEqual(upper, lower)
        else:
            self.assertNotEqual(upper, lower)

    def test_repair_primary_source_path_uses_remaining_duplicate(self) -> None:
        db = scanner.connect(":memory:")
        try:
            scanner.schema(db)
            scanner.migrate(db)
            db.execute(
                """INSERT INTO torrent(
                       info_hash,torrent_path,manifest_sha256,scan_state,asset_kind,metadata_state,indexed_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                ("a" * 40, "/pool/deleted.torrent", "m", "accept", "torrent", "available", "old"),
            )
            db.executemany(
                """INSERT INTO torrent_source(
                       source_path,size,mtime_ns,info_hash,presence_state,last_seen_at,parse_error
                   ) VALUES(?,?,?,?,?,?,NULL)""",
                [
                    ("/pool/deleted.torrent", 100, 1, "a" * 40, "missing", "old"),
                    ("/pool/remaining.torrent", 100, 1, "a" * 40, "present", "old"),
                ],
            )

            scanner._repair_primary_source_paths(db, "new")

            row = db.execute(
                "SELECT torrent_path,metadata_state,indexed_at FROM torrent WHERE info_hash=?",
                ("a" * 40,),
            ).fetchone()
            self.assertEqual(row["torrent_path"], "/pool/remaining.torrent")
            self.assertEqual(row["metadata_state"], "available")
            self.assertEqual(row["indexed_at"], "new")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
