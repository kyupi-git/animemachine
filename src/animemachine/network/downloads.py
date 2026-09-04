"""Verified resumable downloads; Archive-specific policy is layered above."""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable

import httpx

from . import transport


_CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
class RangeProtocolError(RuntimeError):
    """The endpoint did not provide a safe, compatible byte range."""


class IncompleteRangeError(RuntimeError):
    """A valid range response ended before the requested segment completed."""


def _content_range(response: httpx.Response, requested_start: int, requested_end: int,
                   expected_total: int | None = None) -> tuple[int, int]:
    if response.status_code != 206:
        raise RangeProtocolError(f"range request returned HTTP {response.status_code}")
    match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", "").strip())
    if not match:
        raise RangeProtocolError("missing or malformed Content-Range")
    start, end = int(match.group(1)), int(match.group(2))
    if start != requested_start or end < start or end > requested_end:
        raise RangeProtocolError("Content-Range does not match the requested interval")
    total = match.group(3)
    if expected_total is not None and total != "*" and int(total) != expected_total:
        raise RangeProtocolError("Content-Range total size mismatch")
    return start, end


def download(url: str, destination: Path, *, expected_size: int | None = None,
             expected_sha256: str | None = None, timeout: float = 30) -> dict:
    partial = destination.with_suffix(destination.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-", "Accept-Encoding": "identity"} if offset else {"Accept-Encoding": "identity"}
    maximum = int(expected_size) if expected_size is not None else 512 * 1024 * 1024
    final_url = url
    with transport.stream("GET", url, headers=headers, timeout=timeout) as response:
        response.raise_for_status()
        final_url = str(response.url)
        mode = "ab" if offset and response.status_code == 206 else "wb"
        if mode == "wb":
            offset = 0
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and offset + int(declared) > maximum:
            raise ValueError("asset exceeds configured limit")
        received = offset
        with partial.open(mode) as output:
            for chunk in response.iter_bytes(1024 * 1024):
                received += len(chunk)
                if received > maximum:
                    raise ValueError("asset exceeds configured limit")
                output.write(chunk)
    size = partial.stat().st_size
    if expected_size is not None and size != expected_size:
        raise ValueError("asset size mismatch")
    digest = hashlib.sha256()
    with partial.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    sha256 = digest.hexdigest()
    if expected_sha256 and sha256.casefold() != expected_sha256.casefold():
        partial.unlink(missing_ok=True)
        raise ValueError("asset digest mismatch")
    os.replace(partial, destination)
    return {"size": size, "sha256": sha256, "url": final_url}


def _probe(url: str, sample_size: int = 256 * 1024) -> dict:
    started = time.monotonic(); received = 0
    timeout = httpx.Timeout(connect=6, read=8, write=8, pool=6)
    with transport.stream("GET", url, headers={"Range": f"bytes=0-{sample_size-1}", "Accept-Encoding":"identity"}, timeout=timeout) as response:
        response.raise_for_status()
        range_supported = False
        if response.status_code == 206:
            try:
                _content_range(response, 0, sample_size - 1)
                range_supported = True
            except RangeProtocolError:
                pass
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received >= sample_size: break
        elapsed = max(.001, time.monotonic() - started)
        return {"url":url,"range":range_supported,"latency":elapsed,"throughput":received/elapsed}


def _stream_range(url: str, path: Path, start: int, end: int, progress: Callable[[int], None] | None,
                  *, expected_total: int | None = None) -> None:
    existing = path.stat().st_size if path.exists() else 0
    segment_size = end - start + 1
    if existing > segment_size:
        path.unlink()
        existing = 0
    timeout = httpx.Timeout(connect=8, read=45, write=20, pool=8)
    while existing < segment_size:
        cursor = start + existing
        with transport.stream("GET", url, headers={"Range":f"bytes={cursor}-{end}","Accept-Encoding":"identity"}, timeout=timeout) as response:
            response.raise_for_status()
            _returned_start, returned_end = _content_range(response, cursor, end, expected_total)
            expected_response_size = returned_end - cursor + 1
            received = 0
            with path.open("ab") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    remaining = expected_response_size - received
                    if remaining <= 0:
                        break
                    accepted = chunk[:remaining]
                    output.write(accepted)
                    output.flush()
                    received += len(accepted)
                    if progress: progress(len(accepted))
                    if received == expected_response_size:
                        break
            if received != expected_response_size:
                raise IncompleteRangeError("range response ended before Content-Range was fulfilled")
        existing = path.stat().st_size
    if path.stat().st_size != segment_size:
        raise IncompleteRangeError("requested segment remains incomplete")


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, IncompleteRangeError) or transport.is_retryable(exc)


