import os
import ssl
import tempfile
import unittest
import contextlib
from pathlib import Path
from unittest import mock

from animemachine.network import health
from animemachine.network import tls as tls_support
from animemachine.network import transport


class TlsSupportTests(unittest.TestCase):
    def test_concurrent_same_origin_requests_use_bounded_independent_lanes(self):
        created=[]
        def factory(**kwargs):
            value=mock.Mock();value.context=kwargs['verify'];created.append(value);return value
        with mock.patch.object(transport,'_environment_proxies',return_value={}), \
                mock.patch.object(transport,'_system_proxies',return_value={}), \
                mock.patch.object(transport.httpx,'Client',side_effect=factory):
            transport.reset()
            with contextlib.ExitStack() as stack:
                clients=[stack.enter_context(transport._borrow_client('https://covers.invalid/image.jpg','direct',None))
                         for _ in range(24)]
                self.assertEqual(4,len(created))
                self.assertEqual(4,len({id(client.context) for client in clients}))
                self.assertTrue(all(transport._CLIENT_USERS[client]==6 for client in created))
            self.assertFalse(transport._CLIENT_USERS)
            with transport._borrow_client('https://covers.invalid/next.jpg','direct',None) as reused:
                self.assertIs(created[0],reused)
            self.assertEqual(4,len(created))
            transport.reset()

    def test_origins_and_proxy_routes_have_independent_verified_tls_contexts(self):
        with mock.patch.object(transport, '_environment_proxies', return_value={}), \
                mock.patch.object(transport, '_system_proxies', return_value={}), \
                mock.patch.object(transport.httpx, 'Client') as factory:
            transport.reset()
            first = transport.client('https://blocked.invalid/first')
            self.assertIs(first,transport.client('https://blocked.invalid/second'))
            transport.client('https://healthy.invalid/image.jpg')
            transport._client_for('http://localhost:7890',0,'https://blocked.invalid')
            contexts = [call.kwargs['verify'] for call in factory.call_args_list]
            self.assertEqual(3,len(contexts))
            self.assertEqual(3,len({id(context) for context in contexts}))
            self.assertTrue(all(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED for context in contexts))
            transport.reset()

    def test_client_pool_prunes_idle_origins_without_closing_active_streams(self):
        created = []
        def factory(**_kwargs):
            value=mock.Mock();created.append(value);return value
        with mock.patch.object(transport,'_MAX_IDLE_CLIENTS',2), \
                mock.patch.object(transport,'_environment_proxies',return_value={}), \
                mock.patch.object(transport,'_system_proxies',return_value={}), \
                mock.patch.object(transport.httpx,'Client',side_effect=factory):
            transport.reset()
            with transport._borrow_client('https://active.invalid','direct',None) as active:
                with transport._borrow_client('https://idle.invalid','direct',None):
                    pass
                with transport._borrow_client('https://new.invalid','direct',None):
                    pass
                active.close.assert_not_called()
                created[1].close.assert_called_once()
                self.assertEqual(2,len(transport._CLIENTS))
            self.assertFalse(transport._CLIENT_USERS)
            transport.reset()

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

