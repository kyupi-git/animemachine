from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from animemachine.integrations import managed_network as network


class FakeClient:
    endpoint = "http://ani-rss:7789"
    key = "private-unit-test-key"

    def __init__(self):
        self.config = dict(network._DEFAULTS)
        self.items = [{"id": "one", "url": "https://mikanani.me/RSS/Bangumi?bangumiId=123&subgroupid=9",
                       "title": "Example", "enable": False, "currentEpisodeNumber": 3,
                       "standbyRssList": None}]
        self.calls = []

    def call(self, path, *, body=None, **kwargs):
        self.calls.append((path, copy.deepcopy(body)))
        if path == "config":
            return copy.deepcopy(self.config)
        if path == "setConfig":
            self.config.update(body)
        if path == "setAni":
            self.items = [copy.deepcopy(body) if row["id"] == body["id"] else row for row in self.items]

    def subscriptions(self):
        return copy.deepcopy(self.items)


class ManagedNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        patch = mock.patch.dict(os.environ, {"ANM_STATE_DIR": self.temporary.name, "ANM_MANAGED_ANI_RSS": "true"})
        patch.start()
        self.addCleanup(patch.stop)
        network._STATUS.clear()
        network._RECHECK.clear()
        health_patch = mock.patch.object(network.sources, '_HEALTH', None)
        health_patch.start()
        self.addCleanup(health_patch.stop)
        self.client = FakeClient()

    def expire(self):
        state = network._read()
        state["nextCheck"] = 0
        network._save(network._root() / "ani-rss-network.json", state)

    @staticmethod
    def mirror_only(endpoint, *_):
        return endpoint.base_url in {"https://mikan.sakiko.de", "https://bgmapi.anibt.net"}

    def test_external_instance_is_never_read_or_modified(self):
        self.client.endpoint = "http://192.0.2.20:7789"
        with mock.patch.object(network, "probe", side_effect=AssertionError("external instance")):
            self.assertEqual("unmanaged", network.tick(self.client)["state"])
        self.assertEqual([], self.client.calls)

    def test_fallback_keeps_subscription_state_and_restart_does_not_write(self):
        with mock.patch.object(network, "probe", side_effect=self.mirror_only):
            result = network.tick(self.client)
            self.assertEqual("ready", result["state"])
            self.assertTrue(result["changed"])
            self.assertEqual("https://mikan.sakiko.de", self.client.config["mikanHost"])
            self.assertTrue(self.client.config["bgmApi"].startswith("http://animemachine:8787/api/managed-bangumi/"))
            self.assertFalse(self.client.items[0]["enable"])
            self.assertEqual(3, self.client.items[0]["currentEpisodeNumber"])
            self.assertIsNone(self.client.items[0]["standbyRssList"])
            self.assertIn("subgroupid=9", self.client.items[0]["url"])
            self.client.calls.clear()
            network._STATUS.clear()
            again = network.tick(self.client)
            self.assertEqual("ready", again["state"])
            self.assertFalse(again["changed"])
            self.assertEqual([], self.client.calls)
        state_text = (network._root() / "ani-rss-network.json").read_text(encoding="utf-8")
        self.assertNotIn(self.client.key, state_text)

    def test_manual_configuration_is_preserved_and_not_probed(self):
        self.client.config.update(mikanHost="https://user.example", bgmApi="https://private.example", proxyPassword="secret")
        with mock.patch.object(network, "probe", side_effect=AssertionError("manual source")):
            result = network.tick(self.client)
        self.assertEqual(["mikanHost", "bgmApi"], result["manual"])
        self.assertFalse(any(name == "setConfig" for name, _ in self.client.calls))

    def test_legacy_identity_upgrade_preserves_managed_routes_and_user_changes(self):
        with mock.patch.object(network, 'probe', side_effect=self.mirror_only):
            network.tick(self.client)
            state = network._read()
            state.pop('identityVersion')
            state['identity'] = 'a' * 64
            network._save(network._root() / 'ani-rss-network.json', state)
            self.client.config['bgmApi'] = 'https://user.example'
            self.client.calls.clear()
            result = network.tick(self.client)
        self.assertEqual(['bgmApi'], result['manual'])
        self.assertEqual('https://mikan.sakiko.de', self.client.config['mikanHost'])
        self.assertEqual('https://user.example', self.client.config['bgmApi'])
        saved = network._read()
        self.assertEqual(2, saved['identityVersion'])
        self.assertEqual(network._client_identity(self.client.endpoint, self.client.key), saved['identity'])
        self.assertFalse(any(name == 'setConfig' for name, _ in self.client.calls))

    def test_partial_failure_after_config_patch_is_resumed(self):
        with mock.patch.object(network, "probe", side_effect=self.mirror_only), \
                mock.patch.object(network, "_migrate_feeds", side_effect=RuntimeError("temporarily unavailable")):
            self.assertEqual("retrying", network.tick(self.client)["state"])
        self.expire()
        with mock.patch.object(network, "probe", side_effect=self.mirror_only):
            self.assertEqual("ready", network.tick(self.client)["state"])
        self.assertTrue(self.client.items[0]["url"].startswith("https://mikan.sakiko.de/"))

    def test_two_healthy_passes_restore_official_source(self):
        with mock.patch.object(network, "probe", side_effect=self.mirror_only):
            network.tick(self.client)
        with mock.patch.object(network, "probe", return_value=True):
            self.expire()
            network.tick(self.client)
            self.assertEqual("https://mikan.sakiko.de", self.client.config["mikanHost"])
            self.expire()
            network.tick(self.client)
        self.assertEqual(network._DEFAULTS, self.client.config)
        self.assertTrue(self.client.items[0]["url"].startswith("https://mikanani.me/"))

    def test_total_outage_keeps_last_good_settings_and_retries_soon(self):
        with mock.patch.object(network, "probe", side_effect=self.mirror_only):
            network.tick(self.client)
        previous = dict(self.client.config)
        self.expire()
        with mock.patch.object(network, "probe", return_value=False):
            self.assertEqual("retrying", network.tick(self.client)["state"])
        self.assertEqual(previous, self.client.config)
        self.assertLess(network._read()["nextCheck"] - network.time.time(), 61)

    def test_actual_search_timeout_rechecks_and_avoids_the_recently_failing_source(self):
        with mock.patch.object(network, 'probe', return_value=True):
            network.tick(self.client)
        self.assertEqual('https://mikanani.me', self.client.config['mikanHost'])
        network.request_failed(self.client.endpoint, self.client.key)
        network._RECHECK.clear()  # Persistent health also recovers after a restart.
        with mock.patch.object(network, 'probe', return_value=True):
            result = network.tick(self.client)
        self.assertTrue(result['changed'])
        self.assertEqual('https://mikanime.tv', self.client.config['mikanHost'])
        self.assertFalse(self.client.items[0]['enable'])

    def test_external_or_manual_sources_never_record_managed_request_failures(self):
        with mock.patch.object(network, 'probe', return_value=True):
            network.tick(self.client)
        self.client.config['mikanHost'] = 'https://private.example'
        self.expire()
        with mock.patch.object(network, 'probe', return_value=True):
            network.tick(self.client)
        with mock.patch.object(network.sources, '_health', side_effect=AssertionError('manual source')):
            network.request_failed(self.client.endpoint, self.client.key)
            network.request_failed('http://external.example:7789', self.client.key)

    def test_only_mikan_outbound_errors_request_a_route_recheck(self):
        from animemachine.integrations import ani_rss
        client = ani_rss.Client(self.client.endpoint, self.client.key)
        for path, message, expected in [('mikan', 'SocketTimeoutException: Read timed out', True),
                                         ('mikanGroup', 'UnknownHostException', True),
                                         ('mikan', 'Invalid year', False),
                                         ('config', 'SocketTimeoutException', False)]:
            with self.subTest(path=path, message=message), \
                 mock.patch.object(ani_rss.tls_support, 'urlopen') as opened, \
                 mock.patch.object(network, 'request_failed') as failed:
                response = opened.return_value.__enter__.return_value
                response.status = 200
                response.read.return_value = json.dumps({'code': 500, 'message': message}).encode()
                with self.assertRaises(RuntimeError):
                    client.call(path)
                self.assertEqual(int(expected), failed.call_count)

    def test_user_token_stays_on_official_api_and_gateway_strips_credentials(self):
        self.client.config["bgmToken"] = "private-token"
        with mock.patch.object(network, "probe", side_effect=self.mirror_only):
            network.tick(self.client)
        self.assertEqual("https://api.bgm.tv", self.client.config["bgmApi"])
        token = network._metadata_url().rsplit("/", 1)[1]
        with mock.patch.object(network.sources, "fetch_json", return_value=({"id": 123, "name": "Test"}, "unit")) as fetch:
            value = network.metadata_request(token, "/v0/subjects/123", "")
            self.assertEqual(123, value["id"])
            headers = fetch.call_args.kwargs["headers"]
            self.assertEqual({"User-Agent"}, set(headers))
        with self.assertRaises(PermissionError):
            network.metadata_request("bad", "/v0/subjects/123", "")
        for path, query in (("/v0/me", ""), ("/v0/subjects/123", "token=bad"), ("/v0/subjects/123", "offset=-1")):
            with self.assertRaises(ValueError):
                network.metadata_request(token, path, query)

    def test_cancelled_work_and_concurrent_user_edits_do_not_patch(self):
        with mock.patch.object(network, "probe", side_effect=self.mirror_only):
            network.tick(self.client, cancelled=lambda: True)
        self.assertFalse(any(name == "setConfig" for name, _ in self.client.calls))

    def test_only_trusted_public_feeds_can_be_rewritten(self):
        for url in ("https://user:password@mikanani.me/RSS/Bangumi?bangumiId=1",
                    "https://mikanani.me:bad/RSS/Bangumi?bangumiId=1",
                    "https://mikanani.me/RSS/MyBangumi?token=secret",
                    "https://mikanani.me/RSS/Bangumi?bangumiId=1&token=secret",
                    "https://evil.example/RSS/Bangumi?bangumiId=1"):
            self.assertIsNone(network._public_feed(url, "https://mikan.sakiko.de"))

    def test_migration_is_bounded_and_retryable(self):
        self.client.items = [dict(self.client.items[0], id=str(i)) for i in range(12)]
        count, complete = network._migrate_feeds(self.client, "https://mikan.sakiko.de")
        self.assertEqual((10, False), (count, complete))
        self.assertEqual((2, True), network._migrate_feeds(self.client, "https://mikan.sakiko.de"))

    def test_proxy_matching_does_not_match_lookalike_hosts(self):
        config = {"proxy": True, "proxyList": "mikanani.me", "proxyHost": "proxy", "proxyPort": 8080,
                  "proxyUsername": "name@", "proxyPassword": "p:a"}
        self.assertEqual("http://name%40:p%3Aa@proxy:8080", network._proxy(config, "https://mikanani.me/Home"))
        self.assertEqual("", network._proxy(config, "https://fake-mikanani.me/Home"))

    def test_source_probe_validates_html_rss_and_torrent_not_just_http_status(self):
        endpoint = next(row for row in network.registry.for_service("mikan") if row.id == "mikan-sakiko")
        raw = b"d4:infod6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:" + b"0" * 20 + b"ee"
        payloads = [b'&#34588;&#26577; <a href="/Home/Bangumi/1">Huge ongoing feed</a> <a href="/Home/Bangumi/123">Example</a>',
                    b'<rss><channel><item><enclosure url="https://mikan.sakiko.de/Download/test.torrent"/></item></channel></rss>', raw]
        responses = [httpx.Response(200, content=body, request=httpx.Request("GET", endpoint.base_url + "/Home")) for body in payloads]
        with mock.patch.object(network.transport, "request", side_effect=responses) as request, \
                mock.patch.object(network.sources, "_health") as health:
            self.assertTrue(network.probe(endpoint, {}, 123))
            self.assertEqual(3, request.call_count)
            self.assertIn('bangumiId=123', request.call_args_list[1].args[1])
            self.assertEqual("", request.call_args.kwargs["proxy_url"])
            health.return_value.success.assert_called_once()
        with mock.patch.object(network.transport, 'request', side_effect=[httpx.ReadTimeout('slow handshake'), *responses]) as request, \
                mock.patch.object(network.sources, '_health'):
            self.assertTrue(network.probe(endpoint, {}, 123))
            self.assertEqual(4, request.call_count)
            self.assertGreaterEqual(request.call_args.kwargs['timeout'], 7)
            self.assertTrue(all(call.kwargs['deadline'] == request.call_args.kwargs['deadline']
                                for call in request.call_args_list))
        with mock.patch.object(network.transport, 'request', side_effect=httpx.ReadTimeout('unreachable')) as request, \
                mock.patch.object(network.sources, '_health'):
            self.assertFalse(network.probe(endpoint, {}, 123))
            self.assertEqual(2, request.call_count)
        with mock.patch.object(network.transport, "request", return_value=responses[0]), \
                mock.patch.object(network.sources, "_health"):
            self.assertFalse(network.probe(endpoint, {}, 123))

    def test_explicit_probe_routes_do_not_change_application_proxy_ranking(self):
        transport = network.transport
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))) as client, \
                mock.patch.object(transport, "_client_for", return_value=client) as factory, \
                mock.patch.object(transport, "_remember_route") as remember, \
                mock.patch.object(transport.connectivity, "is_offline", return_value=False):
            response = transport.request("GET", "https://probe.invalid/", proxy_url="")
            self.assertEqual(b"ok", response.content)
            self.assertIsNone(factory.call_args.args[0])
            remember.assert_not_called()


