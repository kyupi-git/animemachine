from __future__ import annotations

import unittest
from unittest import mock

import httpx

from animemachine.torrents import collector


class TorrentCollectorNetworkTests(unittest.TestCase):
    def test_dmhy_download_uses_actual_host_and_path(self) -> None:
        url = "https://dl.dmhy.org/2026/example.torrent?download=1"
        with mock.patch.object(collector, "get_text", return_value=(f'<a href="{url}">Torrent</a>', "")):
            self.assertEqual(url, collector.resolve_dmhy_download("https://share.dmhy.org/topics/view/1.html"))
        for invalid in ("https://evil.invalid/x?source=dl.dmhy.org&name=x.torrent",
                        "https://dl.dmhy.org.evil.invalid/x.torrent", "https://evil.invalid/x.torrent",
                        "https://dl.dmhy.org@evil.invalid/x.torrent", "javascript:dl.dmhy.org/x.torrent"):
            with self.subTest(url=invalid), \
                    mock.patch.object(collector, "get_text", return_value=(f'<a href="{invalid}">Torrent</a>', "")), \
                    self.assertRaisesRegex(ValueError, "no .torrent link"):
                collector.resolve_dmhy_download("https://share.dmhy.org/topics/view/1.html")

    def test_standard_http_uses_shared_adaptive_transport(self) -> None:
        request = httpx.Request("GET", "https://example.invalid/feed")
        response = httpx.Response(200, content=b"ok", headers={"Content-Type": "text/plain"}, request=request)
        with mock.patch.object(collector, "PROXY_ENABLED", False), \
                mock.patch.object(collector.network_transport, "request", return_value=response) as shared:
            raw, content_type, final = collector.http_request_once(str(request.url), timeout=7, max_bytes=4096)
        self.assertEqual(b"ok", raw)
        self.assertEqual("text/plain", content_type)
        self.assertEqual(str(request.url), final)
        shared.assert_called_once()
        self.assertEqual(7, shared.call_args.kwargs["timeout"])
        self.assertEqual(4096, shared.call_args.kwargs["max_bytes"])

    def test_legacy_socks_failure_falls_back_to_shared_transport(self) -> None:
        expected = (b"ok", "application/json", "https://example.invalid/api")
        with mock.patch.object(collector, "PROXY_ENABLED", True), \
                mock.patch.object(collector, "_native_http_request_once", side_effect=OSError("proxy offline")) as native, \
                mock.patch.object(collector, "_shared_http_request_once", return_value=expected) as shared:
            result = collector.http_request_once("https://example.invalid/api")
        self.assertEqual(expected, result)
        native.assert_called_once()
        shared.assert_called_once()


if __name__ == "__main__":
    unittest.main()
