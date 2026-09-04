"""Torrent Collector service: source discovery, evidence filtering, and safe persistence."""
from __future__ import annotations

import gzip
import httpx
import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import re
import socket
import ssl
import struct
import time
import urllib.parse
import zlib
from html import unescape
from html.parser import HTMLParser
from typing import Any

from .collector_filter import (
    ARCHIVE_GROUPS as ARCHIVE_GROUP_POLICY,
    SERIAL_RULES as SERIAL_GROUP_POLICY,
    CatalogMatcher,
    FILTER_RULESET_ID,
    SEARCH_RULESET_ID,
    classify_title,
    decide_final,
    decide_title,
    parse_release_title,
    catalog_path_from_env,
)
from .collector_state import CollectorState, namespaced_result_key
from .metainfo import MAX_TORRENT_BYTES, inspect_bytes, read_torrent_file
from ..network import tls as network_tls, transport as network_transport


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().casefold() in {"1", "true", "yes", "on"}


OUT = Path(os.getenv("ANM_TORRENT_POOL_DIR", "/Torrents"))
STATE = Path(os.getenv("TORRENT_COLLECTOR_STATE_DIR", "/Data/state/torrent-collector"))
DB_PATH = Path(os.getenv("TORRENT_COLLECTOR_DB", str(STATE / "collector.sqlite3")))
QUARANTINE = Path(os.getenv("TORRENT_COLLECTOR_QUARANTINE_DIR", str(STATE / "quarantine")))
AUDIT_MODE = os.getenv("TORRENT_COLLECTOR_EXISTING_AUDIT_MODE", "report").strip().casefold()
POLL = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", os.getenv("TORRENT_COLLECTOR_POLL_INTERVAL_SECONDS", "3000"))))
PAGE_DELAY = max(0.0, float(os.getenv("HISTORY_PAGE_DELAY_SECONDS", "1.5")))
TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60")))
OUT_UID = int(os.getenv("PUID", os.getenv("OUTPUT_UID", "1000")))
OUT_GID = int(os.getenv("PGID", os.getenv("OUTPUT_GID", "1000")))
NATIVE_HISTORY_ENABLED = _env_bool("NATIVE_HISTORY_ENABLED", "true")
NATIVE_FULL_HISTORY_ENABLED = _env_bool("NATIVE_FULL_HISTORY_ENABLED", "true")
NATIVE_INCREMENTAL_ENABLED = _env_bool("NATIVE_INCREMENTAL_ENABLED", "true")
NATIVE_INCREMENTAL_PAGES = max(1, int(os.getenv("NATIVE_INCREMENTAL_PAGES", "2")))
HISTORY_PAGES_PER_JOB_PER_CYCLE = max(1, int(os.getenv("HISTORY_PAGES_PER_JOB_PER_CYCLE", "5")))
HISTORY_JOBS_PER_CYCLE = max(1, int(os.getenv("HISTORY_JOBS_PER_CYCLE", "10")))
FULL_HISTORY_PAGES_PER_SOURCE_PER_CYCLE = max(1, int(os.getenv("FULL_HISTORY_PAGES_PER_SOURCE_PER_CYCLE", "10")))
HISTORY_RETRY_SECONDS = max(60, int(os.getenv("HISTORY_RETRY_SECONDS", "60")))
RETRY_BATCH_SIZE = max(1, int(os.getenv("RETRY_BATCH_SIZE", "100")))
REEVALUATION_BATCH_SIZE = 500
HTTP_MAX_RETRIES = max(1, int(os.getenv("HTTP_MAX_RETRIES", "5")))
MAX_RETRY_ATTEMPTS = max(1, int(os.getenv("TORRENT_COLLECTOR_MAX_RETRY_ATTEMPTS", "6")))
HTML_MAX_BYTES = max(1024 * 1024, int(os.getenv("TORRENT_COLLECTOR_HTML_MAX_BYTES", str(8 * 1024 * 1024))))
TORRENT_MAX_BYTES = max(1024 * 1024, int(os.getenv("TORRENT_COLLECTOR_TORRENT_MAX_BYTES", str(MAX_TORRENT_BYTES))))
PROXY_ENABLED = _env_bool("NATIVE_PROXY_ENABLED", "false")
PROXY_HOST = os.getenv("NATIVE_PROXY_HOST", "host.docker.internal").strip()
PROXY_PORT = int(os.getenv("NATIVE_PROXY_PORT", "1080"))

ARCHIVE_GROUPS = {
    str(item.get("name")): tuple(dict.fromkeys([str(item.get("name")), *map(str, item.get("aliases", []))]))
    for item in ARCHIVE_GROUP_POLICY
}
SERIAL_RULES = {
    str(item[1]): {"groups": tuple(map(str, item[2])), "subs": tuple(map(str, item[3]))}
    for item in SERIAL_GROUP_POLICY
}
SEPARATOR_RE = re.compile(r"[ ._-]+")


def group_aliases(group: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([group, *aliases]))


def separator_variants(name: str) -> set[str]:
    parts = [part for part in SEPARATOR_RE.split(name) if part]
    if len(parts) <= 1:
        return {name}
    variants = {parts[0]}
    for part in parts[1:]:
        variants = {prefix + sep + part for prefix in variants for sep in (" ", ".", "_", "-")}
    return variants


def build_search_terms() -> list[str]:
    aliases: list[str] = []
    for group, items in ARCHIVE_GROUPS.items():
        aliases.extend(group_aliases(group, items))
    for group, rule in SERIAL_RULES.items():
        aliases.extend(group_aliases(group, rule["groups"]))
    terms: dict[str, str] = {}
    for alias in aliases:
        for variant in separator_variants(alias):
            value = variant.strip()
            if value:
                terms.setdefault(value.casefold(), value)
    return sorted(terms.values(), key=lambda item: (item.casefold(), item))


