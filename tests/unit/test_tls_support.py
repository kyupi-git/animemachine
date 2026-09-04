import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animemachine.network import health
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
             mock.patch.object(transport, "_environment_proxies", side_effect=[{"https": "http://127.0.0.1:8080"}, {"https": "http://127.0.0.1:8081"}]), \
             mock.patch.object(transport, "_system_proxies", return_value={}), \
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

    def test_transport_switches_from_direct_to_proxy_without_restart(self):
        direct, proxied = mock.Mock(), mock.Mock()
        with mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
             mock.patch.object(transport, "_environment_proxies", side_effect=[{}, {"https": "http://127.0.0.1:8080"}]), \
             mock.patch.object(transport, "_system_proxies", return_value={}), \
             mock.patch.object(transport.httpx, "Client", side_effect=[direct, proxied]) as factory:
            transport.reset()
            self.assertIs(direct, transport.client("https://example.invalid/before"))
            self.assertIs(proxied, transport.client("https://example.invalid/after"))
        self.assertIsNone(factory.call_args_list[0].kwargs["proxy"])
        self.assertEqual("http://127.0.0.1:8080", factory.call_args_list[1].kwargs["proxy"])
        transport.reset()

    def test_transport_reuses_route_clients_across_proxy_revisions(self):
        direct, proxied = mock.Mock(), mock.Mock()
        with mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
             mock.patch.object(transport, "_environment_proxies", side_effect=[{}, {"https": "http://127.0.0.1:8080"}, {}, {"https": "http://127.0.0.1:8080"}]), \
             mock.patch.object(transport, "_system_proxies", return_value={}), \
             mock.patch.object(transport.httpx, "Client", side_effect=[direct, proxied]) as factory:
            transport.reset()
            self.assertIs(direct, transport.client("https://example.invalid/1"))
            self.assertIs(proxied, transport.client("https://example.invalid/2"))
            self.assertIs(direct, transport.client("https://example.invalid/3"))
            self.assertIs(proxied, transport.client("https://example.invalid/4"))
        self.assertEqual(2, factory.call_count)
        self.assertEqual(2, len(transport._CLIENTS))
        transport.reset()


    def test_windows_system_proxy_is_not_hidden_by_no_proxy_environment(self):
        with mock.patch.object(transport.os, "name", "nt"), \
             mock.patch.object(transport.urllib.request, "getproxies_environment", return_value={"no": "localhost,127.0.0.1"}), \
             mock.patch.object(transport.urllib.request, "getproxies_registry", return_value={"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}, create=True):
            self.assertEqual({
                "http": "http://127.0.0.1:7890",
                "https": "http://127.0.0.1:7890",
                "no": "localhost,127.0.0.1",
            }, transport._proxy_settings())

    def test_windows_explicit_environment_proxy_overrides_registry(self):
        with mock.patch.object(transport.os, "name", "nt"), \
             mock.patch.object(transport.urllib.request, "getproxies_environment", return_value={"https": "http://127.0.0.1:7891"}), \
             mock.patch.object(transport.urllib.request, "getproxies_registry", return_value={"https": "http://127.0.0.1:7890"}, create=True):
            self.assertEqual("http://127.0.0.1:7891", transport._proxy_settings()["https"])

    def test_network_health_keeps_direct_and_system_proxy_learning_separate(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "health.sqlite3")
            store.success("mirror", "binary", .2, 1024, route_mode="direct")
            store.failure("mirror", "binary", "ConnectError", route_mode="windows_system_proxy")
            self.assertEqual(1, store.snapshot("mirror", "binary", "direct")["samples"])
            self.assertEqual(1, store.snapshot("mirror", "binary", "windows_system_proxy")["samples"])
            self.assertNotEqual(
                store.snapshot("mirror", "binary", "direct")["recentSuccessRate"],
                store.snapshot("mirror", "binary", "windows_system_proxy")["recentSuccessRate"],
            )

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

