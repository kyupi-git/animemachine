import unittest
import tempfile
from pathlib import Path
from unittest import mock

from animemachine.storage import status


class StorageStatusTests(unittest.TestCase):
    def test_permission_checks_never_modify_the_library(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / '媒体 library'
            path.mkdir()
            original = status.subprocess.run
            guard = f'''
import sys, os
root = {str(path)!r}
def audit(event, args):
    if args and str(args[0]).startswith(root):
        if event in {{'os.remove', 'os.mkdir', 'os.rename', 'os.rmdir'}} or (event == 'open' and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
            raise RuntimeError('storage check attempted a filesystem mutation')
sys.addaudithook(audit)
'''
            def guarded(args, **kwargs):
                args = list(args)
                args[2] = guard + args[2]
                return original(args, **kwargs)
            with mock.patch.object(status.subprocess, 'run', side_effect=guarded):
                for require_write in (False, True):
                    self.assertEqual(status.AVAILABLE, status.probe_path(path, require_write=require_write)[0])
            self.assertEqual([], list(path.iterdir()))

    def test_known_write_denial_stops_the_operation_without_creating_a_probe(self):
        with tempfile.TemporaryDirectory() as raw:
            original = status.subprocess.run
            def denied(args, **kwargs):
                args = list(args)
                args[2] = 'import os; os.access = lambda *args: False\n' + args[2]
                return original(args, **kwargs)
            with mock.patch.object(status.subprocess, 'run', side_effect=denied):
                self.assertEqual(status.PERMISSION_DENIED, status.probe_path(Path(raw), require_write=True)[0])

    def test_cold_network_timeout_retries_once(self):
        profile = status.StorageProfile("library", "Library", Path(r"\\server\anime"), "rw", False, "network")
        with mock.patch.object(status, "probe_path", side_effect=[
                (status.HOST_UNREACHABLE, "probe timeout"), (status.AVAILABLE, "")]) as probe:
            result = status.status_for_profile(profile, timeout=4, use_cache=False)
        self.assertEqual(result.state, status.AVAILABLE)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args_list[1].kwargs["timeout"], 8.0)

    def test_local_timeout_is_not_retried(self):
        profile = status.StorageProfile("library", "Library", Path("C:/anime"), "rw", False, "local-path")
        with mock.patch.object(status, "probe_path", return_value=(status.HOST_UNREACHABLE, "probe timeout")) as probe:
            result = status.status_for_profile(profile, timeout=4, use_cache=False)
        self.assertEqual(result.state, status.HOST_UNREACHABLE)
        probe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
