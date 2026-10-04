"""Best-effort title/description lookup for a saved link."""

import ipaddress
import logging
import re
import socket
import urllib.request
import zlib
from html.parser import HTMLParser
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_BYTES = 512 * 1024
TIMEOUT_S = 4.0
USER_AGENT = "Mozilla/5.0 (compatible; stash-link-preview/1.0)"


class BlockedURL(Exception):
    pass


def assert_public(url: str) -> None:
    """Refuse anything that resolves to a non-public address (SSRF guard)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise BlockedURL(f"{parts.hostname} resolves to non-public address {address}")


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GuardedRedirects())


class _HeadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_SEPARATORS = (" - ", " | ", " — ", " – ", " · ", " :: ")


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def strip_site_name(title: str, site_names: list[str]) -> str:
    """'Amazon DynamoDB - Wikipedia' -> 'Amazon DynamoDB'; the domain is shown next to the title anyway."""
    names = {n.casefold() for n in site_names if n}
    for sep in _SEPARATORS:
        head, _, tail = title.rpartition(sep)
        if head and tail.strip().casefold() in names:
            return head.strip()
        lead, _, rest = title.partition(sep)
        if rest and lead.strip().casefold() in names:
            return rest.strip()
    return title


def _repeats(excerpt: str, title: str) -> bool:
    words = _words(excerpt)
    if not words:
        return True
    title_words = set(_words(title))
    return sum(w in title_words for w in words) / len(words) >= 0.8


def parse_head(html: str, url: str = "") -> dict:
    end = re.search(r"</head\s*>", html, re.I)
    parser = _HeadParser()
    try:
        parser.feed(html[: end.start()] if end else html)
    except Exception:  # malformed markup is common; keep whatever we parsed
        pass
    m = parser.meta
    title = _clean(m.get("og:title") or m.get("twitter:title") or parser.title, 300)
    excerpt = _clean(m.get("og:description") or m.get("description") or m.get("twitter:description") or "", 400)

    host = (urlsplit(url).hostname or "").removeprefix("www.")
    labels = host.split(".")
    domain_label = labels[-2] if len(labels) >= 2 else ""
    title = strip_site_name(title, [m.get("og:site_name", ""), domain_label, host])
    if excerpt and _repeats(excerpt, title):
        excerpt = ""
    return {"title": title, "excerpt": excerpt}


def decompress(raw: bytes, content_encoding: str | None) -> bytes | None:
    """Undo gzip/deflate (some servers send it unasked); output is capped to defuse zip bombs."""
    encoding = (content_encoding or "identity").strip().lower()
    if encoding == "identity":
        return raw
    if encoding in ("gzip", "x-gzip"):
        wbits = 16 + zlib.MAX_WBITS
    elif encoding == "deflate":
        wbits = zlib.MAX_WBITS if raw[:1] == b"\x78" else -zlib.MAX_WBITS
    else:
        return None
    try:
        return zlib.decompressobj(wbits).decompress(raw, MAX_BYTES)
    except zlib.error:
        return None


def _decode(body: bytes, header_charset: str | None) -> str:
    charset = header_charset
    if not charset:
        sniff = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
        charset = sniff.group(1).decode("ascii") if sniff else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch(url: str) -> dict:
    """Return {"title", "excerpt"}; empty strings if the page can't be read."""
    empty = {"title": "", "excerpt": ""}
    try:
        assert_public(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        with _opener.open(request, timeout=TIMEOUT_S) as resp:
            if "html" not in (resp.headers.get_content_type() or ""):
                return empty
            body = decompress(resp.read(MAX_BYTES), resp.headers.get("Content-Encoding"))
            if body is None:
                return empty
            return parse_head(_decode(body, resp.headers.get_content_charset()), resp.geturl())
    except Exception as exc:
        log.info("link preview failed for %s: %s", url, exc)
        return empty
