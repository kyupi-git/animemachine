import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


from animemachine.torrents import submit as MODULE


class SubmitPlanValidationTests(unittest.TestCase):
    def test_runtime_endpoint_overlay_is_applied_in_submission_worker(self):
        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            config_path.write_text(json.dumps(copy.deepcopy(MODULE.CONFIG)), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({
                "ANM_CONFIG_PATH": str(config_path),
                "ANM_MANAGED_QBITTORRENT_URL": "http://qbittorrent:8080",
                "ANM_QBT_LIBRARY_DIR": "/Library",
            })
            command = [
                sys.executable,
                "-c",
                "import json; from animemachine.torrents import submit; "
                "print(json.dumps([submit.CONFIG['components']['downloadClient']['endpoint'], submit.QBT_ROOT]))",
            ]
            result = subprocess.run(command, env=environment, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), ["http://qbittorrent:8080", "/Library"])

    def plan(self, torrent_path: str, operation: str, files: list[dict]):
        group = "VCB-Studio"
        return {
            "schemaVersion": "1.1",
            "category": MODULE.CATEGORY,
            "qbtEndpoint": MODULE.CONFIG["components"]["downloadClient"]["endpoint"],
            "qbtLibraryRoot": MODULE.QBT_ROOT,
            "jobs": [{
                "operation": operation,
                "infoHash": "a" * 40,
                "savePath": MODULE.QBT_ROOT + "/『2000_01』『Test』",
                "contentLayout": "NoSubfolder",
                "resourceGroup": group,
                "tags": sorted(MODULE.BASE_TAGS | {group}),
                "torrentPath": torrent_path,
                "files": files,
            }],
        }

    def test_extension_requires_delta_and_preserves_prior_selection(self):
        with tempfile.NamedTemporaryFile(suffix=".torrent") as torrent:
            files = [
                {"index": 0, "oldPath": "old.mkv", "newPath": "season/old.mkv", "length": 1, "selectedBefore": True, "selected": True},
                {"index": 1, "oldPath": "new.mkv", "newPath": "season/new.mkv", "length": 1, "selectedBefore": False, "selected": True},
            ]
            self.assertEqual(MODULE.validate(self.plan(torrent.name, "extend", files)), [])
            files[0]["selected"] = False
            self.assertTrue(any("cannot deselect" in error for error in MODULE.validate(self.plan(torrent.name, "extend", files))))

    def test_create_remains_backward_compatible(self):
        with tempfile.NamedTemporaryFile(suffix=".torrent") as torrent:
            files = [{"index": 0, "oldPath": "a.mkv", "newPath": "a.mkv", "length": 1, "selected": True}]
            plan = self.plan(torrent.name, "create", files)
            plan["schemaVersion"] = "1.0"
            self.assertEqual(MODULE.validate(plan), [])

    def test_no_subfolder_accepts_content_path_equal_to_save_path(self):
        job = {"savePath": MODULE.QBT_ROOT + "/『2000_01』『Test』", "files": [{}, {}]}
        current = {"root_path": "/transient/root", "content_path": job["savePath"]}
        self.assertTrue(MODULE.no_root_layout(current, job))
        current["content_path"] = job["savePath"] + "/unexpected"
        self.assertFalse(MODULE.no_root_layout(current, job))

    def test_live_preflight_requires_missing_target_to_remain_missing(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.NamedTemporaryFile(suffix=".torrent") as torrent:
            target = Path(raw) / "episode.mkv"
            files = [{"index": 0, "oldPath": "episode.mkv", "newPath": "episode.mkv", "length": 1,
                      "selectedBefore": False, "selected": True, "action": "add_missing", "finalPath": str(target)}]
            plan = self.plan(torrent.name, "create", files)
            self.assertEqual([], MODULE.validate_live_library(plan))
            target.write_bytes(b"x")
            self.assertTrue(any("regenerate" in error for error in MODULE.validate_live_library(plan)))

    def test_live_preflight_pins_replacement_source_shape(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.NamedTemporaryFile(suffix=".torrent") as torrent:
            target = Path(raw) / "episode.mkv"
            target.write_bytes(b"old")
            stat = target.stat()
            files = [{"index": 0, "oldPath": "episode.mkv", "newPath": ".anm-staging/p/episode.mkv", "length": 4,
                      "selectedBefore": False, "selected": True, "action": "stage_replace", "finalPath": str(target),
                      "localBytes": stat.st_size, "localMtimeNs": stat.st_mtime_ns}]
            plan = self.plan(torrent.name, "create", files)
            self.assertEqual([], MODULE.validate_live_library(plan))
            target.write_bytes(b"changed")
            self.assertTrue(any("regenerate" in error for error in MODULE.validate_live_library(plan)))

    def test_extend_execution_enables_only_new_indices(self):
        group = "VCB-Studio"
        job = {
            "operation": "extend", "infoHash": "a" * 40,
            "savePath": MODULE.QBT_ROOT + "/『2000_01』『Test』", "tags": sorted(MODULE.BASE_TAGS | {group}),
            "files": [
                {"index": 0, "oldPath": "old.mkv", "newPath": "season/old.mkv", "length": 1, "selectedBefore": True, "selected": True},
                {"index": 1, "oldPath": "new.mkv", "newPath": "season/new.mkv", "length": 2, "selectedBefore": False, "selected": True},
            ],
        }

        class FakeClient:
            def __init__(self):
                self.files = [
                    {"index": 0, "name": "season/old.mkv", "size": 1, "priority": 1},
                    {"index": 1, "name": "new.mkv", "size": 2, "priority": 0},
                ]
                self.job = {"hash": "a" * 40, "save_path": job["savePath"], "root_path": "", "category": MODULE.CATEGORY,
                            "tags": ",".join(job["tags"]), "auto_tmm": False, "state": "stoppedDL", "downloaded": 1,
                            "total_size": 3, "name": "Test", "content_path": job["savePath"], "download_path": ""}

            def get_json(self, route, fields=None):
                if route == "torrents/info":
                    return [self.job]
                if route == "torrents/files":
                    return [dict(item) for item in self.files]
                raise AssertionError(route)

            def post(self, route, fields=None):
                if route == "torrents/renameFile":
                    item = next(item for item in self.files if item["name"] == fields["oldPath"])
                    item["name"] = fields["newPath"]
                elif route == "torrents/filePrio":
                    for index in fields["id"].split("|"):
                        self.files[int(index)]["priority"] = int(fields["priority"])
                else:
                    raise AssertionError(route)
                return "Ok."

        client = FakeClient()
        result = MODULE.extend_one(client, job)
        self.assertIn("_extensionBefore", result)
        self.assertEqual([item["name"] for item in client.files], ["season/old.mkv", "season/new.mkv"])
        self.assertEqual([item["priority"] for item in client.files], [1, 1])

    def test_extend_reconciles_partial_commit_after_crash(self):
        group = "VCB-Studio"
        job = {
            "operation": "extend", "infoHash": "a" * 40,
            "savePath": MODULE.QBT_ROOT + "/『2000_01』『Test』", "tags": sorted(MODULE.BASE_TAGS | {group}),
            "files": [
                {"index": 0, "oldPath": "old.mkv", "newPath": "season/old.mkv", "length": 1, "selectedBefore": True, "selected": True},
                {"index": 1, "oldPath": "new.mkv", "newPath": "season/new.mkv", "length": 2, "selectedBefore": False, "selected": True},
            ],
        }

        class FakeClient:
            def __init__(self):
                self.files = [
                    {"index": 0, "name": "season/old.mkv", "size": 1, "priority": 1},
                    {"index": 1, "name": "season/new.mkv", "size": 2, "priority": 0},
                ]
                self.renames = 0
                self.job = {"hash": "a" * 40, "save_path": job["savePath"], "root_path": "", "category": MODULE.CATEGORY,
                            "tags": ",".join(job["tags"]), "auto_tmm": False, "state": "stoppedDL", "downloaded": 1,
                            "total_size": 3, "name": "Test", "content_path": job["savePath"], "download_path": ""}

            def get_json(self, route, fields=None):
                if route == "torrents/info": return [self.job]
                if route == "torrents/files": return [dict(item) for item in self.files]
                raise AssertionError(route)

            def post(self, route, fields=None):
                if route == "torrents/renameFile":
                    self.renames += 1
                    item = next(item for item in self.files if item["name"] == fields["oldPath"])
                    item["name"] = fields["newPath"]
                elif route == "torrents/filePrio":
                    for index in fields["id"].split("|"):
                        self.files[int(index)]["priority"] = int(fields["priority"])
                else: raise AssertionError(route)
                return "Ok."

        client = FakeClient()
        MODULE.extend_one(client, job)
        self.assertEqual(client.renames, 0)
        self.assertEqual([item["priority"] for item in client.files], [1, 1])

    def test_create_reconciles_qbt_accept_before_local_commit(self):
        group = "VCB-Studio"
        job = {
            "operation": "create", "_preExisting": True, "infoHash": "a" * 40,
            "savePath": MODULE.QBT_ROOT + "/『2000_01』『Test』", "tags": sorted(MODULE.BASE_TAGS | {group}),
            "files": [
                {"index": 0, "oldPath": "a.mkv", "newPath": "season/a.mkv", "length": 1, "selected": True},
                {"index": 1, "oldPath": "bonus.mkv", "newPath": "bonus.mkv", "length": 2, "selected": False},
            ],
        }

        class FakeClient:
            def __init__(self):
                self.files = [
                    {"index": 0, "name": "season/a.mkv", "size": 1, "priority": 1},
                    {"index": 1, "name": "bonus.mkv", "size": 2, "priority": 1},
                ]
                self.job = {"hash": "a" * 40, "save_path": job["savePath"], "root_path": "", "category": MODULE.CATEGORY,
                            "tags": ",".join(job["tags"]), "auto_tmm": False, "state": "stoppedDL", "downloaded": 0,
                            "total_size": 3, "name": "Test", "content_path": job["savePath"], "download_path": ""}

            def get_json(self, route, fields=None):
                if route == "torrents/info": return [self.job]
                if route == "torrents/files": return [dict(item) for item in self.files]
                raise AssertionError(route)

            def post(self, route, fields=None):
                if route != "torrents/filePrio": raise AssertionError(route)
                for index in fields["id"].split("|"):
                    self.files[int(index)]["priority"] = int(fields["priority"])
                return "Ok."

        client = FakeClient()
        MODULE.reconcile_created_job(client, job)
        self.assertEqual([item["priority"] for item in client.files], [1, 0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
