from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from animemachine.network import downloads


class ArchiveDownloadTests(unittest.TestCase):
    def test_interrupted_range_resumes_from_another_endpoint(self):
        payload = bytes(range(256)) * 4096
        calls = []
        def probe(url): return {"url":url,"range":True,"latency":.01,"throughput":10_000_000 if url.endswith("b") else 9_000_000}
        def stream(url,path,start,end,progress,**_kwargs):
            existing=path.stat().st_size if path.exists() else 0; calls.append((url,start+existing,end))
            remaining=payload[start+existing:end+1]
            if url.endswith("b") and not existing:
                cut=max(1,len(remaining)//2); path.write_bytes(remaining[:cut]); progress(cut); raise OSError("interrupted")
            with path.open("ab") as output: output.write(remaining)
            progress(len(remaining))
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads,"_probe",side_effect=probe), mock.patch.object(downloads,"_stream_range",side_effect=stream):
            target=Path(raw)/"archive.zip"
            result=downloads.download_verified(["https://x/a","https://x/b"],target,expected_size=len(payload),expected_sha256=hashlib.sha256(payload).hexdigest())
            self.assertEqual(payload,target.read_bytes()); self.assertEqual(len(payload),result["size"])
            self.assertTrue(any(start>0 for _url,start,_end in calls))

    def test_tampered_asset_is_never_published(self):
        payload=b"tampered"
        def probe(url): return {"url":url,"range":True,"latency":.01,"throughput":1}
        def stream(_url,path,start,end,_progress,**_kwargs): path.write_bytes(payload[start:end+1])
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads,"_probe",side_effect=probe), mock.patch.object(downloads,"_stream_range",side_effect=stream):
            target=Path(raw)/"archive.zip"
            with self.assertRaisesRegex(ValueError,"digest"):
                downloads.download_verified(["https://x/a"],target,expected_size=len(payload),expected_sha256="0"*64)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".zip.part").exists())

    def test_digest_mismatch_discards_complete_partial_for_next_retry(self):
        payload=b"good-payload"
        class Response:
            status_code=200; headers={"content-length":str(len(payload))}; url="https://x/archive"
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def raise_for_status(self): return None
            def iter_bytes(self,_size): yield payload
        with tempfile.TemporaryDirectory() as raw:
            target=Path(raw)/"archive.zip"
            target.with_suffix(".zip.part").write_bytes(b"x"*len(payload))
            with mock.patch.object(downloads.transport,"stream",return_value=Response()):
                with self.assertRaisesRegex(ValueError,"digest"):
                    downloads.download("https://x/archive",target,expected_size=len(payload),expected_sha256="0"*64)
            self.assertFalse(target.with_suffix(".zip.part").exists())
            with mock.patch.object(downloads.transport,"stream",return_value=Response()):
                downloads.download("https://x/archive",target,expected_size=len(payload),
                                   expected_sha256=hashlib.sha256(payload).hexdigest())
            self.assertEqual(payload,target.read_bytes())

    def test_read_error_and_temporary_5xx_are_retried(self):
        payload=b"retry-me" * 1024
        calls=[]
        request=httpx.Request("GET","https://proxy.invalid/archive")
        response=httpx.Response(503,request=request)
        failures=[httpx.ReadError("connection reset",request=request),
                  httpx.HTTPStatusError("temporary",request=request,response=response)]
        def probe(url): return {"url":url,"range":True,"latency":.01,"throughput":1}
        def stream(url,path,start,end,progress,**_kwargs):
            calls.append(url)
            if failures: raise failures.pop(0)
            with path.open("ab") as output: output.write(payload[start+path.stat().st_size:end+1])
            progress(end-start+1)
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads,"_probe",side_effect=probe), \
                mock.patch.object(downloads,"_stream_range",side_effect=stream), mock.patch.object(downloads.time,"sleep"):
            target=Path(raw)/"archive.zip"
            downloads.download_verified(["https://proxy.invalid/archive"],target,expected_size=len(payload),
                                        expected_sha256=hashlib.sha256(payload).hexdigest(),
                                        attempts_per_source=3,retry_backoff=0)
        self.assertEqual(3,len(calls))

    def test_proxy_exhaustion_switches_to_official_and_resumes(self):
        payload=b"source-switch" * 1024
        calls=[]
        def probe(url): return {"url":url,"range":True,"latency":.01,
                                "throughput":2 if "proxy" in url else 1}
        def stream(url,path,start,end,progress,**_kwargs):
            existing=path.stat().st_size if path.exists() else 0
            calls.append((url,start+existing))
            remaining=payload[start+existing:end+1]
            if "proxy" in url:
                if not existing:
                    cut=max(1,len(remaining)//4)
                    with path.open("ab") as output: output.write(remaining[:cut])
                    progress(cut)
                raise httpx.ReadError("proxy disconnected",request=httpx.Request("GET",url))
            with path.open("ab") as output: output.write(remaining)
            progress(len(remaining))
        urls=["https://proxy.invalid/archive","https://github.com/release/archive"]
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads,"_probe",side_effect=probe), \
                mock.patch.object(downloads,"_stream_range",side_effect=stream), mock.patch.object(downloads.time,"sleep"):
            target=Path(raw)/"archive.zip"
            downloads.download_verified(urls,target,expected_size=len(payload),
                                        expected_sha256=hashlib.sha256(payload).hexdigest(),
                                        attempts_per_source=2,retry_backoff=0)
            self.assertEqual(payload,target.read_bytes())
        self.assertTrue(any("github.com" in url and offset>0 for url,offset in calls))

    def test_restart_reuses_saved_segment(self):
        payload=b"restart-resume" * 1024
        starts=[]
        def probe(url): return {"url":url,"range":True,"latency":.01,"throughput":1}
        def interrupted(_url,path,start,end,progress,**_kwargs):
            existing=path.stat().st_size if path.exists() else 0
            remaining=payload[start+existing:end+1]
            cut=max(1,len(remaining)//3)
            with path.open("ab") as output: output.write(remaining[:cut])
            progress(cut)
            raise httpx.ReadError("process stopped",request=httpx.Request("GET","https://x/archive"))
        def resumed(_url,path,start,end,progress,**_kwargs):
            existing=path.stat().st_size if path.exists() else 0
            starts.append(start+existing)
            remaining=payload[start+existing:end+1]
            with path.open("ab") as output: output.write(remaining)
            progress(len(remaining))
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads,"_probe",side_effect=probe):
            target=Path(raw)/"archive.zip"
            with mock.patch.object(downloads,"_stream_range",side_effect=interrupted), \
                    mock.patch.object(downloads.time,"sleep"), self.assertRaisesRegex(RuntimeError,"range failed"):
                downloads.download_verified(["https://x/archive"],target,expected_size=len(payload),
                                            expected_sha256=hashlib.sha256(payload).hexdigest(),attempts_per_source=1)
            saved=target.with_suffix(".zip.part.0").stat().st_size
            self.assertGreater(saved,0)
            with mock.patch.object(downloads,"_stream_range",side_effect=resumed):
                downloads.download_verified(["https://x/archive"],target,expected_size=len(payload),
                                            expected_sha256=hashlib.sha256(payload).hexdigest(),attempts_per_source=1)
            self.assertEqual(payload,target.read_bytes())
            self.assertEqual(saved,starts[0])

    def test_second_range_and_content_range_are_strictly_validated(self):
        captured=[]
        class Response:
            status_code=206
            headers={"content-range":"bytes 13-19/100"}
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def raise_for_status(self): return None
            def iter_bytes(self,_size): yield b"3456789"
        class Client:
            def stream(self,_method,_url,**kwargs): captured.append(kwargs["headers"]["Range"]); return Response()
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads.transport,"client",return_value=Client()):
            path=Path(raw)/"archive.zip.part.0"
            path.write_bytes(b"012")
            downloads._stream_range("https://github.com/release",path,10,19,None,expected_total=100)
            self.assertEqual("bytes=13-19",captured[0])
            self.assertEqual(10,path.stat().st_size)
            Response.headers={"content-range":"bytes 12-19/100"}
            path.write_bytes(b"012")
            with self.assertRaises(downloads.RangeProtocolError):
                downloads._stream_range("https://github.com/release",path,10,19,None,expected_total=100)
            self.assertEqual(3,path.stat().st_size)

    def test_github_release_redirect_preserves_range_request(self):
        seen=[]
        payload=b"redirected"
        def handler(request):
            seen.append((str(request.url),request.headers.get("range")))
            if request.url.host == "github.com":
                return httpx.Response(302,headers={"location":"https://objects.githubusercontent.com/archive"})
            return httpx.Response(206,headers={"content-range":f"bytes 0-{len(payload)-1}/{len(payload)}"},
                                  content=payload)
        client=httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True)
        try:
            with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads.transport,"client",return_value=client):
                path=Path(raw)/"archive.zip.part.0"
                downloads._stream_range("https://github.com/release",path,0,len(payload)-1,None,
                                        expected_total=len(payload))
                self.assertEqual(payload,path.read_bytes())
        finally:
            client.close()
        self.assertEqual(["bytes=0-9","bytes=0-9"],[value for _url,value in seen])

    def test_proxy_that_caps_each_range_is_consumed_in_successive_requests(self):
        payload=b"0123456789abcdef"
        ranges=[]
        def handler(request):
            value=request.headers["range"]
            ranges.append(value)
            start,end=map(int,value.removeprefix("bytes=").split("-"))
            returned_end=min(end,start+3)
            return httpx.Response(206,headers={"content-range":f"bytes {start}-{returned_end}/{len(payload)}"},
                                  content=payload[start:returned_end+1])
        client=httpx.Client(transport=httpx.MockTransport(handler))
        try:
            with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads.transport,"client",return_value=client):
                path=Path(raw)/"archive.zip.part.0"
                downloads._stream_range("https://proxy.invalid/archive",path,0,len(payload)-1,None,
                                        expected_total=len(payload))
                self.assertEqual(payload,path.read_bytes())
        finally:
            client.close()
        self.assertEqual(["bytes=0-15","bytes=4-15","bytes=8-15","bytes=12-15"],ranges)

    def test_failed_range_transfer_falls_back_to_probed_whole_file_source(self):
        payload=b"whole-file-fallback" * 1024
        whole_calls=[]
        def probe(url):
            return {"url":url,"range":"proxy" in url,"latency":.01,
                    "throughput":2 if "proxy" in url else 1}
        def ranged(url,_path,_start,_end,_progress,**_kwargs):
            if "proxy" in url:
                raise httpx.ReadError("proxy disconnected",request=httpx.Request("GET",url))
            raise downloads.RangeProtocolError("official endpoint ignores Range")
        def whole(url,path,_expected_size,progress):
            whole_calls.append(url)
            path.write_bytes(payload); progress(len(payload))
        urls=["https://proxy.invalid/archive","https://github.com/release/archive"]
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads,"_probe",side_effect=probe), \
                mock.patch.object(downloads,"_stream_range",side_effect=ranged), \
                mock.patch.object(downloads,"_stream_file",side_effect=whole), \
                mock.patch.object(downloads.time,"sleep"):
            target=Path(raw)/"archive.zip"
            result=downloads.download_verified(urls,target,expected_size=len(payload),
                                               expected_sha256=hashlib.sha256(payload).hexdigest(),
                                               attempts_per_source=1,retry_backoff=0)
            self.assertEqual(payload,target.read_bytes())
            self.assertEqual(["https://github.com/release/archive"],whole_calls)
            self.assertEqual(["https://github.com/release/archive"],result["urls"])
            self.assertEqual([],list(Path(raw).glob("archive.zip.part.*")))

    def test_probe_failures_do_not_prevent_source_fallback(self):
        payload=b"probe-fallback"
        used=[]
        def stream(url,path,_expected_size,progress):
            used.append(url)
            if "proxy" in url:
                raise downloads.RangeProtocolError("proxy response is incompatible")
            path.write_bytes(payload)
            progress(len(payload))
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(downloads,"_probe",side_effect=httpx.ConnectError("probe failed")), \
                mock.patch.object(downloads,"_stream_file",side_effect=stream):
            target=Path(raw)/"archive.zip"
            downloads.download_verified(["https://proxy.invalid/archive","https://github.com/release"],target,
                                        expected_size=len(payload),expected_sha256=hashlib.sha256(payload).hexdigest())
            self.assertEqual(payload,target.read_bytes())
        self.assertEqual(["https://proxy.invalid/archive","https://github.com/release"],used)