SEARCH_TERMS = build_search_terms()
PRIMARY_SEARCH_TERMS = sorted(
    {value for group, items in ARCHIVE_GROUPS.items() for value in group_aliases(group, items)}
    | {value for group, rule in SERIAL_RULES.items() for value in group_aliases(group, rule["groups"])},
    key=lambda item: (item.casefold(), item),
)
NATIVE_JOBSET = f"native-v6:{SEARCH_RULESET_ID}"
state: CollectorState | None = None
db = None
catalog_matcher = CatalogMatcher(catalog_path_from_env())
_cycle_stats: dict[str, int] = {}


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def _bump(name: str, amount: int = 1) -> None:
    _cycle_stats[name] = _cycle_stats.get(name, 0) + amount


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._current = None
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            data = dict(attrs)
            self._current = {"href": data.get("href", ""), "class": data.get("class", ""), "rel": data.get("rel", ""), "type": data.get("type", ""), "text": []}
    def handle_data(self, data):
        if self._current is not None:
            self._current["text"].append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current is not None:
            self._current["text"] = "".join(self._current["text"]).strip()
            self.links.append(self._current)
            self._current = None


def anchors(fragment):
    p = LinkParser()
    try:
        p.feed(fragment)
    except Exception:
        pass
    return p.links


def rows(html_text):
    return re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", html_text)


def absolute(base, href):
    return urllib.parse.urljoin(base, unescape(href or ""))


def query_page_from_href(href):
    href = unescape(href or "")
    try:
        u = urllib.parse.urlsplit(href)
        q = urllib.parse.parse_qs(u.query)
        for key in ("p", "page"):
            if key in q and q[key]:
                return int(q[key][0])
        m = re.search(r"/page/(\d+)(?:/|$|\?)", u.path + ("?" + u.query if u.query else ""))
        if m:
            return int(m.group(1))
        m = re.search(r"/Home/Classic/(\d+)(?:/|$)", u.path)
        if m:
            return int(m.group(1))
    except Exception:
        return None
    return None


def html_has_page(html_text, target):
    return any(query_page_from_href(a.get("href")) == target for a in anchors(html_text))


def item_signature(items):
    material = "\n".join(str(x.get("id") or x.get("download_url") or x.get("details_url") or x.get("title")) for x in items)
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


class HttpStatusError(Exception):
    def __init__(self, status, url, body=b""):
        super().__init__(f"HTTP {status}: {url}")
        self.status = status
        self.url = url
        self.body = body


def recv_exact(sock, n):
    data = bytearray()
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise OSError("unexpected EOF from SOCKS5 proxy")
        data.extend(part)
    return bytes(data)


def socks5_socket(host, port, timeout):
    sock = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=timeout)
    sock.settimeout(timeout)
    sock.sendall(b"\x05\x01\x00")
    if recv_exact(sock, 2) != b"\x05\x00":
        sock.close()
        raise OSError("SOCKS5 proxy does not allow no-authentication mode")
    host_bytes = host.encode("idna")
    if len(host_bytes) > 255:
        sock.close()
        raise OSError("SOCKS5 hostname is too long")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack("!H", int(port)))
    head = recv_exact(sock, 4)
    if head[0] != 5 or head[1] != 0:
        sock.close()
        raise OSError(f"SOCKS5 connect failed, reply={head[1] if len(head) > 1 else 'unknown'}")
    atyp = head[3]
    if atyp == 1:
        recv_exact(sock, 4)
    elif atyp == 3:
        recv_exact(sock, recv_exact(sock, 1)[0])
    elif atyp == 4:
        recv_exact(sock, 16)
    else:
        sock.close()
        raise OSError("SOCKS5 invalid address type")
    recv_exact(sock, 2)
    return sock


class SocksHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socks5_socket(self.host, self.port or 80, self.timeout)


class SocksHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = socks5_socket(self.host, self.port or 443, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


COOKIE_JAR = {}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"


def cookie_header(host):
    values = COOKIE_JAR.get(host, {})
    return "; ".join(f"{k}={v}" for k, v in values.items())


def store_cookies(host, headers):
    values = COOKIE_JAR.setdefault(host, {})
    getter = getattr(headers, "get_all", None)
    raw_values = getter("Set-Cookie") if callable(getter) else None
    if raw_values is None:
        getter = getattr(headers, "get_list", None)
        raw_values = getter("set-cookie") if callable(getter) else []
    for raw in raw_values or []:
        first = raw.split(";", 1)[0]
        if "=" in first:
            name, value = first.split("=", 1)
            if name.strip():
                values[name.strip()] = value.strip()


def _read_response_limited(resp: http.client.HTTPResponse, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"response too large (>{max_bytes} bytes)")
    return b"".join(chunks)


def _decompress_limited(raw: bytes, encoding: str, max_bytes: int) -> bytes:
    if encoding not in {"gzip", "deflate"}:
        return raw
    wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
    obj = zlib.decompressobj(wbits)
    result = obj.decompress(raw, max_bytes + 1)
    if len(result) > max_bytes or obj.unconsumed_tail:
        raise ValueError(f"decompressed response too large (>{max_bytes} bytes)")
    tail = obj.flush(max_bytes + 1 - len(result))
    result += tail
    if len(result) > max_bytes:
        raise ValueError(f"decompressed response too large (>{max_bytes} bytes)")
    return result


def _native_http_request_once(url, method="GET", data=None, headers=None, timeout=TIMEOUT, redirects=5, max_bytes=HTML_MAX_BYTES):
    current = url
    body = data
    method_now = method.upper()
    for _ in range(redirects + 1):
        u = urllib.parse.urlsplit(current)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"unsupported URL: {current}")
        port = u.port or (443 if u.scheme == "https" else 80)
        if PROXY_ENABLED:
            cls = SocksHTTPSConnection if u.scheme == "https" else SocksHTTPConnection
        else:
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": timeout}
        if u.scheme == "https":
            kwargs["context"] = network_tls.ssl_context()
        conn = cls(u.hostname, port, **kwargs)
        path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, ""))
        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        ck = cookie_header(u.hostname)
        if ck:
            req_headers["Cookie"] = ck
        if headers:
            req_headers.update(headers)
        try:
            conn.request(method_now, path, body=body, headers=req_headers)
            resp = conn.getresponse()
            raw = _read_response_limited(resp, max_bytes)
            store_cookies(u.hostname, resp.headers)
            encoding = (resp.getheader("Content-Encoding") or "").lower()
            raw = _decompress_limited(raw, encoding, max_bytes)
            status = resp.status
            location = resp.getheader("Location")
            content_type = resp.getheader("Content-Type") or ""
        finally:
            conn.close()
        if status in (301, 302, 303, 307, 308) and location:
            current = urllib.parse.urljoin(current, location)
            if status == 303 or (status in (301, 302) and method_now == "POST"):
                method_now = "GET"
                body = None
            continue
        if status >= 400:
            raise HttpStatusError(status, current, raw[:4096])
        return raw, content_type, current
    raise RuntimeError(f"too many redirects: {url}")


