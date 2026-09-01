from __future__ import annotations

import hashlib
import unittest

from animemachine.torrents.metainfo import BencodeError, inspect_bytes


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
    raise TypeError(type(value))


def torrent(info):
    return bencode({b"info": info})


class TorrentMetainfoV2Test(unittest.TestCase):
    def test_v1_identity_uses_raw_info_bytes(self) -> None:
        info = {b"length": 123, b"name": b"episode.mkv", b"piece length": 16384, b"pieces": b"x" * 20}
        raw_info = bencode(info)
        record = inspect_bytes(torrent(info))
        self.assertEqual(hashlib.sha1(raw_info).hexdigest(), record["infoHashV1"])
        self.assertIsNone(record["infoHashV2"])
        self.assertFalse(record["hybrid"])

    def test_v2_identity_and_file_tree(self) -> None:
        info = {
            b"file tree": {b"movie.mkv": {b"": {b"length": 321, b"pieces root": b"r" * 32}}},
            b"meta version": 2,
            b"name": b"movie",
            b"piece length": 16384,
        }
        raw_info = bencode(info)
        record = inspect_bytes(torrent(info))
        self.assertIsNone(record["infoHashV1"])
        self.assertEqual(hashlib.sha256(raw_info).hexdigest(), record["infoHashV2"])
        self.assertEqual(1, record["manifestSummary"]["mainMediaCount"])

    def test_hybrid_records_both_identities(self) -> None:
        info = {
            b"file tree": {b"show.mkv": {b"": {b"length": 321, b"pieces root": b"r" * 32}}},
            b"length": 321,
            b"meta version": 2,
            b"name": b"show.mkv",
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
        raw_info = bencode(info)
        record = inspect_bytes(torrent(info))
        self.assertEqual(hashlib.sha1(raw_info).hexdigest(), record["infoHashV1"])
        self.assertEqual(hashlib.sha256(raw_info).hexdigest(), record["infoHashV2"])
        self.assertTrue(record["hybrid"])

    def test_strict_parser_rejects_trailing_and_invalid_root(self) -> None:
        info = {b"length": 1, b"name": b"x.mkv", b"piece length": 16384, b"pieces": b"x" * 20}
        with self.assertRaises(BencodeError):
            inspect_bytes(torrent(info) + b"junk")
        with self.assertRaises(BencodeError):
            inspect_bytes(b"d3:foo3:bare")

    def test_manifest_does_not_treat_extras_as_main_video(self) -> None:
        info = {
            b"files": [
                {b"length": 10, b"path": [b"NCOP.mkv"]},
                {b"length": 10, b"path": [b"sample.mkv"]},
                {b"length": 10, b"path": [b"Scans", b"booklet.jpg"]},
            ],
            b"name": b"extras",
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
        record = inspect_bytes(torrent(info))
        self.assertFalse(record["manifestSummary"]["hasMainMedia"])

    def test_ova_specials_are_separate_from_primary_main_media(self) -> None:
        info = {
            b"files": [
                {b"length": 10, b"path": [b"OVA", b"OVA 01.mkv"]},
                {b"length": 10, b"path": [b"Specials", b"SP01.mkv"]},
            ],
            b"name": b"specials",
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
        summary = inspect_bytes(torrent(info))["manifestSummary"]
        self.assertEqual(2, summary["specialMediaCount"])
        self.assertEqual(0, summary["primaryMainMediaCount"])
        self.assertTrue(summary["hasMainMedia"])
        self.assertFalse(summary["hasPrimaryMainMedia"])

    def test_strict_layout_rejects_invalid_piece_and_v2_root_structure(self) -> None:
        bad_v1 = {b"length": 32768, b"name": b"x.mkv", b"piece length": 16384, b"pieces": b"x" * 20}
        with self.assertRaises(BencodeError):
            inspect_bytes(torrent(bad_v1))
        bad_v2 = {
            b"file tree": {b"movie.mkv": {b"": {b"length": 321}}},
            b"meta version": 2,
            b"name": b"movie",
            b"piece length": 16384,
        }
        with self.assertRaises(BencodeError):
            inspect_bytes(torrent(bad_v2))

    def test_v2_piece_layers_and_leaf_shape_are_validated(self) -> None:
        pieces_root = b"r" * 32
        info = {
            b"file tree": {b"movie.mkv": {b"": {b"length": 32768, b"pieces root": pieces_root}}},
            b"meta version": 2,
            b"name": b"movie",
            b"piece length": 16384,
        }
        valid = bencode({b"info": info, b"piece layers": {pieces_root: b"x" * 64}})
        self.assertEqual(1, inspect_bytes(valid)["manifestSummary"]["mainMediaCount"])
        with self.assertRaises(BencodeError):
            inspect_bytes(torrent(info))

        bad_leaf = {
            b"file tree": {
                b"movie": {
                    b"": {b"length": 1, b"pieces root": b"a" * 32},
                    b"child.mkv": {b"": {b"length": 1, b"pieces root": b"b" * 32}},
                }
            },
            b"meta version": 2,
            b"name": b"movie",
            b"piece length": 16384,
        }
        with self.assertRaises(BencodeError):
            inspect_bytes(torrent(bad_leaf))

    def test_hybrid_padding_file_without_path_is_accepted(self) -> None:
        info = {
            b"file tree": {
                b"a.mkv": {b"": {b"length": 10000, b"pieces root": b"a" * 32}},
                b"b.mkv": {b"": {b"length": 10000, b"pieces root": b"b" * 32}},
            },
            b"files": [
                {b"length": 10000, b"path": [b"a.mkv"]},
                {b"attr": b"p", b"length": 6384},
                {b"length": 10000, b"path": [b"b.mkv"]},
            ],
            b"meta version": 2,
            b"name": b"show",
            b"piece length": 16384,
            b"pieces": b"x" * 40,
        }
        record = inspect_bytes(torrent(info))
        self.assertTrue(record["hybrid"])
        self.assertEqual(2, record["manifestSummary"]["primaryMainMediaCount"])



if __name__ == "__main__":
    unittest.main()
