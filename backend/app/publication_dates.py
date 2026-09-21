"""Publication evidence and a rolling calendar-year policy, independent of collection time."""
import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

POLICY_VERSION = 'publication-year-v1'


def today_local():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def parse_publication_date(value):
    text = re.sub(r'(\d{1,2})(?:st|nd|rd|th)\b', r'\1', str(value or '').strip(), flags=re.I)
    if not text:
        return None
    try:
        # Require a complete day. Never invent Jan 1 for a year-only source.
        if re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[T\s])', text):
            return date.fromisoformat(text[:10])
        match = re.fullmatch(r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?(?:\s+.*)?', text)
        if match:
            return date(*map(int, match.groups()))
    except ValueError:
        return None
    try:
        return parsedate_to_datetime(text).date()
    except (ValueError, TypeError, OverflowError):
        pass
    for fmt in ('%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y', '%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def recent_cutoff(today=None):
    current = today or today_local()
    try:
        return current.replace(year=current.year - 1)
    except ValueError:
        return current.replace(year=current.year - 1, day=28)


def date_status(value, today=None):
    parsed, current = parse_publication_date(value), today or today_local()
    if parsed is None:
        return 'unknown'
    if parsed > current:
        return 'future'
    return 'recent' if parsed >= recent_cutoff(current) else 'outdated'


def date_policy(today=None):
    current = today or today_local()
    return {'version': POLICY_VERSION, 'window': '1y', 'cutoff': recent_cutoff(current).isoformat(),
            'until': current.isoformat(), 'timezone': 'Asia/Shanghai', 'basis': 'published_at',
            'rule': '仅纳入最近一年内发布的案例；发布时间不代表实际发生时间，收录和更新时间不能替代发布日期。'}


def with_publication_dates(item):
    result = dict(item)
    parsed = parse_publication_date(result.get('published_at'))
    result['published_at'] = parsed.isoformat() if parsed else None
    result['date_status'] = date_status(result['published_at'])
    result['date_policy'] = date_policy()
    return result


def temporal_view(item):
    """Apply the same gate to existing records on every read as the calendar moves."""
    result = with_publication_dates(item)
    if result.get('discovery_mode') not in {'web_search', 'rss', 'douyin_browser'}:
        return result
    status, timing = result.get('status'), result['date_status']
    if status != 'rejected' and timing != 'recent':
        if timing == 'outdated':
            result['status'] = 'outdated'
        elif timing == 'future' or status in {'selected', 'featured', 'candidate', 'needs_scoring', 'needs_date'}:
            result['status'] = 'needs_date'
        if result.get('status') != status:
            result['content_status'] = status
    return result


_PUBLISHED_KEYS = {'article:published_time', 'og:published_time', 'datepublished', 'pubdate',
                   'publishdate', 'publication_date', 'citation_publication_date', 'dc.date.issued'}
_UPDATED_KEYS = {'article:modified_time', 'datemodified', 'last-modified'}


class _DateParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.published, self.updated, self.json_blocks = [], [], []
        self._json = None

    def handle_starttag(self, tag, attrs):
        attrs = {key.lower(): value or '' for key, value in attrs}
        if tag == 'meta':
            key = (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').lower()
            if key in _PUBLISHED_KEYS:
                self.published.append((attrs.get('content'), f'html_meta:{key}'))
            elif key in _UPDATED_KEYS:
                self.updated.append((attrs.get('content'), f'html_meta:{key}'))
        if tag == 'time' and attrs.get('datetime'):
            marker = (attrs.get('itemprop', '') + ' ' + attrs.get('class', '')).lower()
            if 'modified' in marker or 'updated' in marker:
                self.updated.append((attrs['datetime'], 'html_time:updated'))
            elif 'datepublished' in marker or 'published' in marker or 'pubdate' in attrs:
                self.published.append((attrs['datetime'], 'html_time:published'))
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self._json = []

    def handle_data(self, data):
        if self._json is not None:
            self._json.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._json is not None:
            self.json_blocks.append(''.join(self._json))
            self._json = None


def publication_metadata(payload, content_type='text/html', source_url=''):
    """Accept explicit publisher dates; ignore copyright, navigation and modified dates."""
    if not (content_type.startswith('text/') or content_type in {'application/xhtml+xml', 'application/json'}):
        return {}
    source = payload.decode('utf-8', errors='replace') if isinstance(payload, bytes) else str(payload)
    parser = _DateParser()
    if content_type in {'text/html', 'application/xhtml+xml'}:
        parser.feed(source)
    def page_path(url):
        return urlsplit(str(url)).path.rstrip('/')
    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            types = node.get('@type', [])
            if isinstance(types, str):
                types = [types]
            # Do not walk related links/items: their dates belong to other articles.
            article_types = {'Article', 'NewsArticle', 'BlogPosting', 'TechArticle', 'Report', 'ScholarlyArticle', 'SocialMediaPosting'}
            link = node.get('url')
            if article_types.intersection(types) and (not link or not source_url or page_path(link) == page_path(source_url)):
                parser.published.append((node.get('datePublished'), 'json_ld:datePublished'))
                parser.updated.append((node.get('dateModified'), 'json_ld:dateModified'))
            for key in ('@graph', 'mainEntity'):
                if key in node:
                    walk(node[key])
    for block in parser.json_blocks:
        try:
            walk(json.loads(block))
        except (ValueError, TypeError):
            pass
    # Plain-text representations preserve explicitly labelled publication headers.
    date_expr = r'(\d{4}-\d{2}-\d{2}(?:T[^\s<]+)?|\d{4}年\d{1,2}月\d{1,2}日|[A-Z][a-z]+ \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]+ \d{4})'
    for match in re.finditer(r'(?:Published(?:\s+Time|\s+on|\s+at)?|发布时间|发布日期|发表于)\s*[:：]?\s*' + date_expr, source[:8000], re.I):
        parser.published.append((match.group(1), 'publication_label'))
    # Publisher-specific footer, verified against the article's own dated URL.
    # Archive links elsewhere in the page must not supply a publication date.
    if urlsplit(source_url).hostname == 'simonwillison.net':
        match = re.search(r'<div class="entryFooter">Posted\s*<a href="(/\d{4}/[A-Za-z]{3}/\d{1,2}/)">([^<]+)</a>', source)
        if match and urlsplit(source_url).path.startswith(match.group(1)):
            parser.published.append((match.group(2), 'publisher_footer:posted'))
    result = {}
    for name, candidates in (('published_at', parser.published), ('updated_at', parser.updated)):
        for value, basis in candidates:
            parsed = parse_publication_date(value)
            if parsed:
                result.update({name: parsed.isoformat(), name + '_source': basis, name + '_evidence': str(value)[:200]})
                break
    return result