def _shared_http_request_once(url, method="GET", data=None, headers=None, timeout=TIMEOUT, max_bytes=HTML_MAX_BYTES):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"unsupported URL: {url}")
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
        "Accept-Encoding": "identity",
    }
    ck = cookie_header(u.hostname)
    if ck:
        req_headers["Cookie"] = ck
    if headers:
        req_headers.update(headers)
    try:
        response = network_transport.request(
            method.upper(), url, headers=req_headers, content=data, timeout=timeout, max_bytes=max_bytes)
    except httpx.HTTPStatusError as exc:
        raise HttpStatusError(int(exc.response.status_code), str(exc.response.url)) from None
    store_cookies(urllib.parse.urlsplit(str(response.url)).hostname or u.hostname, response.headers)
    return response.content, response.headers.get("Content-Type", ""), str(response.url)


def http_request_once(url, method="GET", data=None, headers=None, timeout=TIMEOUT, redirects=5, max_bytes=HTML_MAX_BYTES):
    if not PROXY_ENABLED:
        return _shared_http_request_once(url, method=method, data=data, headers=headers, timeout=timeout, max_bytes=max_bytes)
    try:
        return _native_http_request_once(url, method=method, data=data, headers=headers, timeout=timeout,
                                         redirects=redirects, max_bytes=max_bytes)
    except HttpStatusError as exc:
        if exc.status not in {408, 425, 429, 500, 502, 503, 504}:
            raise
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError):
        pass
    return _shared_http_request_once(url, method=method, data=data, headers=headers, timeout=timeout, max_bytes=max_bytes)


