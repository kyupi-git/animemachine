from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from animemachine.storage import path_policy


class PathPolicyTests(unittest.TestCase):
    def test_junction_detection_uses_non_following_metadata(self) -> None:
        with mock.patch.object(Path, "lstat", return_value=SimpleNamespace(st_reparse_tag=0xA0000003)), \
             mock.patch.object(path_policy.stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003, create=True):
            self.assertTrue(path_policy.is_junction("synthetic"))
        with mock.patch.object(Path, "lstat", return_value=SimpleNamespace(st_reparse_tag=0xA000000C)), \
             mock.patch.object(path_policy.stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003, create=True):
            self.assertFalse(path_policy.is_junction("synthetic"))
        with mock.patch.object(Path, "lstat", return_value=SimpleNamespace()):
            self.assertFalse(path_policy.is_junction("synthetic"))
        with mock.patch.object(Path, "lstat", side_effect=FileNotFoundError):
            self.assertFalse(path_policy.is_junction("missing"))
        with mock.patch.object(Path, "lstat", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                path_policy.is_junction("unreadable")

    def test_symlink_escape_is_rejected_and_internal_link_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "library"
            outside = base / "outside"
            root.mkdir(); outside.mkdir()
            internal = root / "episode.mkv"
            external = outside / "secret.mkv"
            internal.write_bytes(b"inside"); external.write_bytes(b"outside")
            inside_link = root / "inside-link.mkv"
            outside_link = root / "outside-link.mkv"
            try:
                inside_link.symlink_to(internal)
                outside_link.symlink_to(external)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable in this environment")
            self.assertEqual(path_policy.authorize_existing(inside_link, [root]), internal.resolve())
            with self.assertRaises(path_policy.PathAuthorizationError):
                path_policy.authorize_existing(outside_link, [root])

    def test_open_authorized_keeps_authorization_and_open_identity_together(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            media = root / "episode.mkv"
            media.write_bytes(b"payload")
            with path_policy.open_authorized(media, [root]) as (stream, current, opened):
                self.assertEqual(stream.read(), b"payload")
                self.assertEqual(current, media.resolve())
                now = os.stat(current)
                if opened.st_dev and opened.st_ino and now.st_dev and now.st_ino:
                    self.assertEqual((opened.st_dev, opened.st_ino), (now.st_dev, now.st_ino))

    def test_parent_escape_and_missing_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "library"
            root.mkdir()
            outside = base / "outside.mkv"
            outside.write_bytes(b"x")
            with self.assertRaises(path_policy.PathAuthorizationError):
                path_policy.authorize_existing(root / ".." / "outside.mkv", [root])
            with self.assertRaises(path_policy.PathAuthorizationError):
                path_policy.authorize_existing(root / "missing.mkv", [root])


if __name__ == "__main__":
    unittest.main()
