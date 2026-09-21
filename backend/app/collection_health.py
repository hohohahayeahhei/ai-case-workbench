"""Explain collection outcomes and order work; never changes quality thresholds."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import re

PROCESSING_VERSION = 'reliability-v3.1'


def item_diagnostic(item):
    status = item.get('status')
    extraction = item.get('extraction') or {}
    quality = item.get('quality_evaluation') or {}
    text = str(extraction.get('reason') or '')
    if status == 'outdated':
        return {'code': 'outdated', 'label': '超出最近一年', 'reason': f"原文发布于 {item.get('published_at', '未知')}；保留历史记录，不进入当前精选。"}
    if status == 'needs_date':
        return {'code': 'date_unavailable', 'label': '发布日期待核实', 'reason': '缺少有效原文发布日期，或日期晚于今天；收录时间、页面更新时间不能作为新案例依据。'}
    if status in {'selected', 'featured'}:
        return {'code': 'selected', 'label': '通过核验与复审', 'reason': item.get('scorecard', {}).get('reason', '')}
    if status == 'candidate':
        return {'code': 'below_threshold', 'label': '已核验，未达精选标准', 'reason': item.get('scorecard', {}).get('reason', '')}
    if status == 'rejected':
        return {'code': 'content_rejected', 'label': '内容未通过', 'reason': quality.get('review', {}).get('reason') or item.get('verification', {}).get('reason') or text}
    if status == 'needs_scoring':
        return {'code': 'quality_pending', 'label': '评分或复审未完成', 'reason': '；'.join(quality.get('errors', [])) or '等待评分与独立复审'}
    if extraction.get('extraction_status') == 'fetch_failed':
        code, label = 'fetch_failed', '原文读取失败'
        # A direct 403 followed by a Reader timeout is a transient final failure,
        # not a permanent access block deserving a 24-hour cool-down.
        final_failure = text.rsplit('; ', 1)[-1].lower()
        if any(value in final_failure for value in ('403', '401', 'access restricted', 'challenge page')):
            code, label = 'access_blocked', '来源拒绝自动访问'
        elif 'private/local' in final_failure:
            code, label = 'address_rejected', '来源域名解析到非公网地址'
        elif 'fetch limit' in final_failure:
            code, label = 'page_too_large', '网页超出读取上限'
        elif 'timeout' in final_failure or 'timed out' in final_failure:
            label = '原文读取超时'
        return {'code': code, 'label': label, 'reason': text}
    if extraction.get('extraction_status') == 'date_unavailable':
        return {'code': 'date_unavailable', 'label': '原文日期待确认', 'reason': text}
    if extraction.get('extraction_status') in {'model_unavailable', 'verification_unavailable'}:
        return {'code': 'model_unavailable', 'label': '模型服务暂未完成', 'reason': text}
    if extraction.get('extraction_status') == 'model_rejected':
        return {'code': 'extraction_invalid', 'label': '抽取引文未通过校验', 'reason': text}
    return {'code': 'queued', 'label': '尚未处理', 'reason': '已发现线索，尚未消耗抓取和评审名额'}


def retry_ready(item):
    if item.get('attempt_count', 0) >= 3:
        return False
    value = item.get('next_retry_at')
    if not value:
        return True
    try:
        return datetime.fromisoformat(value) <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def reopen_fixed_failure(item):
    """Give old, exhausted failures one new bounded budget after a relevant fix."""
    if item.get('processing_version') == PROCESSING_VERSION or item.get('attempt_count', 0) < 1:
        return False
    reason_code = item_diagnostic(item)['code']
    fixed_date = reason_code == 'date_unavailable' and urlparse(item.get('source_url', '')).hostname == 'simonwillison.net'
    if not fixed_date and reason_code not in {
        'access_blocked', 'address_rejected', 'fetch_failed', 'page_too_large',
        'model_unavailable', 'extraction_invalid', 'quality_pending'
    }:
        return False
    item.setdefault('attempt_history', []).append({
        'attempt_count': item['attempt_count'], 'diagnostic': item_diagnostic(item),
        'processing_version': item.get('processing_version', 'legacy'),
        'at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    item.update(attempt_count=0, processing_version=PROCESSING_VERSION, retry_state='ready')
    item.pop('next_retry_at', None)
    return True


def set_retry_state(item):
    item['processing_version'] = PROCESSING_VERSION
    item['diagnostic'] = item_diagnostic(item)
    item.pop('next_retry_at', None)
    if item.get('status') not in {'needs_extraction', 'needs_scoring', 'needs_date'}:
        item['retry_state'] = 'not_needed'
        return
    if item.get('attempt_count', 0) >= 3:
        item['retry_state'] = 'exhausted'
        return
    # A content rejection is final; access blocks need a longer cool-down.
    hours = 24 if item['diagnostic']['code'] in {'access_blocked', 'address_rejected', 'date_unavailable'} else 0.25
    item['next_retry_at'] = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec='seconds')
    item['retry_state'] = 'cooldown'


def prioritize_candidates(items):
    def priority(item):
        score = {'practitioner': 85, 'official': 75, 'official_editorial': 70,
                 'professional_media': 45, 'analysis': 40, 'official_social': 15}.get(item.get('source_class'), 50)
        if item.get('status') == 'needs_scoring' and item.get('raw_document_id'):
            score += 70  # Finish an already fetched and verified article first.
        elif item.get('raw_document_id'):
            score += 15
        title = str(item.get('title_original', '')).lower()
        if re.search(r'how (?:i|we)|i (?:built|used|made)|case study|实战|实测|亲测', title):
            score += 15
        if re.search(r'benchmark|finetun|fine.tun|introducing|\btraining\b|发布|论文', title):
            score -= 35
        if item_diagnostic(item)['code'] in {'access_blocked', 'address_rejected', 'fetch_failed'}:
            score -= 80
        return -score, item.get('attempt_count', 0), item.get('collected_at', '')
    remaining = sorted(items, key=priority)
    ordered = []
    # One domain per pass, so a feed or inaccessible vendor cannot consume all slots.
    while remaining:
        used, deferred = set(), []
        for item in remaining:
            host = (urlparse(item['source_url']).hostname or '').removeprefix('www.')
            if host in used:
                deferred.append(item)
            else:
                used.add(host)
                ordered.append(item)
        remaining = deferred
    return ordered


def summarize_outcomes(entries):
    counts = Counter(entry.get('diagnostic', {}).get('code', 'unknown') for entry in entries)
    selected = sum(entry.get('status') in {'selected', 'featured'} for entry in entries)
    technical = sum(counts[key] for key in ('access_blocked', 'address_rejected', 'page_too_large', 'fetch_failed', 'model_unavailable', 'extraction_invalid', 'quality_pending'))
    return {'processed': len(entries), 'selected': selected, 'technical_pending': technical,
            'content_rejected': counts['content_rejected'], 'below_threshold': counts['below_threshold'],
            'outdated': counts['outdated'], 'date_pending': counts['date_unavailable'],
            'by_reason': dict(counts),
            'summary': f"本轮处理 {len(entries)} 条：{selected} 条入选，{technical} 条因抓取/模型/引文或复审问题待处理，{counts['content_rejected']} 条内容未通过，{counts['below_threshold']} 条已核验但未达精选标准，{counts['outdated']} 条超出最近一年，{counts['date_unavailable']} 条发布日期待核实。"}