def http_request(url, method="GET", data=None, headers=None, timeout=TIMEOUT, redirects=5, max_bytes=HTML_MAX_BYTES):
    retry_status = {408, 425, 429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(HTTP_MAX_RETRIES):
        try:
            raw, ctype, final = http_request_once(url, method=method, data=data, headers=headers, timeout=timeout, redirects=redirects, max_bytes=max_bytes)
            probe = raw[:8192].lower()
            if b"too frequently" in probe or "访问频率".encode("utf-8") in raw[:8192]:
                raise HttpStatusError(429, final, raw[:4096])
            if (ctype or "").lower().startswith("application/json") and b'"success":false' in probe and (b"frequent" in probe or b"rate" in probe):
                raise HttpStatusError(429, final, raw[:4096])
            return raw, ctype, final
        except HttpStatusError as error:
            if error.status not in retry_status:
                raise
            last_error = error
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as error:
            last_error = error
        if attempt + 1 < HTTP_MAX_RETRIES:
            delay = min(120.0, 2.0 * (2 ** attempt) + random.random())
            log(f"WARN HTTP retry {attempt + 1}/{HTTP_MAX_RETRIES} {url}: {last_error}; sleep {delay:.1f}s")
            time.sleep(delay)
    raise last_error if last_error else RuntimeError(f"request failed: {url}")


def decode_text(raw, content_type=""):
    m = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", re.I)
    encodings = [m.group(1)] if m else []
    encodings += ["utf-8", "gb18030", "big5"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", "replace")


def get_text(url, headers=None):
    raw, ctype, final = http_request(url, headers=headers)
    return decode_text(raw, ctype), final


def get_json(url, method="GET", data=None, headers=None):
    raw, _ctype, _final = http_request(url, method=method, data=data, headers=headers)
    return json.loads(raw.decode("utf-8"))


def parse_nekobt_payload(data):
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError(str(data.get("message") if isinstance(data, dict) else "invalid nekoBT response"))
    payload = data.get("data") or {}
    results = payload.get("results") or []
    items = []
    for r in results:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("id") or "").strip()
        title = str(r.get("title") or "").strip()
        if not tid or not title or r.get("deleted") or r.get("hidden") or r.get("waiting_approve"):
            continue
        qid = urllib.parse.quote(tid, safe="")
        items.append({
            "id": "nekobt:" + tid,
            "title": title,
            "details_url": f"https://nekobt.to/api/v1/torrents/{qid}",
            "download_url": f"https://nekobt.to/api/v1/torrents/{qid}/download?public=true",
        })
    return items, bool(payload.get("more"))


def fetch_nekobt(term, page, sort_by="latest"):
    params = {"limit": 100, "offset": (page - 1) * 100, "sort_by": sort_by}
    if term:
        params["query"] = term
    url = "https://nekobt.to/api/v1/torrents/search?" + urllib.parse.urlencode(params)
    return parse_nekobt_payload(get_json(url, headers={"Accept": "application/json"}))


def parse_tokyotosho_html(text):
    items = []
    for row in rows(text):
        if not re.search(r'\bcategory_0\b', row, re.I):
            continue
        aa = anchors(row)
        downloads = [a for a in aa if (a.get("type") or "").lower() == "application/x-bittorrent" and a.get("href")]
        if not downloads:
            continue
        details = [a for a in aa if re.search(r'(?:^|/)details\.php\?id=\d+', a.get("href") or "", re.I)]
        d = downloads[0]
        title = str(d.get("text") or "").strip()
        if not title:
            continue
        details_url = absolute("https://www.tokyotosho.info/", details[0]["href"]) if details else ""
        key = details_url or absolute("https://www.tokyotosho.info/", d["href"])
        items.append({
            "id": "tokyotosho:" + key,
            "title": title,
            "details_url": details_url,
            "download_url": absolute("https://www.tokyotosho.info/", d["href"]),
        })
    return items


def fetch_tokyotosho(term, page):
    if term:
        params = {"terms": term, "cat": "0", "page": str(page)}
        url = "https://www.tokyotosho.info/search.php?" + urllib.parse.urlencode(params)
    else:
        params = {"page": str(page)}
        url = "https://www.tokyotosho.info/index.php?" + urllib.parse.urlencode(params)
    text, _ = get_text(url)
    return parse_tokyotosho_html(text), html_has_page(text, page + 1)


def fetch_nyaa(term, page, order="desc"):
    params = {"f": "0", "c": "0_0", "q": term, "s": "id", "o": order, "p": str(page)}
    url = "https://nyaa.si/?" + urllib.parse.urlencode(params)
    text, _ = get_text(url)
    items = []
    for row in rows(text):
        aa = anchors(row)
        details = [a for a in aa if re.match(r"^/view/\d+", a["href"])]
        downloads = [a for a in aa if re.match(r"^/download/\d+\.torrent(?:\?|$)", a["href"])]
        if not details or not downloads:
            continue
        a = details[-1]
        href = a["href"]
        items.append({
            "id": "nyaa:" + href,
            "title": a["text"],
            "details_url": absolute("https://nyaa.si/", href),
            "download_url": absolute("https://nyaa.si/", downloads[0]["href"]),
        })
    return items, html_has_page(text, page + 1)


def fetch_acgrip(term, page):
    params = {"term": term, "page": str(page)}
    url = "https://acg.rip/?" + urllib.parse.urlencode(params)
    text, _ = get_text(url)
    items = []
    for row in rows(text):
        aa = anchors(row)
        details = [a for a in aa if re.match(r"^/t/\d+(?:$|[/?#])", a["href"]) and a["text"]]
        downloads = [a for a in aa if ".torrent" in a["href"].lower()]
        if not details or not downloads:
            continue
        a = details[0]
        items.append({
            "id": "acgrip:" + a["href"],
            "title": a["text"],
            "details_url": absolute("https://acg.rip/", a["href"]),
            "download_url": absolute("https://acg.rip/", downloads[0]["href"]),
        })
    return items, html_has_page(text, page + 1)


def fetch_mikan(term, page):
    if term:
        params = {"searchstr": term, "page": str(page)}
        url = "https://mikanani.me/Home/Search?" + urllib.parse.urlencode(params)
    else:
        url = "https://mikanani.me/Home/Classic" if page == 1 else f"https://mikanani.me/Home/Classic/{page}"
    text, _ = get_text(url)
    items = []
    for row in rows(text):
        aa = anchors(row)
        details = [a for a in aa if a["href"].startswith("/Home/Episode/") and a["text"]]
        downloads = [a for a in aa if a["href"].startswith("/Download/")]
        if not details or not downloads:
            continue
        a = details[0]
        items.append({
            "id": "mikan:" + a["href"],
            "title": a["text"],
            "details_url": absolute("https://mikanani.me/", a["href"]),
            "download_url": absolute("https://mikanani.me/", downloads[0]["href"]),
        })
    return items, html_has_page(text, page + 1)


def fetch_dmhy(term, page):
    params = {"keyword": term, "sort_id": "0", "team_id": "0", "order": "date-desc"}
    url = f"https://share.dmhy.org/topics/list/page/{page}?" + urllib.parse.urlencode(params)
    text, _ = get_text(url)
    items = []
    for row in rows(text):
        aa = anchors(row)
        details = [a for a in aa if re.match(r"^/topics/view/\d+\.html", a["href"]) and a["text"]]
        if not details:
            continue
        a = details[0]
        items.append({
            "id": "dmhy:" + a["href"],
            "title": a["text"],
            "details_url": absolute("https://share.dmhy.org/", a["href"]),
            "download_url": "",
        })
    return items, html_has_page(text, page + 1)


def fetch_bangumi(term, page):
    if term:
        url = "https://bangumi.moe/api/v2/torrent/search"
        data = get_json(
            url,
            method="POST",
            data=urllib.parse.urlencode({"query": term, "p": page}).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    else:
        url = f"https://bangumi.moe/api/torrent/page/{page}"
        data = get_json(url, headers={"Accept": "application/json"})
    if isinstance(data, dict) and data.get("success") is False:
        message = str(data.get("message") or "Bangumi API returned success=false")
        if "frequent" in message.lower() or "rate" in message.lower() or "频繁" in message:
            raise HttpStatusError(429, url, message.encode("utf-8", "replace"))
        raise ValueError(message)
    torrents = data.get("torrents") if isinstance(data, dict) else None
    if not isinstance(torrents, list):
        torrents = []
    items = []
    for r in torrents:
        tid = str(r.get("_id") or "").strip()
        title = str(r.get("title") or "").strip()
        if not tid or not title:
            continue
        items.append({
            "id": "bangumi:" + tid,
            "title": title,
            "details_url": f"https://bangumi.moe/torrent/{tid}",
            "download_url": f"https://bangumi.moe/download/torrent/{tid}/{tid}.torrent",
        })
    page_count = int(data.get("page_count") or 0) if isinstance(data, dict) else 0
    has_next = page < page_count if page_count > 0 else bool(items)
    return items, has_next


def fetch_nyaa_full(page):
    params = {"f": "0", "c": "1_0", "q": "", "s": "id", "o": "desc", "p": str(page)}
    url = "https://nyaa.si/?" + urllib.parse.urlencode(params)
    text, _ = get_text(url)
    items = []
    for row in rows(text):
        aa = anchors(row)
        details = [a for a in aa if re.match(r"^/view/\d+", a["href"])]
        downloads = [a for a in aa if re.match(r"^/download/\d+\.torrent(?:\?|$)", a["href"])]
        if not details or not downloads:
            continue
        a = details[-1]
        items.append({"id": "nyaa:" + a["href"], "title": a["text"], "details_url": absolute("https://nyaa.si/", a["href"]), "download_url": absolute("https://nyaa.si/", downloads[0]["href"])})
    return items, html_has_page(text, page + 1)


def fetch_acgrip_full(page):
    url = "https://acg.rip/" if page == 1 else f"https://acg.rip/page/{page}"
    text, _ = get_text(url)
    items = []
    for row in rows(text):
        aa = anchors(row)
        details = [a for a in aa if re.match(r"^/t/\d+(?:$|[/?#])", a["href"]) and a["text"]]
        downloads = [a for a in aa if ".torrent" in a["href"].lower()]
        if not details or not downloads:
            continue
        a = details[0]
        items.append({"id": "acgrip:" + a["href"], "title": a["text"], "details_url": absolute("https://acg.rip/", a["href"]), "download_url": absolute("https://acg.rip/", downloads[0]["href"])})
    return items, html_has_page(text, page + 1)


def fetch_mikan_full(page):
    return fetch_mikan("", page)


def fetch_dmhy_full(page):
    return fetch_dmhy("", page)


def fetch_bangumi_full(page):
    return fetch_bangumi("", page)


def fetch_nekobt_full(page):
    return fetch_nekobt("", page, "oldest")


FULL_HISTORY_FETCHERS = {
    "nyaa": fetch_nyaa_full,
    "acgrip": fetch_acgrip_full,
    "mikan": fetch_mikan_full,
    "dmhy": fetch_dmhy_full,
    "bangumi": fetch_bangumi_full,
    "nekobt": fetch_nekobt_full,
    "tokyotosho": lambda page: fetch_tokyotosho("", page),
}


HISTORY_FETCHERS = {
    "nyaa-desc": lambda term, page: fetch_nyaa(term, page, "desc"),
    "nyaa-asc": lambda term, page: fetch_nyaa(term, page, "asc"),
    "acgrip": fetch_acgrip,
    "mikan": fetch_mikan,
    "dmhy": fetch_dmhy,
    "bangumi": fetch_bangumi,
    "nekobt-desc": lambda term, page: fetch_nekobt(term, page, "latest"),
    "nekobt-asc": lambda term, page: fetch_nekobt(term, page, "oldest"),
    "tokyotosho": fetch_tokyotosho,
}
INCREMENTAL_FETCHERS = {
    "nyaa": lambda term, page: fetch_nyaa(term, page, "desc"),
    "acgrip": fetch_acgrip,
    "mikan": fetch_mikan,
    "dmhy": fetch_dmhy,
    "bangumi": fetch_bangumi,
    "nekobt": lambda term, page: fetch_nekobt(term, page, "latest"),
    "tokyotosho": fetch_tokyotosho,
}
SOURCE_DELAY = {"nyaa": 2.0, "nyaa-desc": 2.0, "nyaa-asc": 2.0, "acgrip": 1.0, "mikan": 30.0, "dmhy": 1.5, "bangumi": 4.0, "nekobt": 2.0, "nekobt-desc": 2.0, "nekobt-asc": 2.0, "tokyotosho": 2.0}




def _require_state() -> CollectorState:
    if state is None:
        raise RuntimeError("collector state is not initialized")
    return state


def native_job_get(source: str, term: str) -> tuple[int, bool, bool]:
    return _require_state().native_job_get(NATIVE_JOBSET, source, term)


def native_job_update(source: str, term: str, next_page: int, expected_more: bool, *, done: bool) -> None:
    _require_state().native_job_update(NATIVE_JOBSET, source, term, next_page, expected_more, done=done)


def signature_seen(source: str, term: str, signature: str):
    return _require_state().signature_seen(NATIVE_JOBSET, source, term, signature)


def remember_signature(source: str, term: str, page: int, signature: str) -> None:
    _require_state().remember_signature(NATIVE_JOBSET, source, term, page, signature)


def _retryability(error: Exception) -> tuple[bool, str]:
    if isinstance(error, HttpStatusError):
        if error.status in {404, 410}:
            return False, f"terminal_http_{error.status}"
        return error.status in {408, 425, 429, 500, 502, 503, 504}, f"http_{error.status}"
    if isinstance(error, ValueError):
        text = str(error).casefold()
        if any(token in text for token in ("no http .torrent url", "invalid", "bencode", "no info", "too large", "html")):
            return False, "terminal_content"
    if isinstance(error, (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError)):
        return True, "network_transient"
    return True, type(error).__name__.casefold()


def resolve_dmhy_download(details_url: str) -> str:
    text, _ = get_text(details_url)
    for anchor in anchors(text):
        href = unescape(anchor.get("href", ""))
        if ".torrent" in href.lower() and ("dl.dmhy.org" in href.lower() or href.lower().endswith(".torrent")):
            return absolute(details_url, href)
    raise ValueError("DMHY detail page has no .torrent link")


def download_native(item: dict[str, Any], source: str) -> tuple[bytes, str]:
    url = str(item.get("download_url") or "")
    if source == "dmhy" and not url:
        url = resolve_dmhy_download(str(item.get("details_url") or ""))
    if not url or url.lower().startswith("magnet:"):
        raise ValueError("no HTTP .torrent URL")
    raw, content_type, final = http_request(
        url,
        headers={"Accept": "application/x-bittorrent,application/octet-stream,*/*;q=0.5"},
        max_bytes=TORRENT_MAX_BYTES,
    )
    probe = raw[:512].lstrip().lower()
    if probe.startswith((b"<html", b"<!doctype", b"{")):
        raise ValueError("HTML/JSON response is not a torrent")
    if "text/html" in (content_type or "").casefold() and not raw.startswith((b"d", b"l")):
        raise ValueError("HTML response is not a torrent")
    return raw, final


def process_native_item(item: dict[str, Any], source: str, *, force: bool = False) -> int:
    store = _require_state()
    key = store.record_discovery(source, item)
    existing = store.get_discovery(key)
    if not force and store.result_complete(key):
        return 0
    title = str(item.get("title") or "")
    evidence = parse_release_title(title)
    catalog = catalog_matcher.match(title, evidence)
    title_decision = decide_title(evidence, catalog)
    _bump("discovered")
    if title_decision.decision == "reject":
        store.record_decision(key, title_decision)
        _bump("title_hard_reject")
        _bump(title_decision.reason)
        return 0
    if title_decision.decision == "defer":
        _bump("deferred")
    try:
        saved_name = str(existing["saved_filename"] or "") if existing is not None else ""
        saved_path = store.out_dir / saved_name if saved_name else None
        if saved_path is not None and saved_path.is_file():
            raw = read_torrent_file(saved_path)
            _bump("local_torrent_reused")
        else:
            raw, resolved = download_native(item, source)
            item["download_url"] = resolved
            _bump("torrent_downloaded")
        metadata = inspect_bytes(raw, filename=title, include_files=True)
        final = decide_final(evidence, metadata.get("manifestSummary"), catalog)
        if final.decision == "reject":
            store.record_decision(key, final, metadata=metadata)
            _bump("manifest_reject")
            _bump(final.reason)
            return 0
        if final.decision == "defer":
            store.record_decision(key, final, metadata=metadata)
            _bump("deferred")
            return 0
        saved, filename = store.atomic_save(raw, title, source, key, metadata)
        store.record_decision(key, final, metadata=metadata, saved_filename=filename, collector_owned=True if saved else None)
        _bump("saved", int(saved))
        if final.reason == "movie_main_media":
            _bump("movie_accept")
        elif final.reason.startswith("catalog_"):
            _bump("catalog_accept")
        elif final.reason.startswith("explicit_"):
            _bump("explicit_accept")
        if not saved:
            _bump("duplicate")
        if saved:
            log(f"SAVED {filename}")
        return int(saved)
    except Exception as exc:
        retryable, error_class = _retryability(exc)
        status = store.queue_retry(source, item, key, str(exc), retryable=retryable, error_class=error_class)
        _bump(status)
        log(f"WARN {status} [{source}] {title}: {exc}")
        return 0


def run_discovery_reevaluation() -> int:
    store = _require_state()
    generation = catalog_matcher.generation()
    rows = store.discoveries_needing_reevaluation(generation, REEVALUATION_BATCH_SIZE)
    saved = 0
    for row in rows:
        try:
            payload = json.loads(str(row["discovery_json"] or "{}"))
            item = payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            item = {}
        item = dict(item)
        item["id"] = str(row["result_key"])
        item.setdefault("title", str(row["original_title"] or ""))
        item.setdefault("details_url", str(row["details_url"] or ""))
        item.setdefault("download_url", str(row["download_url"] or ""))
        saved += process_native_item(item, str(row["source"]), force=True)
        _bump("reevaluated")
    return saved


def run_retry_queue() -> int:
    store = _require_state()
    saved = 0
    for row in store.due_retries(RETRY_BATCH_SIZE):
        item = {
            "id": str(row["result_key"]).split(":", 1)[-1],
            "title": row["title"],
            "details_url": row["details_url"],
            "download_url": row["download_url"],
        }
        saved += process_native_item(item, str(row["source"]), force=True)
    return saved


def _audit_classifier(title: str, metadata: dict[str, Any]):
    evidence = parse_release_title(title)
    catalog = catalog_matcher.match(title, evidence)
    return decide_final(evidence, metadata.get("manifestSummary"), catalog)


def source_sleep(source):
    delay = max(PAGE_DELAY, SOURCE_DELAY.get(source, PAGE_DELAY))
    if delay:
        time.sleep(delay)


def crawl_history_term(source, term, page_budget=HISTORY_PAGES_PER_JOB_PER_CYCLE):
    fetcher = HISTORY_FETCHERS[source]
    page, expected_more, done = native_job_get(source, term)
    if done:
        return 0, True
    saved = 0
    pages_done = 0
    log(f"HISTORY {source}: term={term!r}, resume page={page}")
    while pages_done < page_budget:
        try:
            items, has_next = fetcher(term, page)
        except HttpStatusError as e:
            if e.status == 404 and not expected_more:
                native_job_update(source, term, page, False, done=True)
                log(f"HISTORY DONE {source}: term={term!r}, terminal HTTP 404 at page {page}")
                return saved, True
            log(f"WARN history fetch failed [{source}] term={term!r} page={page}: {e}")
            return saved, False
        except Exception as e:
            log(f"WARN history fetch failed [{source}] term={term!r} page={page}: {e}")
            return saved, False
        if not items:
            if page == 1 or not expected_more:
                native_job_update(source, term, page, False, done=True)
                log(f"HISTORY DONE {source}: term={term!r}, empty page={page}")
                return saved, True
            log(f"WARN {source} advertised another page but page {page} is empty; checkpoint held")
            return saved, False
        sig = item_signature(items)
        prior = signature_seen(source, term, sig)
        if prior is not None and int(prior[0]) != page:
            if not expected_more:
                native_job_update(source, term, page, False, done=True)
                log(f"HISTORY DONE {source}: term={term!r}, terminal page repeated page {prior[0]}")
                return saved, True
            log(f"WARN {source} pagination loop: page {page} repeats page {prior[0]}; checkpoint held")
            return saved, False
        for item in items:
            saved += process_native_item(item, source.replace("nyaa-desc", "nyaa").replace("nyaa-asc", "nyaa").replace("nekobt-desc", "nekobt").replace("nekobt-asc", "nekobt"))
        remember_signature(source, term, page, sig)
        next_page = page + 1
        native_job_update(source, term, next_page, has_next, done=False)
        expected_more = has_next
        page = next_page
        pages_done += 1
        source_sleep(source)
    return saved, True


def run_native_history():
    if not NATIVE_HISTORY_ENABLED:
        return 0
    saved = 0
    jobs = 0
    for source in HISTORY_FETCHERS:
        for term in SEARCH_TERMS:
            _page, _expected, done = native_job_get(source, term)
            if done:
                continue
            term_saved, source_ok = crawl_history_term(source, term)
            saved += term_saved
            jobs += 1
            if not source_ok:
                log(f"WARN pausing remaining {source} keyword-history jobs until next cycle")
                break
            if jobs >= HISTORY_JOBS_PER_CYCLE:
                return saved
    return saved


def native_history_complete():
    if not NATIVE_HISTORY_ENABLED:
        return True
    sources = tuple(HISTORY_FETCHERS)
    total = len(sources) * len(SEARCH_TERMS)
    placeholders = ",".join("?" for _ in sources)
    done = db.execute(
        f"SELECT COUNT(*) FROM native_jobs WHERE jobset=? AND source IN ({placeholders}) AND done=1",
        (NATIVE_JOBSET, *sources),
    ).fetchone()[0]
    return int(done) >= total


def crawl_full_history_source(source):
    state_source = "full:" + source
    term = "__ALL_PUBLIC_PAGES__"
    fetcher = FULL_HISTORY_FETCHERS[source]
    page, expected_more, done = native_job_get(state_source, term)
    if done:
        return 0, True
    saved = 0
    pages_done = 0
    log(f"FULL HISTORY {source}: resume public page={page}")
    while pages_done < FULL_HISTORY_PAGES_PER_SOURCE_PER_CYCLE:
        try:
            items, has_next = fetcher(page)
        except HttpStatusError as e:
            if e.status in (404, 410) and page > 1 and not expected_more:
                native_job_update(state_source, term, page, False, done=True)
                log(f"FULL HISTORY DONE {source}: terminal HTTP {e.status} at page {page}")
                return saved, True
            log(f"WARN full-history fetch failed [{source}] page={page}: {e}")
            return saved, False
        except Exception as e:
            log(f"WARN full-history fetch failed [{source}] page={page}: {e}")
            return saved, False
        if not items:
            if page == 1 or not expected_more:
                native_job_update(state_source, term, page, False, done=True)
                log(f"FULL HISTORY DONE {source}: empty page={page}")
                return saved, True
            log(f"WARN {source} advertised another public page but page {page} is empty; checkpoint held")
            return saved, False
        sig = item_signature(items)
        prior = signature_seen(state_source, term, sig)
        if prior is not None and int(prior[0]) != page:
            if not expected_more:
                native_job_update(state_source, term, page, False, done=True)
                log(f"FULL HISTORY DONE {source}: terminal page repeated page {prior[0]}")
                return saved, True
            log(f"WARN {source} full-history pagination loop: page {page} repeats page {prior[0]}; checkpoint held")
            return saved, False
        for item in items:
            saved += process_native_item(item, source)
        remember_signature(state_source, term, page, sig)
        next_page = page + 1
        native_job_update(state_source, term, next_page, has_next, done=False)
        expected_more = has_next
        page = next_page
        pages_done += 1
        source_sleep(source)
    return saved, True


def run_full_history():
    if not NATIVE_FULL_HISTORY_ENABLED:
        return 0
    saved = 0
    for source in FULL_HISTORY_FETCHERS:
        _page, _expected, done = native_job_get("full:" + source, "__ALL_PUBLIC_PAGES__")
        if not done:
            source_saved, _ok = crawl_full_history_source(source)
            saved += source_saved
    return saved


def full_history_complete():
    if not NATIVE_FULL_HISTORY_ENABLED:
        return True
    done = 0
    for source in FULL_HISTORY_FETCHERS:
        _page, _expected, is_done = native_job_get("full:" + source, "__ALL_PUBLIC_PAGES__")
        done += int(is_done)
    return done == len(FULL_HISTORY_FETCHERS)
def run_native_incremental():
    if not NATIVE_INCREMENTAL_ENABLED:
        return 0
    saved = 0
    for source, fetcher in INCREMENTAL_FETCHERS.items():
        previous_sig = None
        for page in range(1, NATIVE_INCREMENTAL_PAGES + 1):
            try:
                items, has_next = fetcher("", page)
            except Exception as e:
                log(f"WARN incremental fetch failed [{source}] page={page}: {e}")
                break
            if not items:
                break
            sig = item_signature(items)
            if sig == previous_sig:
                break
            previous_sig = sig
            for item in items:
                saved += process_native_item(item, source)
            if not has_next:
                break
            source_sleep(source)
    return saved



def _parse_fixture_nyaa(text):
    out = []
    for row in rows(text):
        aa = anchors(row); d=[a for a in aa if re.match(r"^/view/\d+",a["href"])]; t=[a for a in aa if re.match(r"^/download/\d+\.torrent",a["href"])]
        if d and t: out.append((d[-1]["text"],t[0]["href"]))
    return out

def _parse_fixture_acg(text):
    out=[]
    for row in rows(text):
        aa=anchors(row); d=[a for a in aa if re.match(r"^/t/\d+(?:$|[/?#])",a["href"]) and a["text"]]; t=[a for a in aa if ".torrent" in a["href"].lower()]
        if d and t: out.append((d[0]["text"],t[0]["href"]))
    return out

def _parse_fixture_mikan(text):
    out=[]
    for row in rows(text):
        aa=anchors(row); d=[a for a in aa if a["href"].startswith("/Home/Episode/") and a["text"]]; t=[a for a in aa if a["href"].startswith("/Download/")]
        if d and t: out.append((d[0]["text"],t[0]["href"]))
    return out

def _parse_fixture_dmhy(text):
    out=[]
    for row in rows(text):
        aa=anchors(row); d=[a for a in aa if re.match(r"^/topics/view/\d+\.html",a["href"]) and a["text"]]
        if d: out.append((d[0]["text"],d[0]["href"]))
    return out



def run_parser_self_test() -> None:
    nyaa = '<table><tr><td></td><td><a href="/view/123">[VCB-Studio] A [BDRip]</a></td><td><a href="/download/123.torrent">D</a></td></tr></table><a href="/?q=x&p=2">Next</a>'
    acg = '<table><tr><td class="title"><span class="title"><a href="/t/123">[VCB-Studio] A [BDRip]</a></span></td><td class="action"><a href="/t/123.torrent">Download</a></td></tr></table><li class="next"><a href="/?page=2">Next</a></li>'
    mikan = '<table><tr><td><a href="/Home/Episode/abc">[ANi] A [Baha][CHT]</a></td><td><a href="/Download/abc.torrent">D</a></td></tr></table><a href="/Home/Search?searchstr=x&page=2">2</a>'
    dmhy = '<table><tr><td><a href="/topics/view/123.html">[VCB-Studio] A [BDRip]</a></td><td><a href="magnet:?xt=urn:btih:X">M</a></td></tr></table><a href="/topics/list/page/2?keyword=x">下一頁</a>'
    tests = {
        "nyaa": len(_parse_fixture_nyaa(nyaa)) == 1 and html_has_page(nyaa, 2),
        "acgrip": len(_parse_fixture_acg(acg)) == 1 and html_has_page(acg, 2),
        "mikan": len(_parse_fixture_mikan(mikan)) == 1 and html_has_page(mikan, 2),
        "dmhy": len(_parse_fixture_dmhy(dmhy)) == 1 and html_has_page(dmhy, 2),
    }
    failed = [name for name, ok in tests.items() if not ok]
    if failed:
        raise RuntimeError("parser self-test failed: " + ",".join(failed))


def _history_counts() -> tuple[bool, bool]:
    return full_history_complete(), native_history_complete()


def main(*, self_test: bool = False, audit_only: bool = False, once: bool = False, restore_quarantine: int | None = None) -> int:
    global state, db, _cycle_stats
    run_parser_self_test()
    if self_test:
        print("torrent-collector self-test passed", flush=True)
        return 0
    state = CollectorState(
        DB_PATH,
        OUT,
        quarantine_dir=QUARANTINE,
        max_retry_attempts=MAX_RETRY_ATTEMPTS,
        output_uid=OUT_UID,
        output_gid=OUT_GID,
    )
    db = state.db
    try:
        if restore_quarantine is not None:
            restored = state.restore_quarantine(restore_quarantine)
            print(json.dumps({"restored": restored, "moveId": restore_quarantine}, sort_keys=True))
            return 0 if restored else 1
        reconciled = state.reconcile()
        audit = state.audit_existing(_audit_classifier, mode=AUDIT_MODE)
        if audit_only:
            print(json.dumps({"reconcile": reconciled, "audit": audit}, ensure_ascii=False, sort_keys=True))
            return 0
        log("torrent-collector starting")
        log(f"output={OUT}; state={STATE}; search_ruleset={SEARCH_RULESET_ID}; filter_ruleset={FILTER_RULESET_ID}")
        log(f"search_terms={len(SEARCH_TERMS)}; audit={AUDIT_MODE}; network={'SOCKS5h '+PROXY_HOST+':'+str(PROXY_PORT)+' -> adaptive fallback' if PROXY_ENABLED else 'adaptive proxy/system/direct'}")
        next_native_incremental_at = 0.0
        while True:
            _cycle_stats = {}
            cycle_saved = 0
            rec = state.reconcile()
            for key, value in rec.items():
                _bump("pool_" + key, value)
            cycle_saved += run_discovery_reevaluation()
            cycle_saved += run_retry_queue()
            now = time.monotonic()
            if now >= next_native_incremental_at:
                cycle_saved += run_native_incremental()
                next_native_incremental_at = time.monotonic() + POLL
            if not full_history_complete():
                cycle_saved += run_full_history()
            if not native_history_complete():
                cycle_saved += run_native_history()
            rec = state.reconcile()
            for key, value in rec.items():
                _bump("pool_" + key, value)
            full_complete, keyword_complete = _history_counts()
            pending = db.execute("SELECT COUNT(*) FROM retry_queue WHERE state='retryable'").fetchone()[0]
            reeval_pending = state.reevaluation_pending_count(catalog_matcher.generation())
            _cycle_stats["reevaluation_pending"] = reeval_pending
            histories_complete = full_complete and keyword_complete
            if state.needs_legacy_discovery_crawl() and histories_complete:
                state.mark_legacy_discovery_crawl_complete()
            sleep_for = min(POLL, HISTORY_RETRY_SECONDS) if not histories_complete or pending or reeval_pending else POLL
            _cycle_stats["saved"] = cycle_saved
            summary = " ".join(f"{key}={value}" for key, value in sorted(_cycle_stats.items()))
            log(f"cycle complete: {summary} full_history={full_complete} keyword_history={keyword_complete} retryable={pending} next={sleep_for}s")
            if once:
                return 0
            time.sleep(sleep_for)
    finally:
        if state is not None:
            state.close()
        state = None
        db = None
