import os
import ssl
import unittest
from unittest import mock

from animemachine.network import tls as tls_support
from animemachine.network import transport


class TlsSupportTests(unittest.TestCase):
    def tearDown(self):
        tls_support.ssl_context.cache_clear()

    def test_https_uses_verified_context_and_http_does_not(self):
        with mock.patch.object(transport, "open_url", return_value="ok") as opened:
            self.assertEqual("ok", tls_support.urlopen("https://example.invalid", timeout=2))
        with mock.patch.object(transport, "open_url", return_value="ok") as opened:
            tls_support.urlopen("http://127.0.0.1", timeout=2)
            self.assertEqual(2, opened.call_args.kwargs["timeout"])

    def test_transport_rebuilds_client_when_proxy_changes(self):
        first, second = mock.Mock(), mock.Mock()
        with mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
             mock.patch.object(transport.urllib.request, "getproxies", side_effect=[{"https": "http://127.0.0.1:8080"}, {"https": "http://127.0.0.1:8081"}]), \
             mock.patch.object(transport.httpx, "Client", side_effect=[first, second]) as factory:
            transport.reset()
            self.assertIs(first, transport.client("https://example.invalid/a"))
            self.assertIs(second, transport.client("https://example.invalid/b"))
        self.assertEqual("http://127.0.0.1:8080", factory.call_args_list[0].kwargs["proxy"] )
        self.assertEqual("http://127.0.0.1:8081", factory.call_args_list[1].kwargs["proxy"] )
        first.close.assert_not_called()
        transport.reset()
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_transport_reset_reloads_ca_context(self):
        tls_support.ssl_context()
        self.assertGreater(tls_support.ssl_context.cache_info().currsize, 0)
        transport.reset()
        self.assertEqual(0, tls_support.ssl_context.cache_info().currsize)

    def test_transport_keeps_private_endpoints_direct(self):
        self.assertIsNone(transport._proxy_for_url("http://192.168.1.20:7789/api/about", {"http": "http://proxy.invalid:8080"}))

    def test_missing_custom_ca_fails_closed(self):
        with mock.patch.dict(os.environ, {"ANM_CA_BUNDLE": "missing-anm-ca.pem"}, clear=False):
            with self.assertRaises(RuntimeError):
                tls_support.ssl_context()


if __name__ == "__main__":
    unittest.main()

