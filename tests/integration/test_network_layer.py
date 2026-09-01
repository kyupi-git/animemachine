from __future__ import annotations

import json
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

from animemachine.network import hedging, registry, sources, transport, validators


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
        with mock.patch("animemachine.network.transport.client", return_value=client):
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
        with tempfile.TemporaryDirectory() as raw, mock.patch("animemachine.network.transport.request",side_effect=request):
            result, endpoint, _ = hedging.first_valid(endpoints, validator=lambda data,_:json.loads(data),
                health=__import__("animemachine.network.health",fromlist=["Store"]).Store(Path(raw)/"h.sqlite3"))
            elapsed=time.monotonic()-started
            time.sleep(1.3)
        self.assertEqual("fast",endpoint.id); self.assertLess(elapsed,1.8); self.assertIn("fast",result["url"])

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
