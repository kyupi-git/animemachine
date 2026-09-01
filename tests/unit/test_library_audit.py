import unittest
import tempfile
from pathlib import Path

from animemachine.library import audit as library_audit


class LibraryAuditTests(unittest.TestCase):
    def test_distribution_similarity_uses_kind_episode_and_size(self):
        expected = [library_audit._signature("Episode 01.mkv", 1000, "main_video"),
                    library_audit._signature("Scans/a.png", 100, "scans")]
        same = [library_audit._signature("Show - 01.mkv", 990, "main_video"),
                library_audit._signature("Booklet/a.png", 99, "scans")]
        partial = [library_audit._signature("Show - 01.mkv", 990, "main_video")]
        self.assertGreaterEqual(library_audit.distribution_similarity(expected, same), 95)
        self.assertGreater(library_audit.distribution_similarity(expected, partial), 80)

    def test_threshold_states(self):
        self.assertEqual(library_audit._state(95), "complete")
        self.assertEqual(library_audit._state(80), "near_complete")
        self.assertEqual(library_audit._state(60), "partial_high")
        self.assertEqual(library_audit._state(30), "partial")
        self.assertEqual(library_audit._state(29.9), "incomplete")

    def test_observed_reuses_physical_root_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / "Episode 01.mkv"
            media.write_bytes(b"first")
            cache = {}
            first = library_audit._observed([root], cache)
            media.unlink()
            second = library_audit._observed([root], cache)
        self.assertEqual(first, second)
        self.assertEqual(1, len(second))


if __name__ == "__main__":
    unittest.main()