def _stream_file(url: str, path: Path, expected_size: int,
                 progress: Callable[[int], None] | None) -> None:
    """Resume one-file downloads when possible; retain bytes after interruption."""
    existing = path.stat().st_size if path.exists() else 0
    if existing > expected_size:
        path.unlink()
        existing = 0
    timeout=httpx.Timeout(connect=8,read=45,write=20,pool=8)
    while existing < expected_size:
        headers = {"Accept-Encoding":"identity"}
        if existing:
            headers["Range"] = f"bytes={existing}-{expected_size-1}"
        with transport.stream("GET", url, headers=headers, timeout=timeout) as response:
            response.raise_for_status()
            if existing and response.status_code == 206:
                _start, returned_end = _content_range(response, existing, expected_size-1, expected_size)
                mode="ab"; expected_response=returned_end-existing+1
            elif response.status_code == 200:
                if existing and progress: progress(-existing)
                existing=0; mode="wb"; expected_response=expected_size
            elif not existing and response.status_code == 206:
                _start, returned_end = _content_range(response, 0, expected_size-1, expected_size)
                mode="wb"; expected_response=returned_end+1
            else:
                raise RangeProtocolError(f"download returned HTTP {response.status_code}")
            received=0
            with path.open(mode) as output:
                for chunk in response.iter_bytes(1024*1024):
                    remaining=expected_response-received
                    if remaining<=0: break
                    accepted=chunk[:remaining]
                    output.write(accepted); output.flush(); received+=len(accepted)
                    if progress: progress(len(accepted))
                    if received==expected_response: break
            if received!=expected_response:
                raise IncompleteRangeError("file response ended before its declared range was fulfilled")
        existing=path.stat().st_size
    if path.stat().st_size!=expected_size:
        raise IncompleteRangeError("download remains incomplete")


