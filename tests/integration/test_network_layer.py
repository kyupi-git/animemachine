from __future__ import annotations

import contextlib
import json
import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

from animemachine.network import connectivity, diagnostics, health, hedging, registry, sources, transport, validators


class FakeResponse:
    def __init__(self, data=b"{}", url="https://mirror.invalid"):
        self.content=data; self.headers={"content-type":"application/json"}; self.url=url


class NetworkLayerTests(unittest.TestCase):
    def test_transport_requests_identity_encoding_by_default(self):
        seen = []
        def handler(request):
            seen.append(request.headers.get("accept-encoding"))
            return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch("animemachine.network.transport._client_for", return_value=client):
            response = transport.request("GET", "https://example.invalid/data.json")
        client.close()
        self.assertEqual(b"{}", response.content)
        self.assertEqual(["identity"], seen)

    def test_slow_first_endpoint_is_hedged(self):
        endpoints=[registry.Endpoint("slow","x","https://slow.invalid","official"),
                   registry.Endpoint("fast","x","https://fast.invalid","community_mirror")]
        def request(_method, url, **_kwargs):
            if "slow" in url: time.sleep(1.2)
            return FakeResponse(json.dumps({"url":url}).encode(),url)
        started=time.monotonic()
        profile = {"routeMode": "direct", "id": "test-network"}
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch("animemachine.network.transport.request", side_effect=request), \
                mock.patch.object(transport, "network_profile", return_value=profile):
            result, endpoint, _ = hedging.first_valid(endpoints, validator=lambda data,_:json.loads(data),
                health=__import__("animemachine.network.health",fromlist=["Store"]).Store(Path(raw)/"h.sqlite3"))
            elapsed=time.monotonic()-started
            time.sleep(1.3)
        self.assertEqual("fast",endpoint.id); self.assertLess(elapsed,1.8); self.assertIn("fast",result["url"])

    def test_honor_cooldown_skips_persistently_unhealthy_endpoint(self):
        endpoints = [
            registry.Endpoint("bad", "update", "https://bad.invalid", "official"),
            registry.Endpoint("good", "update", "https://good.invalid", "official"),
        ]
        calls = []
        profile = {"routeMode": "direct", "id": "test-network"}
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "h.sqlite3")
            store.failure("bad", "update_json", "ConnectError", network_id="test-network")
            store.failure("bad", "update_json", "ConnectError", network_id="test-network")
            def request(_method, url, **_kwargs):
                calls.append(url)
                return FakeResponse(b"{}", url)
            with mock.patch.object(transport, "network_profile", return_value=profile), \
                 mock.patch.object(transport, "request", side_effect=request):
                result, selected, _ = hedging.first_valid(
                    endpoints, validator=lambda data, _mime: json.loads(data),
                    capability="update_json", health=store, hedge_delays=(0,), honor_cooldown=True,
                )
        self.assertEqual({}, result)
        self.assertEqual("good", selected.id)
        self.assertEqual(["https://good.invalid"], calls)

    def test_credentials_never_reach_public_mirror(self):
        endpoints=[registry.Endpoint("mirror","x","https://mirror.invalid","community_mirror")]
        seen=[]
        def request(_method,_url,**kwargs): seen.append(kwargs.get("headers")); return FakeResponse()
        with tempfile.TemporaryDirectory() as raw, mock.patch("animemachine.network.transport.request",side_effect=request):
            hedging.first_valid(endpoints,validator=lambda data,_:data,headers={"Authorization":"Bearer secret"},credentials=True,
                health=__import__("animemachine.network.health",fromlist=["Store"]).Store(Path(raw)/"h.sqlite3"))
        self.assertFalse(seen[0] and "Authorization" in seen[0])

    def test_transient_failure_is_retried_within_one_endpoint(self):
        endpoint=registry.Endpoint("direct","x","https://direct.invalid","official")
        calls=[]
        def request(method,url,**kwargs):
            calls.append(url)
            if len(calls) < 3:
                raise httpx.ReadTimeout("interrupted", request=httpx.Request(method,url))
            return FakeResponse(b"{}",url)
        with tempfile.TemporaryDirectory() as raw, mock.patch("animemachine.network.transport.request",side_effect=request):
            result, selected, _ = hedging.first_valid([endpoint], validator=lambda data,_:json.loads(data),
                attempts_per_endpoint=3, retry_backoff=0, hedge_delays=(0,),
                health=__import__("animemachine.network.health",fromlist=["Store"]).Store(Path(raw)/"h.sqlite3"))
        self.assertEqual({}, result); self.assertEqual("direct", selected.id); self.assertEqual(3, len(calls))

    def test_direct_failure_uses_valid_image_mirror(self):
        buffer=io.BytesIO(); Image.new("RGB",(12,18),"green").save(buffer,"PNG")
        def request(_method,url,**_kwargs):
            if "direct.invalid" in url:
                raise httpx.ConnectError("offline", request=httpx.Request("GET",url))
            response=FakeResponse(buffer.getvalue(),url); response.headers["content-type"]="image/png"; return response
        with mock.patch("animemachine.network.transport.request",side_effect=request):
            data,mime,url=sources.fetch_binary(
                ["https://direct.invalid/a.jpg","https://proxy.invalid/a.jpg"], timeout=1,
                validator=validators.image_bytes, attempts=2)
        self.assertEqual("image/webp",mime); self.assertTrue(data); self.assertIn("proxy.invalid",url)

    def test_invalid_proxy_payload_does_not_prevent_source_switch(self):
        buffer=io.BytesIO(); Image.new("RGB",(12,18),"yellow").save(buffer,"PNG")
        def request(_method,url,**_kwargs):
            if "proxy.invalid" in url:
                response=FakeResponse(b"<html>temporary error</html>",url); response.headers["content-type"]="text/html"; return response
            response=FakeResponse(buffer.getvalue(),url); response.headers["content-type"]="image/png"; return response
        with mock.patch("animemachine.network.transport.request",side_effect=request):
            data,mime,url=sources.fetch_binary(
                ["https://proxy.invalid/a.jpg","https://direct.invalid/a.jpg"], timeout=1,
                validator=validators.image_bytes, attempts=2)
        self.assertEqual("image/webp",mime); self.assertTrue(data); self.assertIn("direct.invalid",url)

    def test_live_proxy_route_distinguishes_environment_system_and_direct(self):
        transport.reset()
        with mock.patch.object(transport, "_environment_proxies", return_value={}), \
                mock.patch.object(transport, "_system_proxies", return_value={}):
            direct = transport.proxy_route("https://example.invalid/data")
        self.assertEqual("direct", direct["mode"])

        with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://user:secret@127.0.0.1:8080"}), \
                mock.patch.object(transport, "_system_proxies", return_value={"https": "http://127.0.0.1:8888"}):
            environment = transport.proxy_route("https://example.invalid/data")
        self.assertEqual("environment_proxy", environment["mode"])
        self.assertEqual("http://127.0.0.1:8080", environment["proxy"])
        self.assertNotIn("secret", environment["proxy"])

        with mock.patch.object(transport, "_environment_proxies", return_value={}), \
                mock.patch.object(transport, "_system_proxies", return_value={"https": "http://127.0.0.1:8888"}), \
                mock.patch.object(transport.os, "name", "nt"):
            system = transport.proxy_route("https://example.invalid/data")
        self.assertEqual("windows_system_proxy", system["mode"])
        self.assertEqual("http://127.0.0.1:8888", system["proxy"])


    def test_route_failover_uses_system_proxy_when_environment_proxy_is_broken(self):
        transport.reset()
        calls = []
        def client_for(proxy, _revision):
            def handler(request):
                calls.append(proxy or "direct")
                if proxy == "http://127.0.0.1:8080":
                    raise httpx.ConnectError("environment proxy offline", request=request)
                return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})
            return httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://127.0.0.1:8080"}), \
                mock.patch.object(transport, "_system_proxies", return_value={"https": "http://127.0.0.1:8888"}), \
                mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
                mock.patch.object(transport.os, "name", "nt"), \
                mock.patch.object(transport, "_client_for", side_effect=client_for):
            first = transport.request("GET", "https://example.invalid/data.json")
            second = transport.request("GET", "https://example.invalid/data.json")
        self.assertEqual("windows_system_proxy", first.extensions["animemachine_proxy_route"]["mode"])
        self.assertEqual("windows_system_proxy", second.extensions["animemachine_proxy_route"]["mode"])
        self.assertEqual(["http://127.0.0.1:8080", "http://127.0.0.1:8888", "http://127.0.0.1:8888"], calls)

    def test_proxy_auth_failure_falls_back_to_direct_without_marking_online(self):
        transport.reset()
        connectivity.reset()
        connectivity.note_probe(False, now=100.0)
        calls = []
        def client_for(proxy, _revision):
            def handler(request):
                calls.append(proxy or "direct")
                if proxy:
                    return httpx.Response(407, request=request)
                return httpx.Response(200, content=b"ok", request=request)
            return httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://127.0.0.1:8080"}), \
                mock.patch.object(transport, "_system_proxies", return_value={}), \
                mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
                mock.patch.object(transport, "_client_for", side_effect=client_for):
            response = transport.request("GET", "https://example.invalid/data")
        self.assertEqual(b"ok", response.content)
        self.assertEqual("direct", response.extensions["animemachine_proxy_route"]["mode"])
        self.assertEqual(["http://127.0.0.1:8080", "direct"], calls)
        self.assertFalse(connectivity.outage_suspected())

    def test_route_failover_reaches_direct_when_both_proxy_routes_are_broken(self):
        transport.reset()
        calls = []
        def client_for(proxy, _revision):
            def handler(request):
                calls.append(proxy or "direct")
                if proxy:
                    raise httpx.ConnectError("proxy offline", request=request)
                return httpx.Response(200, content=b"ok")
            return httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://127.0.0.1:8080"}), \
                mock.patch.object(transport, "_system_proxies", return_value={"https": "http://127.0.0.1:8888"}), \
                mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
                mock.patch.object(transport.os, "name", "nt"), \
                mock.patch.object(transport, "_client_for", side_effect=client_for):
            response = transport.request("GET", "https://example.invalid/file")
        self.assertEqual(b"ok", response.content)
        self.assertEqual("direct", response.extensions["animemachine_proxy_route"]["mode"])
        self.assertEqual(["http://127.0.0.1:8080", "http://127.0.0.1:8888", "direct"], calls)


    def test_malformed_environment_proxy_falls_back_to_direct(self):
        transport.reset()
        calls = []

        def client_for(proxy, _revision):
            calls.append(proxy or "direct")
            if proxy:
                raise httpx.InvalidURL("Invalid proxy")
            return httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"ok", request=request)
            ))

        with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://127.0.0.1:notaport"}), \
                mock.patch.object(transport, "_system_proxies", return_value={}), \
                mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
                mock.patch.object(transport, "_client_for", side_effect=client_for):
            routes = transport.route_candidates("https://example.invalid/data")
            response = transport.request("GET", "https://example.invalid/data")

        self.assertEqual("<invalid-proxy>", routes[0]["proxy"])
        self.assertEqual(b"ok", response.content)
        self.assertEqual("direct", response.extensions["animemachine_proxy_route"]["mode"])
        self.assertEqual(["http://127.0.0.1:notaport", "direct"], calls)

    def test_socks_proxy_without_optional_transport_falls_back_to_direct(self):
        transport.reset()
        calls = []

        def client_for(proxy, _revision):
            calls.append(proxy or "direct")
            if proxy:
                raise ImportError("socksio unavailable")
            return httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"ok", request=request)
            ))

        with mock.patch.object(transport, "_environment_proxies", return_value={"https": "socks5://127.0.0.1:1080"}), \
                mock.patch.object(transport, "_system_proxies", return_value={}), \
                mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False), \
                mock.patch.object(transport, "_client_for", side_effect=client_for):
            response = transport.request("GET", "https://example.invalid/data")

        self.assertEqual(b"ok", response.content)
        self.assertEqual("direct", response.extensions["animemachine_proxy_route"]["mode"])
        self.assertEqual(["socks5://127.0.0.1:1080", "direct"], calls)

    def test_route_candidates_do_not_duplicate_identical_environment_and_system_proxy(self):
        transport.reset()
        with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://127.0.0.1:8080"}), \
                mock.patch.object(transport, "_system_proxies", return_value={"https": "http://127.0.0.1:8080"}), \
                mock.patch.object(transport.urllib.request, "proxy_bypass", return_value=False):
            routes = transport.route_candidates("https://example.invalid/data")
        self.assertEqual(["environment_proxy", "direct"], [item["mode"] for item in routes])

    def test_proxy_revision_changes_when_proxy_is_toggled_and_restored(self):
        transport.reset()
        with mock.patch.object(transport, "_system_proxies", return_value={}):
            with mock.patch.object(transport, "_environment_proxies", return_value={}):
                first = transport.proxy_route("https://example.invalid/data")["revision"]
            with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://127.0.0.1:8080"}):
                second = transport.proxy_route("https://example.invalid/data")["revision"]
            with mock.patch.object(transport, "_environment_proxies", return_value={}):
                third = transport.proxy_route("https://example.invalid/data")["revision"]
            with mock.patch.object(transport, "_environment_proxies", return_value={"https": "http://127.0.0.1:8080"}):
                fourth = transport.proxy_route("https://example.invalid/data")["revision"]
        self.assertEqual([first + 1, first + 2, first + 3], [second, third, fourth])

    def test_local_requests_remain_direct_when_proxy_is_configured(self):
        transport.reset()
        with mock.patch.object(transport, "_environment_proxies", return_value={"http": "http://127.0.0.1:8080"}), \
                mock.patch.object(transport, "_system_proxies", return_value={}):
            route = transport.proxy_route("http://127.0.0.1:7789/api/about")
        self.assertEqual("direct", route["mode"])
        self.assertEqual("local", route["reason"])

    def test_hedged_success_uses_the_response_route_for_network_profile(self):
        endpoint = registry.Endpoint("mirror", "x", "https://mirror.invalid", "official")
        response = FakeResponse(b"{}", "https://mirror.invalid")
        response.extensions = {"animemachine_proxy_route": {"mode": "environment_proxy"}}
        store = mock.Mock()
        store.rank.return_value = ([endpoint.id], {})

        def profile(_url, route_mode=None):
            mode = route_mode or "direct"
            return {"routeMode": mode, "id": f"profile-{mode}"}

        with mock.patch.object(transport, "network_profile", side_effect=profile), \
                mock.patch.object(transport, "request", return_value=response):
            hedging.first_valid([endpoint], validator=lambda data, _mime: json.loads(data),
                                health=store, hedge_delays=(0,))

        success = store.success.call_args.kwargs
        self.assertEqual("environment_proxy", success["route_mode"])
        self.assertEqual("profile-environment_proxy", success["network_id"])

    def test_diagnostic_success_uses_the_response_route_for_network_profile(self):
        response = FakeResponse(b"{}", "https://mirror.invalid/data.json")
        response.extensions = {"animemachine_proxy_route": {"mode": "windows_system_proxy"}}
        store = mock.Mock()
        store.snapshot.return_value = {}

        def profile(_url, route_mode=None):
            mode = route_mode or "direct"
            return {"routeMode": mode, "id": f"profile-{mode}"}

        item = {"service": "bangumi_api", "capability": "json",
                "baseUrl": "https://mirror.invalid", "probeUrl": "https://mirror.invalid/data.json"}
        with mock.patch.object(transport, "network_profile", side_effect=profile), \
                mock.patch.object(transport, "proxy_route", return_value={"mode": "windows_system_proxy"}), \
                mock.patch.object(transport, "request", return_value=response):
            diagnostics._probe_json(item, store, 1.0)

        success = store.success.call_args.kwargs
        self.assertEqual("windows_system_proxy", success["route_mode"])
        self.assertEqual("profile-windows_system_proxy", success["network_id"])

    def test_health_learning_and_trends_are_isolated_by_network_route(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "health.sqlite3")
            for _ in range(4):
                store.success("mirror-a", "binary", .2, 2 * 1024 * 1024, route_mode="direct")
                store.failure("mirror-a", "binary", "ConnectError", route_mode="environment_proxy")
                store.success("mirror-b", "binary", .8, 128 * 1024, route_mode="environment_proxy")
            self.assertLess(
                store.score("mirror-a", "binary", "direct"),
                store.score("mirror-b", "binary", "direct"),
            )
            self.assertGreater(
                store.score("mirror-a", "binary", "environment_proxy"),
                store.score("mirror-b", "binary", "environment_proxy"),
            )
            direct = store.snapshot("mirror-a", "binary", "direct")
            proxied = store.snapshot("mirror-a", "binary", "environment_proxy")
            self.assertEqual(4, direct["samples"])
            self.assertEqual(4, proxied["samples"])
            self.assertEqual(1.0, direct["recentSuccessRate"])
            self.assertEqual(0.0, proxied["recentSuccessRate"])
            trends = store.trend("mirror-a", "binary")
            self.assertTrue(trends["direct"])
            self.assertTrue(trends["environment_proxy"])
            self.assertEqual(0.0, trends["environment_proxy"][-1]["score"])
            self.assertFalse(trends["windows_system_proxy"])

    def test_diagnostics_explains_selected_source(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "health.sqlite3")
            items = [
                {"service": "bangumi_image", "capability": "binary", "baseUrl": "https://a.invalid", "probeUrl": "https://a.invalid/a.jpg"},
                {"service": "bangumi_image", "capability": "binary", "baseUrl": "https://b.invalid", "probeUrl": "https://b.invalid/a.jpg"},
            ]
            rendered = [{}, {}]
            with mock.patch.object(transport, "proxy_route", return_value={"mode": "direct"}):
                diagnostics._annotate_selection(items, rendered, store)
            selected = [item for item in rendered if item.get("selection", {}).get("selected")]
            self.assertEqual(1, len(selected))
            self.assertTrue(selected[0]["selection"]["reason"])
            self.assertIn("confidence", selected[0]["selection"])

    def test_network_profile_changes_with_lan_or_proxy_environment(self):
        base_a = {"kind": "wifi", "label": "Lab-A", "baseKey": "wifi|Lab-A||192.168.1.0/24"}
        base_b = {"kind": "lan", "label": "eth0", "baseKey": "lan||eth0|10.0.0.0/24"}
        with mock.patch.object(transport, "_active_network_base", return_value=base_a), \
                mock.patch.object(transport, "_proxy_decision", return_value=("direct", None, 1, "none")):
            direct_a = transport.network_profile("https://example.invalid")
        with mock.patch.object(transport, "_active_network_base", return_value=base_b), \
                mock.patch.object(transport, "_proxy_decision", return_value=("direct", None, 1, "none")):
            direct_b = transport.network_profile("https://example.invalid")
        with mock.patch.object(transport, "_active_network_base", return_value=base_a), \
                mock.patch.object(transport, "_proxy_decision", return_value=("environment_proxy", "http://127.0.0.1:8080", 2, "environment")):
            proxy_a = transport.network_profile("https://example.invalid")
        self.assertNotEqual(direct_a["id"], direct_b["id"])
        self.assertNotEqual(direct_a["id"], proxy_a["id"])
        self.assertEqual("Lab-A", direct_a["label"])

    def test_network_profile_can_address_each_route_without_changing_current_profile_ids(self):
        base = {"kind": "wifi", "label": "Lab-A", "baseKey": "wifi|Lab-A|wlan0|10.0.0.0/24", "localAddress": "10.0.0.2"}
        with mock.patch.object(transport, "_active_network_base", return_value=base), \
                mock.patch.object(transport, "_proxy_snapshot", return_value=(
                    {"https": "http://127.0.0.1:1080"}, {"https": "http://127.0.0.1:8080"}, 1)):
            direct = transport.network_profile("https://example.invalid", "direct")
            environment = transport.network_profile("https://example.invalid", "environment_proxy")
            system = transport.network_profile("https://example.invalid", "windows_system_proxy")
        self.assertEqual("direct", direct["routeMode"])
        self.assertEqual("environment_proxy", environment["routeMode"])
        self.assertEqual("windows_system_proxy", system["routeMode"])
        self.assertEqual("http://127.0.0.1:1080", environment["proxy"])
        self.assertEqual("http://127.0.0.1:8080", system["proxy"])
        self.assertEqual(20, len(direct["id"]))
        self.assertEqual({20}, {len(direct["id"]), len(environment["id"]), len(system["id"])})

    def test_health_learning_is_isolated_by_network_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "health.sqlite3")
            store.success("mirror", "binary", .1, 1024 * 1024, network_id="wifi-a")
            store.failure("mirror", "binary", "ReadTimeout", network_id="wifi-b")
            wifi_a = store.snapshot("mirror", "binary", network_id="wifi-a")
            wifi_b = store.snapshot("mirror", "binary", network_id="wifi-b")
        self.assertEqual(1, wifi_a["samples"])
        self.assertEqual(1, wifi_b["samples"])
        self.assertEqual(1.0, wifi_a["successRate"])
        self.assertEqual(0.0, wifi_b["successRate"])

    def test_health_schema_migration_preserves_existing_sample_confidence(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "health.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "CREATE TABLE endpoint_route_health("
                    "endpoint_id TEXT NOT NULL,capability TEXT NOT NULL,route_mode TEXT NOT NULL,"
                    "successes INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,"
                    "consecutive_failures INTEGER NOT NULL DEFAULT 0,latency_ewma REAL,throughput_ewma REAL,"
                    "cooldown_until REAL NOT NULL DEFAULT 0,last_status TEXT,last_updated REAL NOT NULL,"
                    "success_ewma REAL,last_failure TEXT,last_failure_at REAL,"
                    "PRIMARY KEY(endpoint_id,capability,route_mode))"
                )
                db.execute(
                    "INSERT INTO endpoint_route_health VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("mirror", "binary", "direct", 7, 1, 0, .2, 1024 * 1024, 0, "ok", 1000.0, .9, "", 0),
                )
            with mock.patch("animemachine.network.health.time.time", return_value=1000.0):
                store = health.Store(path)
                snapshot = store.snapshot("mirror", "binary")
            self.assertEqual(8.0, snapshot["effectiveSamples"])
            self.assertGreater(snapshot["confidence"], 0.5)

    def test_health_confidence_and_recent_success_decay_with_age(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "health.sqlite3")
            with mock.patch("animemachine.network.health.time.time", return_value=1000.0):
                for _ in range(6):
                    store.success("mirror", "binary", .2, 1024 * 1024)
                fresh = store.snapshot("mirror", "binary")
            with mock.patch("animemachine.network.health.time.time", return_value=1000.0 + 6 * 3600):
                aged = store.snapshot("mirror", "binary")
            self.assertGreater(fresh["confidence"], aged["confidence"])
            self.assertGreater(fresh["decayedSuccessRate"], aged["decayedSuccessRate"])
            self.assertAlmostEqual(.75, aged["decayedSuccessRate"], places=3)
            self.assertLess(aged["effectiveSamples"], fresh["effectiveSamples"])

    def test_source_hysteresis_ignores_small_low_confidence_advantage_then_switches(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "health.sqlite3")
            candidates = [("mirror-a", "direct"), ("mirror-b", "direct")]
            with mock.patch("animemachine.network.health.time.time", return_value=1000.0):
                ranked, initial = store.rank(candidates, "binary")
            self.assertEqual("mirror-a", ranked[0])
            self.assertEqual("best_quality", initial["reason"])

            with mock.patch("animemachine.network.health.time.time", return_value=1100.0):
                store.success("mirror-b", "binary", .15, 2 * 1024 * 1024)
                ranked, retained = store.rank(candidates, "binary")
            self.assertEqual("mirror-a", ranked[0])
            self.assertEqual("hysteresis_margin", retained["reason"])
            self.assertGreater(retained["switchThreshold"], retained["advantage"])

            with mock.patch("animemachine.network.health.time.time", return_value=1200.0):
                for _ in range(20):
                    store.success("mirror-b", "binary", .1, 4 * 1024 * 1024)
                ranked, switched = store.rank(candidates, "binary")
            self.assertEqual("mirror-b", ranked[0])
            self.assertEqual("meaningfully_better", switched["reason"])
            self.assertTrue(switched["changed"])

    def test_adaptive_health_score_uses_recent_success_latency_and_throughput(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "health.sqlite3")
            for _ in range(4):
                store.success("fast", "binary", .25, 2 * 1024 * 1024)
                store.success("slow", "binary", .9, 64 * 1024)
            self.assertLess(store.score("fast", "binary"), store.score("slow", "binary"))
            for _ in range(3):
                store.failure("fast", "binary", "ReadTimeout")
            self.assertGreater(store.score("fast", "binary"), store.score("slow", "binary"))
            snapshot = store.snapshot("fast", "binary")
            self.assertLess(snapshot["recentSuccessRate"], snapshot["successRate"])
            self.assertEqual("ReadTimeout", snapshot["lastFailure"])
