"""A small, dependency-free website adapter for SelfGraph MCP.

The adapter validates an approved public URL, extracts visible text, and
builds the ``insert_text`` payload expected by SelfGraph.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import ipaddress
import re
import socket
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 25
UPSTREAM_REPO = "https://github.com/hydra-db/selfgraph"


class SelfGraphSiteError(ValueError):
    """A user-fixable website connector error."""


@dataclass(frozen=True)
class SiteConfig:
    url: str
    allowed_hosts: tuple[str, ...]
    namespace: str = "default"


def _blocked_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified


def _normalise_host(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if host.startswith("*."):
        host = host[2:]
    if not host or any(char in host for char in " /\\:@"):
        raise SelfGraphSiteError("网站白名单里的域名不合法")
    return host


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    return any(host == allowed for allowed in allowed_hosts)


def validate_url(url: str, allowed_hosts: tuple[str, ...] | list[str] | None = None) -> str:
    """Validate and canonicalise a public HTTP(S) URL."""
    raw = str(url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise SelfGraphSiteError("网站地址必须使用 http 或 https")
    if not parts.hostname or parts.username or parts.password:
        raise SelfGraphSiteError("网站地址必须包含公开域名，不能带账号或密码")
    host = parts.hostname.lower().rstrip(".")
    if _blocked_ip(host) or host in {"localhost", "local", "broadcasthost"} or host.endswith(".local"):
        raise SelfGraphSiteError("网站连接器不允许访问本机或内网地址")
    hosts = tuple(_normalise_host(item) for item in (allowed_hosts or (host,)))
    if not _host_allowed(host, hosts):
        raise SelfGraphSiteError("这个网站不在已配置的域名白名单里")
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))


def validate_config(payload: dict) -> SiteConfig:
    """Validate the persisted, non-secret website connector settings."""
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        raise SelfGraphSiteError("请先填写网站地址")
    parts = urlsplit(raw_url)
    host = (parts.hostname or "").lower().rstrip(".")
    raw_hosts = payload.get("allowed_hosts") or [host]
    if not isinstance(raw_hosts, list | tuple):
        raise SelfGraphSiteError("网站白名单格式不正确")
    hosts = tuple(dict.fromkeys(_normalise_host(item) for item in raw_hosts))
    url = validate_url(raw_url, hosts)
    namespace = str(payload.get("namespace") or "default").strip()
    if not namespace or len(namespace) > 80 or any(char.isspace() or ord(char) < 32 for char in namespace):
        raise SelfGraphSiteError("SelfGraph 命名空间不能为空、不能含空白字符，长度最多 80 个字符")
    return SiteConfig(url=url, allowed_hosts=hosts, namespace=namespace)


class _VisibleTextParser(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg", "canvas"}
    _BLOCK = {"article", "blockquote", "br", "dd", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre", "section", "td", "th", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._title_depth = 0
        self._pieces: list[str] = []
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title" and not self._skip_depth:
            self._title_depth = 1
        if not self._skip_depth and tag in self._BLOCK:
            self._pieces.append("\n")

    def handle_startendtag(self, tag: str, attrs):
        if tag.lower() in self._BLOCK:
            self._pieces.append("\n")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag == "title" and self._title_depth:
            self._title_depth = 0
        if not self._skip_depth and tag in self._BLOCK:
            self._pieces.append("\n")
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        text = html_lib.unescape(data)
        if self._title_depth:
            self._title.append(text)
        self._pieces.append(text)

    @property
    def title(self) -> str:
        return _clean_inline(" ".join(self._title))

    @property
    def text(self) -> str:
        lines = []
        for line in "".join(self._pieces).splitlines():
            value = _clean_inline(line)
            if value and (not lines or value != lines[-1]):
                lines.append(value)
        return "\n".join(lines)


def _clean_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_document(url: str, body: str, content_type: str = "text/html") -> dict:
    """Convert HTML or plain text into a deterministic SelfGraph document."""
    if "html" in content_type.lower() or "xhtml" in content_type.lower():
        parser = _VisibleTextParser()
        try:
            parser.feed(body)
            parser.close()
        except Exception as exc:  # HTMLParser is forgiving, but malformed input should be explicit.
            raise SelfGraphSiteError("网页内容无法解析") from exc
        title, text = parser.title, parser.text
    else:
        title, text = "", "\n".join(_clean_inline(line) for line in body.splitlines() if _clean_inline(line))
    if not text:
        raise SelfGraphSiteError("网页没有可导入的正文文本")
    if not title:
        title = _clean_inline(urlsplit(url).path.rsplit("/", 1)[-1]) or urlsplit(url).hostname or "网页"
    source_id = "web:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return {"source_id": source_id, "url": url, "title": title[:240], "text": text, "content_type": content_type}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SelfGraphSiteError("网站发生跳转，请把跳转后的最终地址加入白名单后再导入")


def _assert_resolved_public_host(host: str) -> None:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        return
    if any(_blocked_ip(address) for address in addresses):
        raise SelfGraphSiteError("网站域名解析到了本机或内网地址，已拒绝访问")


def fetch_document(url: str, allowed_hosts: tuple[str, ...] | list[str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fetch one approved page without following cross-origin redirects."""
    canonical = validate_url(url, allowed_hosts)
    _assert_resolved_public_host(urlsplit(canonical).hostname or "")
    request = Request(canonical, headers={"User-Agent": "livis-selfgraph-adapter/1.0", "Accept": "text/html,text/plain;q=0.9"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=max(1.0, min(60.0, float(timeout)))) as response:
            content_type = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else "text/html"
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise SelfGraphSiteError("这个地址不是网页或纯文本页面")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SelfGraphSiteError("网页超过 4 MB，暂不导入")
            headers = Message()
            headers["content-type"] = response.headers.get("Content-Type", "")
            charset = headers.get_content_charset() or "utf-8"
            return extract_document(canonical, body.decode(charset, errors="replace"), content_type)
    except SelfGraphSiteError:
        raise
    except HTTPError as exc:
        raise SelfGraphSiteError(f"网站返回 HTTP {exc.code}") from exc
    except URLError as exc:
        raise SelfGraphSiteError("网站暂时无法访问") from exc


def insert_arguments(document: dict, namespace: str) -> dict:
    """Build the exact arguments for SelfGraph's MCP ``insert_text`` tool."""
    return {
        "text": document["text"],
        "source_id": document["source_id"],
        "metadata": {"url": document["url"], "title": document["title"], "connector": "livis-selfgraph", "namespace": namespace},
    }
