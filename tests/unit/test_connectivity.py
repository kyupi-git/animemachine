import os
import unittest
from unittest import mock

from animemachine.integrations import connectivity


class ConnectivityTests(unittest.TestCase):
    def test_qbittorrent_probe_does_not_expose_key(self):
        with mock.patch.dict(os.environ, {"ANM_QBT_API_KEY": "secret"}, clear=False), \
             mock.patch.object(connectivity, "_get", return_value=(200, "5.1.0")) as call:
            result = connectivity.probe("qbittorrent", "http://127.0.0.1:8080")
        self.assertTrue(result["authenticated"])
        self.assertEqual(call.call_args.args[1]["Authorization"], "Bearer secret")
        self.assertEqual(call.call_args.args[1]["X-API-Key"], "secret")
        self.assertNotIn("secret", str(result))

    def test_core_connector_rejects_indexer_credentials(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            connectivity.probe("indexer", "http://127.0.0.1:9696")


if __name__ == "__main__":
    unittest.main()