def download_verified(urls: Iterable[str], destination: Path, *, expected_size: int,
                      expected_sha256: str, progress: Callable[[dict], None] | None = None,
                      segments: int = 2, attempts_per_source: int = 3,
                      retry_backoff: float = .5) -> dict:
    """Race small ranges, resume independent segments, verify, then atomically publish."""
    values = list(dict.fromkeys(urls))
    if not values: raise ValueError("no download endpoints")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=min(4,len(values)))
    futures={pool.submit(_probe,url):url for url in values}; probes=[]
    try:
        done,pending=concurrent.futures.wait(futures,timeout=4,return_when=concurrent.futures.FIRST_COMPLETED)
        # Give concurrently healthy mirrors a short ranking window without
        # letting a slow or unreachable endpoint delay the actual transfer.
        if done and pending:
            more,_pending=concurrent.futures.wait(pending,timeout=.25)
            done |= more
        for future in done:
            try: probes.append(future.result())
            except Exception: pass
        if not probes:
            done,_pending=concurrent.futures.wait(futures,timeout=4)
            for future in done:
                try: probes.append(future.result())
                except Exception: pass
    finally:
        pool.shutdown(wait=False,cancel_futures=True)
    probes.sort(key=lambda item:(not item["range"],-item["throughput"],item["latency"]))
    ranked=list(dict.fromkeys([item["url"] for item in probes] + values))
    done=0; started=time.monotonic()
    def report(delta:int):
        nonlocal done
        done += delta
        if progress: progress({"phase":"download","received":done,"total":expected_size,"speed":done/max(.001,time.monotonic()-started)})
    partial=destination.with_suffix(destination.suffix+".part")
    attempts_per_source=max(1,min(8,int(attempts_per_source)))
    # Unprobed endpoints remain failover candidates.  This is important when a
    # nearby proxy answers the probe first but later drops a large transfer:
    # the original GitHub Release URL can still resume the same segment.
    probed_range=[item["url"] for item in probes if item["range"]]
    probed_non_range=[item["url"] for item in probes if not item["range"]]
    range_capable=(list(dict.fromkeys(probed_range + [url for url in values if url not in probed_non_range]))
                   if probed_range else [])
    if range_capable:
        count=max(1,min(4,segments,expected_size//(8*1024*1024) or 1))
        bounds=[]
        for index in range(count):
            start=index*expected_size//count; end=((index+1)*expected_size//count)-1
            bounds.append((start,end,partial.with_suffix(partial.suffix+f".{index}")))
        for start,end,path in bounds:
            if path.exists() and path.stat().st_size>end-start+1:
                path.unlink()
        done=sum(min(path.stat().st_size,end-start+1) for start,end,path in bounds if path.exists())
        def segment_job(item):
            start,end,path=item; errors=[]
            for url in range_capable:
                for attempt in range(attempts_per_source):
                    try:
                        _stream_range(url,path,start,end,report,expected_total=expected_size)
                        return url
                    except RangeProtocolError as exc:
                        errors.append(f"{url}:{type(exc).__name__}")
                        break
                    except Exception as exc:
                        errors.append(f"{url}:{type(exc).__name__}")
                        if not _retryable(exc) or attempt + 1 >= attempts_per_source:
                            break
                        time.sleep(max(0.0,retry_backoff) * (2**attempt))
            raise RuntimeError("range failed: "+";".join(errors))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                used=[future.result() for future in [pool.submit(segment_job,item) for item in bounds]]
        except RuntimeError:
            if not probed_non_range:
                raise
            errors=[]; used=[]
            for url in probed_non_range:
                for attempt in range(attempts_per_source):
                    try:
                        done=partial.stat().st_size if partial.exists() else 0
                        _stream_file(url,partial,expected_size,report)
                        used=[url]
                        break
                    except RangeProtocolError as exc:
                        errors.append(f"{url}:{type(exc).__name__}")
                        break
                    except Exception as exc:
                        errors.append(f"{url}:{type(exc).__name__}")
                        if not _retryable(exc) or attempt+1>=attempts_per_source: break
                        time.sleep(max(0.0,retry_backoff)*(2**attempt))
                if partial.exists() and partial.stat().st_size==expected_size:
                    break
            else:
                raise RuntimeError("all download streams failed: "+";".join(errors))
        else:
            with partial.open("wb") as output:
                for _start,_end,path in bounds:
                    with path.open("rb") as source:
                        while chunk:=source.read(4*1024*1024): output.write(chunk)
                    path.unlink(missing_ok=True)
    else:
        errors=[]
        for url in ranked:
            for attempt in range(attempts_per_source):
                try:
                    done=partial.stat().st_size if partial.exists() else 0
                    _stream_file(url,partial,expected_size,report)
                    break
                except RangeProtocolError as exc:
                    errors.append(f"{url}:{type(exc).__name__}")
                    break
                except Exception as exc:
                    errors.append(f"{url}:{type(exc).__name__}")
                    if not _retryable(exc) or attempt+1>=attempts_per_source: break
                    time.sleep(max(0.0,retry_backoff)*(2**attempt))
            if partial.exists() and partial.stat().st_size==expected_size:
                break
        else: raise RuntimeError("all download streams failed: "+";".join(errors))
        used=[url]
    if partial.stat().st_size != expected_size: raise ValueError("asset size mismatch")
    digest=hashlib.sha256()
    with partial.open("rb") as stream:
        while chunk:=stream.read(4*1024*1024): digest.update(chunk)
    actual=digest.hexdigest()
    if actual.casefold()!=expected_sha256.casefold():
        partial.unlink(missing_ok=True)
        raise ValueError("asset digest mismatch")
    os.replace(partial,destination)
    for path in destination.parent.glob(partial.name+".*"):
        path.unlink(missing_ok=True)
    return {"size":expected_size,"sha256":actual,"urls":list(dict.fromkeys(used))}
