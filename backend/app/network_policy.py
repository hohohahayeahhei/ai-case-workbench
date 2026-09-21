"""Bounded retry budgets shared by HTTP and its enclosing MCP call."""
import os
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}

def request_timeout(search=False):
    key = 'AI_CASE_SEARCH_TIMEOUT' if search else 'AI_CASE_LLM_TIMEOUT'
    return max(5., min(180., float(os.environ.get(key, '90' if search else '120'))))

def mcp_timeout(tool):
    # Two HTTP attempts plus backoff, initialization, and clean shutdown.
    return request_timeout(True) * 2 + 40 if tool == 'search_web' else 240.

def retry_delay(error=None, attempt=0):
    headers = getattr(error, 'headers', None)
    value = headers.get('Retry-After') if headers else None
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 2 ** attempt + random.uniform(0, 0.5)
    return max(0., min(8., delay))
