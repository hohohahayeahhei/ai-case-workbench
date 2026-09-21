"""Source discovery and extraction adapters for the collection side.

The adapter is deliberately dependency-light: fixture discovery is fully
offline, while RSS/Atom and HTML fetching are opt-in through explicit URLs.
Fetched pages are treated as untrusted content and are never executed.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import io
import json
import re
import socket
import time
from http.client import IncompleteRead, RemoteDisconnected
from .network_policy import TRANSIENT_HTTP, retry_delay
from .reader_transport import fetch_reader, validate_reader_target
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


from .publication_dates import publication_metadata, parse_publication_date, date_status


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self.title_parts: list[str] = []
        self.article_parts: list[str] = []
        self._title = False
        self._article = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if tag == "title":
            self._title = True
        if tag in {"article", "main"}:
            self._article += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._title = False
        if tag in {"article", "main"} and self._article:
            self._article -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)
                if self._title:
                    self.title_parts.append(text)
                if self._article:
                    self.article_parts.append(text)


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) article URLs are accepted")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Private/local addresses are not article sources")


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SourceDiscoveryAgent:
    name = "source-discovery-agent"

    def __init__(self, project_root: str | Path, timeout: int = 18) -> None:
        self.root = Path(project_root)
        self.timeout = timeout

    def fixture_candidates(self, source_mode: str = "online_snapshot") -> list[dict[str, Any]]:
        paths = []
        if source_mode in {"online_snapshot", "all"}:
            paths.append(self.root / "data/fixtures/online_cases.json")
        if source_mode in {"fixture", "all"}:
            paths.append(self.root / "data/fixtures/cases.json")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in paths:
            for case in json.loads(path.read_text(encoding="utf-8")):
                if case.get("case_id") not in seen:
                    seen.add(case["case_id"])
                    items.append({**case, "discovery_mode": "fixture"})
        return items

    def catalog_feed_urls(self) -> list[str]:
        """Return enabled public RSS/Atom sources from the local catalog."""
        catalog_path = self.root / "data/fixtures/source_catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        return [
            str(item["url"])
            for item in catalog
            if item.get("enabled") and item.get("kind") in {"rss", "atom"} and item.get("url")
        ]

    def _catalog(self) -> list[dict[str, Any]]:
        catalog_path = self.root / "data/fixtures/source_catalog.json"
        return json.loads(catalog_path.read_text(encoding="utf-8"))

    def source_metadata(self, url: str) -> dict[str, Any]:
        """Return source trust metadata without turning it into a quality score.

        A source tier guides discovery and evidence requirements.  It never
        bypasses extraction, verification, or independent quality review.
        """
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/") or "/"
        if host == "developer.cloud.tencent.com" or (host == "cloud.tencent.com" and path.startswith("/developer/")):
            return {
                "source_name": "腾讯云开发者社区（作者投稿）",
                "source_class": "community_submission",
                "source_role": "discovery",
                "evidence_policy": "requires_original_link",
                "source_catalog_id": None,
            }
        for item in self._catalog():
            candidates = [str(item.get("url", ""))] + [str(value) for value in item.get("alternate_urls", [])]
            for candidate in candidates:
                candidate_url = urlparse(candidate)
                candidate_host = (candidate_url.hostname or "").lower().removeprefix("www.")
                if candidate_host != host:
                    continue
                match_path = str(item.get("match_path") or "").rstrip("/")
                if match_path and not (path == match_path or path.startswith(match_path + "/")):
                    continue
                return {
                    "source_name": str(item.get("name") or host or "公开网页"),
                    "source_class": str(item.get("source_class") or item.get("kind") or "unknown_web"),
                    "source_role": str(item.get("source_role") or "candidate"),
                    "evidence_policy": str(item.get("evidence_policy") or "requires_original_link"),
                    "source_catalog_id": item.get("source_id"),
                }
        return {
            "source_name": host or "公开网页",
            "source_class": "unknown_web",
            "source_role": "candidate",
            "evidence_policy": "requires_original_link",
            "source_catalog_id": None,
        }

    def source_label(self, url: str) -> str:
        """Return a human-readable source label for the UI and exports."""
        return self.source_metadata(url)["source_name"]

    @staticmethod
    def fetch_profile(url: str) -> dict[str, Any]:
        """Classify a public page and declare bounded read fallbacks.

        These are read-only public representations. The original URL remains
        the citation URL; a fallback only supplies text when the site blocks
        the local HTTP client.
        """
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/") or "/"
        if host == "reddit.com" or host.endswith(".reddit.com"):
            alternatives = [
                f"https://old.reddit.com{path}{'?' + parsed.query if parsed.query else ''}",
                f"https://www.reddit.com{path}.json{'?' + parsed.query if parsed.query else ''}",
                "https://r.jina.ai/" + url,
            ]
            return {"category": "community_platform", "label": "社区平台：优先公开JSON/旧版页面", "alternatives": alternatives}
        if host == "medium.com" or host.endswith(".medium.com"):
            return {"category": "publishing_platform", "label": "文章平台：优先只读文本转译", "alternatives": ["https://r.jina.ai/" + url]}
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            return {"category": "professional_network", "label": "职业社区：仅尝试公开文本转译", "alternatives": ["https://r.jina.ai/" + url]}
        if host.endswith(".substack.com") or host == "substack.com":
            return {"category": "newsletter_platform", "label": "Newsletter平台：优先公开文本转译", "alternatives": ["https://r.jina.ai/" + url]}
        if path.lower().endswith(".pdf"):
            return {"category": "pdf_document", "label": "PDF文档：下载后提取正文", "alternatives": []}
        if host in {"openai.com", "techradar.com", "tomsguide.com", "theverge.com", "creativebloq.com", "androidcentral.com"} or host.endswith(".tomsguide.com"):
            return {"category": "script_heavy_publisher", "label": "脚本型媒体：失败后使用只读文本转译", "alternatives": ["https://r.jina.ai/" + url]}
        return {"category": "standard_public_web", "label": "公开网页及公开文本备选", "alternatives": ["https://r.jina.ai/" + url]}

    def official_source_search_query(self, topic: str) -> str:
        """Build a focused query for configured first-party Chinese sources."""
        items = self._catalog()
        official = [item for item in items if item.get("enabled") and item.get("search_enabled", True)
                    and item.get("source_class") in {"official", "official_editorial"}]
        names = "、".join(str(item.get("name")) for item in official)
        domains = []
        for item in official:
            for value in [item.get("url", "")] + list(item.get("alternate_urls", [])):
                host = urlparse(str(value)).hostname
                if host and host not in domains:
                    domains.append(host)
        sites = " OR ".join(f"site:{domain}" for domain in domains)
        return f"{topic} 第一方真实使用案例 {names} ({sites})" if sites else topic

    def search_branches(self, topic: str) -> list[dict[str, str]]:
        """Build bounded, tier-aware search branches for the scout agent."""
        items = [item for item in self._catalog() if item.get("enabled") and item.get("search_enabled", True)]
        groups = [
            ("official", "官方一手来源", {"official", "official_editorial"}, "真实使用案例 产品实践 工作流"),
            ("official_social", "官方社交线索", {"official_social"}, "发布 公告 实践 原文链接"),
            ("practitioner", "实践者原文", {"practitioner"}, "亲身实践 工作流 步骤 结果"),
            ("context", "专业资讯线索", {"professional_media", "analysis"}, "案例 原始来源 用户实践"),
        ]
        branches: list[dict[str, str]] = []
        for branch_id, label, classes, suffix in groups:
            selected = [item for item in items if item.get("source_class") in classes]
            domains: list[str] = []
            names: list[str] = []
            for item in selected:
                names.append(str(item.get("name") or ""))
                for value in [item.get("url", "")] + list(item.get("alternate_urls", [])):
                    host = urlparse(str(value)).hostname
                    if host and host not in domains:
                        domains.append(host)
            if len(domains) > 4:
                from .publication_dates import today_local
                offset = today_local().toordinal() % len(domains)
                domains = (domains[offset:] + domains[:offset])[:4]
            sites = " OR ".join(f"site:{domain}" for domain in domains)
            if sites:
                branches.append({"id": branch_id, "label": label,
                                 "query": f"{topic} {suffix} ({sites})"})
        return branches

    def discover_feeds(self, feed_urls: Iterable[str], query: str = "", max_results: int = 20) -> list[dict[str, Any]]:
        """Fetch explicit RSS/Atom feeds and return raw candidates.

        This is intentionally a candidate stage.  A feed item without an
        article-level evidence bundle cannot enter the selected catalog.
        """
        results: list[dict[str, Any]] = []
        tokens = [token.lower() for token in query.split() if token]
        for feed_url in feed_urls:
            payload = self._fetch_bytes(feed_url, accept="application/rss+xml, application/atom+xml, text/xml")
            for item in self._parse_feed(payload, feed_url):
                haystack = json.dumps(item, ensure_ascii=False).lower()
                if tokens and not any(token in haystack for token in tokens):
                    continue
                item.update(self.source_metadata(item.get("source_url") or feed_url))
                item["source_name"] = self.source_label(item.get("source_url") or feed_url)
                results.append(item)
                if len(results) >= max_results:
                    return results
        return results

    def fetch_page(self, url: str) -> dict[str, Any]:
        profile = self.fetch_profile(url)
        attempts = [url] + [candidate for candidate in profile["alternatives"] if candidate != url]
        failures = []
        for index, candidate in enumerate(attempts):
            try:
                payload, content_type = self._fetch_bytes(candidate, accept="text/html, text/plain", include_content_type=True)
                text, title = self._extract_document(payload, content_type, candidate)
                if self._access_page(text, title):
                    raise ValueError("Source access restricted: challenge page or login/paywall")
                if len(text) < 100:
                    raise ValueError("Article body is empty or too short")
                return {
                    "url": url,
                    "domain": urlparse(url).netloc,
                    "title": title or self._title(text),
                    "text": text[:20000],
                    "content_hash": hashlib.sha256(payload).hexdigest(),
                    "fetched_url": candidate,
                    "fetch_strategy": "direct" if index == 0 else profile["category"],
                    "fetch_strategy_label": profile["label"],
                    **publication_metadata(payload, content_type, url),
                }
            except (OSError, ValueError) as exc:
                strategy = 'direct' if index == 0 else profile['category']
                failures.append(f"{strategy}[{urlparse(candidate).hostname}]: {type(exc).__name__}: {str(exc)[:160]}")
                # Do not use a fallback for private/local addresses: that is a
                # security decision, not a transient access problem.
                if "Private/local" in str(exc):
                    # Local DNS may be polluted. A public-DNS-verified domain can
                    # still be read by the public Reader service; never connect
                    # directly to the rejected address or forward private IPs.
                    if index == 0 and any(urlparse(alt).hostname == 'r.jina.ai' for alt in attempts[1:]):
                        try:
                            validate_reader_target(url)
                            continue
                        except (OSError, ValueError):
                            pass
                    break
        raise RuntimeError("; ".join(failures[-3:])[:700])

    @staticmethod
    def _extract_document(payload: bytes, content_type: str, source_url: str) -> tuple[str, str]:
        if content_type == "application/pdf" or source_url.lower().split("?", 1)[0].endswith(".pdf"):
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(payload))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                return " ".join(text.split()), ""
            except Exception as exc:
                raise ValueError(f"PDF正文提取失败: {type(exc).__name__}") from exc
        if content_type == "application/json" or source_url.endswith(".json"):
            try:
                data = json.loads(payload.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise ValueError("JSON article payload is invalid") from exc
            parts = []
            title = ""
            def walk(value: Any) -> None:
                nonlocal title
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == "title" and isinstance(child, str) and not title:
                            title = child
                        if key in {"selftext", "body", "text", "content"} and isinstance(child, str):
                            parts.append(child)
                        elif isinstance(child, (dict, list)):
                            walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(data)
            return " ".join(" ".join(parts).split()), title
        if content_type == 'text/plain':
            decoded = payload.decode('utf-8', errors='replace')
            title_match = re.search(r'^Title: (.+)$', decoded, re.M)
            # Reader's transport headers are metadata, not article evidence.
            body = decoded.split('Markdown Content:', 1)[-1]
            return ' '.join(body.split()), title_match.group(1) if title_match else ''
        parser = _TextParser()
        parser.feed(payload.decode("utf-8", errors="replace"))
        article = " ".join(parser.article_parts)
        text = article if len(article) >= 200 else " ".join(parser.parts)
        return text, " ".join(parser.title_parts)

    @staticmethod
    def _access_page(text, title):
        title = title.lower().strip()
        start = text[:1800].lower()
        markers = ('just a moment', 'verify you are human', 'checking your browser',
                   'enable javascript and cookies to continue', 'access denied',
                   'subscribe to continue reading', 'sign in to continue', 'captcha')
        # Only gate obvious interstitials; a normal article may discuss captcha.
        return any(m in title for m in markers) or (len(text) < 5000 and any(m in start for m in markers))

    def _fetch_bytes(self, url: str, accept: str, include_content_type: bool = False):
        reader = urlparse(url).hostname == 'r.jina.ai'
        if reader:
            # Nested proxy targets get the same public-address validation.
            target = url.split('r.jina.ai/', 1)[1]
            try:
                validate_public_url(target)
            except ValueError as exc:
                if 'Private/local' not in str(exc):
                    raise
                validate_reader_target(target)
        pinned_reader = False
        try:
            validate_public_url(url)
        except ValueError as exc:
            if not reader or 'Private/local' not in str(exc):
                raise
            # Never connect to the rejected local address. Resolve the fixed
            # Reader service publicly, pin its IP and retain TLS hostname checks.
            pinned_reader = True
        headers = {"User-Agent": "ai-case-workbench/0.3 (+public-evidence-reader)", "Accept": 'text/plain' if reader else accept}
        if reader:
            headers.update({'X-Timeout': '20', 'X-Respond-Timing':'visible-content'})
        request = urllib.request.Request(url, headers=headers)
        timeout = max(self.timeout, 45) if reader else self.timeout
        for attempt in range(2):
            error = None
            try:
                if pinned_reader:
                    payload, content_type = fetch_reader(url, headers, timeout, 10_000_000)
                    return (payload, content_type) if include_content_type else payload
                with urllib.request.build_opener(_PublicRedirect()).open(request, timeout=timeout) as response:
                    content_type = response.headers.get_content_type()
                    allowed = content_type.startswith("text/") or content_type in {"application/xhtml+xml", "application/rss+xml", "application/atom+xml", "application/xml", "application/json", "application/pdf"}
                    if not allowed:
                        raise ValueError(f"Unsupported article format: {content_type}")
                    size_limit = 10_000_000 if accept == 'text/html, text/plain' else 2_000_000
                    payload = response.read(size_limit + 1)
                    if len(payload) > size_limit:
                        raise ValueError(f"Source exceeds the {size_limit // 1_000_000} MB fetch limit")
                    return (payload, content_type) if include_content_type else payload
            except urllib.error.HTTPError as exc:
                if attempt or exc.code not in TRANSIENT_HTTP: raise
                error = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected) as exc:
                if attempt: raise
                error = exc
            time.sleep(retry_delay(error, attempt))

    @staticmethod
    def _parse_feed(payload: bytes, feed_url: str) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ValueError("Source is not valid RSS/Atom XML") from exc
        items: list[dict[str, Any]] = []
        nodes = list(root.findall(".//item")) + list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
        for node in nodes:
            def child(*names: str) -> str:
                for name in names:
                    found = node.find(name)
                    if found is not None and found.text:
                        return html.unescape(" ".join(found.text.split()))
                return ""

            link = child("link", "{http://www.w3.org/2005/Atom}link")
            if not link:
                atom_link = node.find("{http://www.w3.org/2005/Atom}link")
                link = str(atom_link.attrib.get("href", "")) if atom_link is not None else ""
            if not link:
                continue
            title = child("title", "{http://www.w3.org/2005/Atom}title")
            item_id = "feed-" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:14]
            items.append({
                "case_id": item_id,
                "title_original": title or link,
                "source_url": link,
                "source_name": urlparse(feed_url).netloc,
                "source_type": "rss",
                "published_at": child("pubDate", "published", "{http://www.w3.org/2005/Atom}published"),
                "published_at_source": "feed:published",
                "updated_at": child("updated", "{http://www.w3.org/2005/Atom}updated"),
                "summary": child("description", "summary", "{http://www.w3.org/2005/Atom}summary"),
                "status": "needs_extraction",
                "discovery_mode": "rss",
            })
        return items

    @staticmethod
    def _title(text: str) -> str:
        match = re.search(r".{0,120}", text)
        return (match.group(0) if match else "")[:120]
