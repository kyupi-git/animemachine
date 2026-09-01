import unittest
from pathlib import Path
from unittest import mock

from animemachine.storage import status


class StorageStatusTests(unittest.TestCase):
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