class ManagedInitializationTests(unittest.TestCase):
    def test_restart_and_upgrade_preserve_live_configuration(self):
        from animemachine import cli
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = {"ANM_MANAGED_QBT": "true", "ANM_MANAGED_ANI_RSS": "true",
                   "ANM_QBT_CONFIG_DIR": str(root / "qbt"), "ANM_ANI_RSS_CONFIG_DIR": str(root / "ani"),
                   "ANM_QBT_API_KEY_FILE": str(root / "qbt/.animemachine/api-key"),
                   "ANM_ANI_RSS_API_KEY_FILE": str(root / "ani/.animemachine/api-key"),
                   "ANM_QBT_API_KEY": "", "ANM_ANI_RSS_API_KEY": "", "ANM_QBT_WEB_PASSWORD": ""}
            with mock.patch.dict(os.environ, env):
                cli.initialize_managed_services()
                config = root / "ani/config.v2.json"
                value = json.loads(config.read_text(encoding="utf-8"))
                value["mikanHost"] = "https://custom.example"
                config.write_text(json.dumps(value), encoding="utf-8")
                snapshot = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
                cli.initialize_managed_services()
                self.assertEqual(snapshot, {path: path.read_bytes() for path in snapshot})
                (root / "qbt/.animemachine/initialized.json").unlink()
                with mock.patch.object(cli, "bootstrap_qbittorrent", side_effect=AssertionError("upgrade must not rewrite")):
                    cli.initialize_managed_services()
                (root / "ani/.animemachine/api-key").unlink()
                errors = cli.initialize_managed_services()
                self.assertIn("credentials are missing", errors[0])
                self.assertEqual(snapshot[config], config.read_bytes())
