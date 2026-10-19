import os
import unittest
from unittest import mock

import httpx

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

    def test_transient_qbittorrent_key_does_not_modify_environment(self):
        with mock.patch.dict(os.environ, {"ANM_QBT_API_KEY": "saved-key"}, clear=False), \
             mock.patch.object(connectivity, "_get", return_value=(200, "5.2.0")) as call:
            result = connectivity.probe("qbittorrent", "http://127.0.0.1:8080", "typed-key")
            self.assertEqual("saved-key", os.environ["ANM_QBT_API_KEY"])
        self.assertTrue(result["authenticated"])
        self.assertEqual("Bearer typed-key", call.call_args.args[1]["Authorization"])
        self.assertNotIn("typed-key", str(result))

    def test_core_connector_rejects_indexer_credentials(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            connectivity.probe("indexer", "http://127.0.0.1:9696")

    def test_new_endpoint_does_not_receive_saved_credentials(self):
        with mock.patch.dict(os.environ, {"ANM_QBT_API_KEY": "saved-key"}), \
                mock.patch.object(connectivity, "_get", return_value=(403, "Forbidden")) as call:
            result = connectivity.probe("qbittorrent", "http://new-nas:8080", use_saved_key=False)
        self.assertFalse(result["authenticated"])
        self.assertEqual({}, call.call_args.args[1])

    def test_local_docker_and_nas_endpoints_remain_supported(self):
        from animemachine.network.validators import service_endpoint
        for endpoint in ("http://127.0.0.1:8080", "http://localhost:8080", "http://qbittorrent:8080",
                         "http://192.168.11.30:7789", "http://[::1]:8080", "https://nas.example/qbt"):
            self.assertEqual(endpoint, service_endpoint(endpoint))
        for endpoint in ("file:///etc/passwd", "http://user:pass@nas:8080", "http://nas:0",
                         "http://nas:bad", "http://nas?token=secret", "http://nas/#section",
                         "http://169.254.169.254", "http://[fe80::1]", "http://0.0.0.0",
                         "http://metadata.google.internal", "http://nas/../api", "http://nas\\@evil"):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "invalid_service_endpoint"):
                service_endpoint(endpoint)

    def test_authenticated_probe_does_not_follow_redirects(self):
        from animemachine.network import transport
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"Location": "http://other.invalid/steal"})
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client, \
                mock.patch.object(transport, "client", return_value=client), \
                mock.patch.object(transport.connectivity, "is_offline", return_value=False):
            result = connectivity.probe("qbittorrent", "http://nas:8080", "typed-key")
        self.assertEqual(1, len(seen))
        self.assertEqual("nas", seen[0].url.host)
        self.assertFalse(result["authenticated"])
        self.assertEqual("service_redirect", result["message"])

    def test_ani_rss_uses_same_redirect_boundary(self):
        from animemachine.integrations import ani_rss
        from animemachine.network import transport
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(307, headers={"Location": "http://other.invalid/api/about"})
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client, \
                mock.patch.object(transport, "client", return_value=client), \
                mock.patch.object(transport.connectivity, "is_offline", return_value=False):
            result = ani_rss.probe({"components": {"aniRss": {"endpoint": "http://ani-rss:7789"}}}, "typed-key")
        self.assertEqual(1, len(seen))
        self.assertEqual("service_redirect", result["message"])


if __name__ == "__main__":
    unittest.main()

