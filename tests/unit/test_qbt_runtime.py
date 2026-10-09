import unittest
from unittest import mock
from pathlib import Path

from animemachine.integrations import qbt_runtime


class QbtRuntimeTests(unittest.TestCase):
    def test_duplicate_torrent_identity_fails_before_database_changes(self):
        row = {"hash": "a"*40, "state": "stoppedDL"}
        with mock.patch.object(qbt_runtime, "fetch", return_value=[row, {**row, "state": "uploading"}]), \
                mock.patch.object(qbt_runtime.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "duplicate torrent identities"):
                qbt_runtime.refresh(Path("unused.sqlite3"), "http://localhost", "AnimeMachine")
        connect.assert_not_called()

    def test_lifecycle(self):
        self.assertEqual(qbt_runtime.lifecycle({"state":"stoppedDL","progress":0}), "queued")
        self.assertEqual(qbt_runtime.lifecycle({"state":"downloading","progress":0,"downloaded":0}), "downloading")
        self.assertEqual(qbt_runtime.lifecycle({"state":"pausedDL","progress":0,"downloaded":0}), "queued")
        self.assertEqual(qbt_runtime.lifecycle({"state":"pausedDL","progress":0.4,"downloaded":400}), "downloading")
        self.assertEqual(qbt_runtime.lifecycle({"state":"stoppedUP","progress":1}), "existing")


if __name__ == "__main__":
    unittest.main()
