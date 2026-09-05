from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from animemachine.network import connectivity, diagnostics, health, transport


class NetworkConnectivityStateTests(unittest.TestCase):
    def setUp(self):
        connectivity.reset()

    def tearDown(self):
        connectivity.reset()

    def test_offline_requires_thirty_minutes_of_continuous_failed_probes(self):
        self.assertFalse(connectivity.note_probe(False, now=100.0)["offline"])
        for stamp in range(160, 1900, 60):
            self.assertFalse(connectivity.note_probe(False, now=float(stamp))["offline"])
        state = connectivity.note_probe(False, now=1900.0)
        self.assertTrue(state["offline"])
        self.assertTrue(state["enteredOffline"])

    def test_long_probe_gap_restarts_offline_confirmation_window(self):
        connectivity.note_probe(False, now=100.0)
        connectivity.note_probe(False, now=100.0 + connectivity.MAX_FAILED_PROBE_GAP_SECONDS + 1)
        state = connectivity.note_probe(False, now=100.0 + connectivity.MAX_FAILED_PROBE_GAP_SECONDS + 1 + 1799)
        self.assertFalse(state["offline"])

    def test_network_environment_change_restarts_offline_confirmation_window(self):
        connectivity.note_environment("network-a")
        connectivity.note_probe(False, now=100.0)
        for stamp in range(160, 1841, 60):
            connectivity.note_probe(False, now=float(stamp))
        self.assertTrue(connectivity.outage_suspected())
        self.assertFalse(connectivity.is_offline())
        self.assertTrue(connectivity.note_environment("network-b", now=1841.0))
        state = connectivity.note_probe(False, now=1841.0)
        self.assertFalse(state["offline"])
        self.assertEqual(0.0, state["failedForSeconds"])

    def test_network_environment_change_releases_confirmed_local_mode_for_fresh_probe(self):
        connectivity.note_environment("network-a")
        connectivity.note_probe(False, now=100.0)
        for stamp in range(160, 1901, 60):
            connectivity.note_probe(False, now=float(stamp))
        self.assertTrue(connectivity.is_offline())
        self.assertTrue(connectivity.note_environment("network-b", now=1901.0))
        self.assertFalse(connectivity.is_offline())
        self.assertTrue(connectivity.outage_suspected())
        self.assertFalse(connectivity.failure_learning_allowed())

    def test_successful_remote_activity_interrupts_offline_candidate(self):
        connectivity.note_probe(False, now=100.0)
        connectivity.note_online_activity(now=1000.0)
        state = connectivity.note_probe(False, now=1901.0)
        self.assertFalse(state["offline"])
        self.assertEqual(0.0, state["failedForSeconds"])

    def test_success_during_a_failing_probe_wins_the_race(self):
        connectivity.note_probe(False, now=100.0)
        connectivity.note_online_activity(now=200.0)
        state = connectivity.note_probe(False, now=201.0, started_at=150.0)
        self.assertFalse(state["offline"])
        self.assertFalse(state["outageSuspected"])
        self.assertEqual(0.0, state["failedForSeconds"])

    def test_confirmed_offline_recovers_on_successful_probe(self):
        connectivity.note_probe(False, now=100.0)
        for stamp in range(160, 1901, 60):
            connectivity.note_probe(False, now=float(stamp))
        self.assertTrue(connectivity.is_offline())
        state = connectivity.note_probe(True, now=1901.0)
        self.assertFalse(state["offline"])
        self.assertTrue(state["recovered"])

    def test_confirmed_local_mode_blocks_remote_transport_but_allows_loopback(self):
        connectivity.set_forced_offline(True)
        with self.assertRaises(connectivity.OfflineModeError):
            transport.request("GET", "https://example.invalid/data", timeout=.1)

        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"ok", request=request)
        ))
        route = {"mode": "direct", "proxy": None, "reason": "local", "revision": 0}
        try:
            with mock.patch.object(transport, "_route_candidates", return_value=[route]), \
                    mock.patch.object(transport, "client", return_value=client), \
                    mock.patch.object(transport, "_remember_route"):
                response = transport.request("GET", "http://127.0.0.1:7789/health", timeout=.1)
            self.assertEqual(b"ok", response.content)
        finally:
            client.close()


    def test_recovery_permission_is_applied_inside_connectivity_worker_threads(self):
        items = [
            {"service": "archive_descriptor", "capability": "json", "probeUrl": "https://a.invalid/x", "baseUrl": ""},
            {"service": "bangumi_api", "capability": "json", "probeUrl": "https://b.invalid/x", "baseUrl": ""},
        ]
        connectivity.set_forced_offline(True)
        seen = []
        with mock.patch.object(diagnostics, "_configured_endpoints", return_value=(items, "")), \
                mock.patch.object(diagnostics, "_light_probe", side_effect=lambda *_args: seen.append(connectivity.recovery_allowed()) or True):
            self.assertTrue(diagnostics.connectivity_probe(Path("unused.sqlite3"), {}, timeout=.1))
        self.assertTrue(seen)
        self.assertTrue(all(seen))

    def test_recovery_permission_is_applied_inside_canary_worker_threads(self):
        connectivity.set_forced_offline(True)
        seen = []
        with mock.patch.object(diagnostics, "_light_probe", side_effect=lambda *_args: seen.append(connectivity.recovery_allowed()) or True):
            self.assertTrue(diagnostics.internet_canary_probe(timeout=.1))
        self.assertTrue(seen)
        self.assertTrue(all(seen))

    def test_proxy_bypass_does_not_bypass_confirmed_offline_mode(self):
        connectivity.set_forced_offline(True)
        with mock.patch("urllib.request.proxy_bypass", return_value=True):
            with self.assertRaises(connectivity.OfflineModeError):
                transport.request("GET", "https://remote.example/data", timeout=.1)

    def test_remote_proxy_bypass_success_still_proves_connectivity(self):
        connectivity.note_probe(False, now=100.0)
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(204, request=request)
        ))
        route = {"mode": "direct", "proxy": None, "reason": "bypass", "revision": 0}
        try:
            with mock.patch("urllib.request.proxy_bypass", return_value=True), \
                    mock.patch.object(transport, "_route_candidates", return_value=[route]), \
                    mock.patch.object(transport, "client", return_value=client), \
                    mock.patch.object(transport, "_remember_route"):
                with mock.patch.object(connectivity, "note_online_activity") as online:
                    transport.request("GET", "https://remote.example/data", timeout=.1)
            online.assert_called_once_with()
        finally:
            client.close()

    def test_arbitrary_single_label_host_is_not_treated_as_local(self):
        connectivity.set_forced_offline(True)
        with self.assertRaises(connectivity.OfflineModeError):
            transport.request("GET", "http://example/service", timeout=.1)

    def test_local_service_names_remain_available_in_confirmed_offline_mode(self):
        connectivity.set_forced_offline(True)
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"ok", request=request)
        ))
        route = {"mode": "direct", "proxy": None, "reason": "local", "revision": 0}
        try:
            with mock.patch.object(transport, "_route_candidates", return_value=[route]), \
                    mock.patch.object(transport, "client", return_value=client), \
                    mock.patch.object(transport, "_remember_route"):
                response = transport.request("GET", "http://qbittorrent:8080/api/v2/app/version", timeout=.1)
                self.assertEqual(b"ok", response.content)
                response = transport.request("GET", "http://ani-rss.local:7789/health", timeout=.1)
                self.assertEqual(b"ok", response.content)
        finally:
            client.close()

    def test_confirmed_offline_blocks_remote_redirect_from_local_endpoint(self):
        transport.reset()
        pooled = transport._client_for(None, transport.proxy_revision())
        self.assertIn(transport._offline_request_guard, pooled.event_hooks.get("request", []))
        transport.reset()
        connectivity.set_forced_offline(True)
        seen = []
        def handler(request):
            seen.append(str(request.url))
            if request.url.host == "127.0.0.1":
                return httpx.Response(302, headers={"Location": "https://remote.example/data"}, request=request)
            return httpx.Response(200, request=request)
        client = httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True,
            event_hooks={"request": [transport._offline_request_guard]},
        )
        try:
            with self.assertRaises(connectivity.OfflineModeError):
                client.get("http://127.0.0.1:7789/start")
            self.assertEqual(["http://127.0.0.1:7789/start"], seen)
        finally:
            client.close()

    def test_suspected_or_confirmed_outage_failures_do_not_pollute_health_learning(self):
        with tempfile.TemporaryDirectory() as raw:
            store = health.Store(Path(raw) / "network-health.sqlite3")
            connectivity.note_probe(False, now=100.0)
            self.assertTrue(connectivity.outage_suspected())
            self.assertFalse(connectivity.is_offline())
            store.failure("endpoint", "json", "ConnectError")
            self.assertEqual(0, store.snapshot("endpoint", "json")["samples"])
            connectivity.note_online_activity(now=101.0)
            store.failure("endpoint", "json", "ConnectError")
            self.assertEqual(1, store.snapshot("endpoint", "json")["samples"])

    def test_worker_can_suppress_failure_learning_without_entering_local_mode(self):
        connectivity.set_forced_failure_suppression(True)
        self.assertFalse(connectivity.is_offline())
        self.assertTrue(connectivity.outage_suspected())
        self.assertFalse(connectivity.failure_learning_allowed())
        connectivity.set_forced_failure_suppression(False)
        self.assertTrue(connectivity.failure_learning_allowed())

    def test_http_connectivity_canary_rejects_captive_portal_body(self):
        item = {
            "service": "internet_canary", "capability": "binary",
            "probeUrl": "http://canary.invalid/connecttest.txt", "baseUrl": "http://canary.invalid",
            "expectedBody": "Microsoft Connect Test",
        }
        response = mock.MagicMock()
        response.status_code = 200
        response.iter_bytes.return_value = iter([b"Sign in to this network"])
        response.extensions = {}
        manager = mock.MagicMock()
        manager.__enter__.return_value = response
        manager.__exit__.return_value = False
        profile = {"routeMode": "direct", "id": "network"}
        with mock.patch.object(transport, "stream", return_value=manager), \
                mock.patch.object(transport, "network_profile", return_value=profile), \
                mock.patch.object(diagnostics, "_health_id", return_value="canary"):
            self.assertFalse(diagnostics._light_probe(item, None, .1))

        response.iter_bytes.return_value = iter([b"Microsoft Connect Test\n"])
        with mock.patch.object(transport, "stream", return_value=manager), \
                mock.patch.object(transport, "network_profile", return_value=profile), \
                mock.patch.object(diagnostics, "_health_id", return_value="canary"):
            self.assertTrue(diagnostics._light_probe(item, None, .1))

    def test_proxy_auth_response_is_not_treated_as_connectivity_proof(self):
        item = {"service": "canary", "capability": "json", "probeUrl": "https://canary.invalid/x", "baseUrl": ""}
        response = mock.MagicMock()
        response.status_code = 407
        response.iter_bytes.return_value = iter([b""])
        response.extensions = {}
        manager = mock.MagicMock()
        manager.__enter__.return_value = response
        manager.__exit__.return_value = False
        profile = {"routeMode": "environment_proxy", "id": "network"}
        with mock.patch.object(transport, "stream", return_value=manager), \
                mock.patch.object(transport, "network_profile", return_value=profile), \
                mock.patch.object(diagnostics, "_health_id", return_value="canary"):
            self.assertFalse(diagnostics._light_probe(item, None, .1))

    def test_connectivity_probe_prefers_independent_services_before_mirrors(self):
        items = [
            {"service": "archive_descriptor", "capability": "json", "probeUrl": "https://a1.invalid/x", "baseUrl": "https://a1.invalid"},
            {"service": "archive_descriptor", "capability": "json", "probeUrl": "https://a2.invalid/x", "baseUrl": "https://a2.invalid"},
            {"service": "bangumi_api", "capability": "json", "probeUrl": "https://b1.invalid/x", "baseUrl": "https://b1.invalid"},
            {"service": "bangumi_image", "capability": "binary", "probeUrl": "https://c1.invalid/x", "baseUrl": "https://c1.invalid"},
        ]
        seen = []
        with mock.patch.object(diagnostics, "_configured_endpoints", return_value=(items, "")), \
                mock.patch.object(diagnostics, "_light_probe", side_effect=lambda item, _store, _timeout: seen.append(item["probeUrl"]) or False), \
                mock.patch.object(diagnostics, "internet_canary_probe", return_value=False):
            self.assertFalse(diagnostics.connectivity_probe(Path("unused.sqlite3"), {}, timeout=.1))
        self.assertEqual(1, seen.count("https://a1.invalid/x"))
        self.assertEqual(1, seen.count("https://b1.invalid/x"))
        self.assertEqual(1, seen.count("https://c1.invalid/x"))
        self.assertEqual(1, seen.count("https://a2.invalid/x"))
        self.assertEqual(["https://a2.invalid/x"], seen[3:])

    def test_configured_fallback_prevents_false_whole_network_outage_when_canaries_are_filtered(self):
        items = [
            {"service": "archive_descriptor", "capability": "json", "probeUrl": "https://a1.invalid/x", "baseUrl": ""},
            {"service": "archive_descriptor", "capability": "json", "probeUrl": "https://a2.invalid/x", "baseUrl": ""},
            {"service": "bangumi_api", "capability": "json", "probeUrl": "https://b1.invalid/x", "baseUrl": ""},
            {"service": "bangumi_image", "capability": "binary", "probeUrl": "https://c1.invalid/x", "baseUrl": ""},
        ]
        seen = []
        def probe(item, _store, _timeout):
            seen.append(item["probeUrl"])
            return item["probeUrl"] == "https://a2.invalid/x"
        with mock.patch.object(diagnostics, "_configured_endpoints", return_value=(items, "")), \
                mock.patch.object(diagnostics, "_light_probe", side_effect=probe), \
                mock.patch.object(diagnostics, "internet_canary_probe", return_value=False):
            self.assertTrue(diagnostics.connectivity_probe(Path("unused.sqlite3"), {}, timeout=.1))
        self.assertEqual("https://a2.invalid/x", seen[-1])

    def test_confirmed_offline_allows_rate_limited_opportunistic_recovery(self):
        connectivity.note_probe(False, now=100.0)
        for stamp in range(160, 1901, 60):
            connectivity.note_probe(False, now=float(stamp))
        self.assertTrue(connectivity.is_offline())
        with connectivity.opportunistic_recovery(now=1901.0) as allowed:
            self.assertTrue(allowed)
            self.assertTrue(connectivity.recovery_allowed())
        with connectivity.opportunistic_recovery(now=1902.0) as allowed:
            self.assertFalse(allowed)
        with connectivity.opportunistic_recovery(
            now=1901.0 + connectivity.OPPORTUNISTIC_RECOVERY_INTERVAL_SECONDS
        ) as allowed:
            self.assertTrue(allowed)

    def test_successful_opportunistic_remote_request_recovers_confirmed_offline(self):
        current = time.monotonic()
        first = current - connectivity.OFFLINE_AFTER_SECONDS
        connectivity.note_environment("test-network", now=first)
        connectivity.note_probe(False, now=first)
        for offset in range(60, connectivity.OFFLINE_AFTER_SECONDS + 1, 60):
            connectivity.note_probe(False, now=first + offset)
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(204, request=request)
        ))
        route = {"mode": "direct", "proxy": None, "reason": "recovery", "revision": 0}
        try:
            with mock.patch.object(transport, "_route_candidates", return_value=[route]), \
                    mock.patch.object(transport, "client", return_value=client), \
                    mock.patch.object(transport, "network_environment_id", return_value="test-network"), \
                    mock.patch.object(transport, "_remember_route"):
                response = transport.request("GET", "https://reachable.example/health", timeout=.1)
            self.assertEqual(204, response.status_code)
            self.assertFalse(connectivity.is_offline())
        finally:
            client.close()

    def test_neutral_canary_prevents_service_outage_from_becoming_whole_network_outage(self):
        items = [
            {"service": "archive_descriptor", "capability": "json", "probeUrl": "https://a.invalid/x", "baseUrl": ""},
            {"service": "bangumi_api", "capability": "json", "probeUrl": "https://b.invalid/x", "baseUrl": ""},
        ]
        with mock.patch.object(diagnostics, "_configured_endpoints", return_value=(items, "")), \
                mock.patch.object(diagnostics, "_light_probe", return_value=False), \
                mock.patch.object(diagnostics, "internet_canary_probe", return_value=True) as canary:
            self.assertTrue(diagnostics.connectivity_probe(Path("unused.sqlite3"), {}, timeout=.1))
        canary.assert_called_once_with(timeout=.1)

    def test_prewarm_candidate_budget_is_two_hosts_per_service(self):
        items = []
        for service in ("archive_descriptor", "bangumi_api", "bangumi_image", "bangumi_subject_cache", "extra"):
            for index in range(4):
                items.append({"service": service, "capability": "json",
                              "probeUrl": f"https://{service}-{index}.invalid/x", "baseUrl": ""})
        selected = diagnostics._prewarm_candidates(items)
        self.assertLessEqual(len(selected), 8)
        counts = {}
        for item in selected:
            counts[item["service"]] = counts.get(item["service"], 0) + 1
        self.assertTrue(all(value <= 2 for value in counts.values()))

    def test_forced_worker_state_is_reported_without_changing_parent_confirmation(self):
        connectivity.set_forced_offline(True)
        state = connectivity.snapshot(now=200.0)
        self.assertTrue(state["offline"])
        self.assertFalse(state["confirmedOffline"])
        self.assertTrue(state["forcedOffline"])


if __name__ == "__main__":
    unittest.main()
