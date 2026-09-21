"""Short-lived, serialized WeCom smart-bot WebSocket sessions.

Credentials only live in memory. Subscription failure is safely retryable;
loss of the send acknowledgement is unknown and must not be blindly retried.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

ENDPOINT = 'wss://openws.work.weixin.qq.com'
TIMEOUT = 12
_LOGGER = logging.getLogger(__name__ + '.wire')
_LOGGER.disabled = True


class AIBotError(Exception):
    def __init__(self, code: str, *, unknown: bool = False):
        self.code = code
        self.unknown = unknown
        super().__init__(code)


@dataclass(frozen=True)
class BotConfig:
    bot_id: str = field(repr=False)
    secret: str = field(repr=False)
    user_id: str = field(default='', repr=False)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_@.:-]{1,256}', value))


def _user_identifier(value: object) -> bool:
    # Non-admin bots receive opaque encrypted user IDs; their alphabet is not
    # specified. JSON serialization safely handles punctuation in those IDs.
    return (isinstance(value, str) and 1 <= len(value) <= 1024
            and not re.search(r'[\s\x00-\x1f\x7f]', value))


def binding_path(data_dir: str | Path) -> Path:
    return Path(os.environ.get('AI_CASE_WECOM_BINDING_FILE') or Path(data_dir) / 'wecom-aibot-binding.json')


def load_bot_config(data_dir: str | Path, *, require_user: bool = True) -> BotConfig:
    """Read already-loaded environment plus an optional recipient binding file."""
    bot_id = os.environ.get('AI_CASE_WECOM_BOT_ID', '').strip()
    secret = os.environ.get('AI_CASE_WECOM_BOT_SECRET', '').strip()
    user_id = os.environ.get('AI_CASE_WECOM_USER_ID', '').strip()
    if not _identifier(bot_id) or not secret or len(secret) > 1024 or re.search(r'[\s\x00-\x1f]', secret):
        raise AIBotError('invalid_aibot_configuration')
    if require_user and not user_id:
        try:
            path = binding_path(data_dir)
            if path.stat().st_size > 8192:
                raise ValueError()
            binding = json.loads(path.read_text(encoding='utf-8'))
            if binding.get('bot_id') != bot_id:
                raise ValueError()
            user_id = binding['user_id']
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            raise AIBotError('aibot_recipient_not_bound') from None
    if (require_user and not _user_identifier(user_id)) or (user_id and not _user_identifier(user_id)):
        raise AIBotError('invalid_aibot_recipient')
    return BotConfig(bot_id, secret, user_id)


def target_id(config: BotConfig) -> str:
    # A rotated secret does not cause the same digest to be delivered twice.
    return hashlib.sha256(('wecom_aibot\0' + config.bot_id + '\0' + config.user_id).encode()).hexdigest()


@contextmanager
def _bot_lock(config: BotConfig):
    token = hashlib.sha256(config.bot_id.encode()).hexdigest()[:32]
    path = Path(tempfile.gettempdir()) / f'ai-case-wecom-{os.getuid()}-{token}.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AIBotError('aibot_connection_busy') from None
        yield
    finally:
        os.close(fd)


def _connect():
    try:
        from websockets.sync.client import connect
    except ImportError:
        raise AIBotError('aibot_dependency_missing') from None
    return connect(ENDPOINT, open_timeout=TIMEOUT, close_timeout=2,
                   ping_interval=20, ping_timeout=TIMEOUT, max_size=65536, logger=_LOGGER)


def _send(ws, command: str, body: dict | None = None) -> str:
    request_id = uuid4().hex
    message = {'cmd': command, 'headers': {'req_id': request_id}}
    if body is not None:
        message['body'] = body
    ws.send(json.dumps(message, ensure_ascii=False))
    return request_id


def _receive(ws, timeout: float) -> dict:
    payload = ws.recv(timeout=max(0.001, timeout))
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError('invalid_frame')
    return result


def _ack(ws, request_id: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        message = _receive(ws, deadline - time.monotonic())
        if message.get('headers', {}).get('req_id') != request_id:
            continue
        code = message.get('errcode')
        if type(code) is not int:
            raise ValueError('invalid_ack')
        if code != 0:
            raise AIBotError(f'aibot_platform_error_{code}')
        return
    raise TimeoutError()


def _subscribe(ws, config: BotConfig) -> None:
    request_id = _send(ws, 'aibot_subscribe', {'bot_id': config.bot_id, 'secret': config.secret})
    _ack(ws, request_id)


def send_markdown(config: BotConfig, content: str) -> None:
    send_started = False
    acknowledged = False
    try:
        with _bot_lock(config), _connect() as ws:
            _subscribe(ws, config)
            send_started = True
            request_id = _send(ws, 'aibot_send_msg', {
                'chatid': config.user_id, 'chat_type': 1,
                'msgtype': 'markdown', 'markdown': {'content': content},
            })
            _ack(ws, request_id)
            acknowledged = True
    except AIBotError:
        raise
    except Exception:
        if acknowledged:
            return  # A clean ACK remains authoritative if closing fails.
        raise AIBotError('aibot_delivery_unknown' if send_started else 'aibot_connection_failed',
                         unknown=send_started) from None


def _binding_user(message: dict, phrase: str, bot_id: str) -> str | None:
    if message.get('cmd') != 'aibot_msg_callback':
        return None
    body = message.get('body')
    if not isinstance(body, dict) or body.get('chattype') != 'single' or body.get('msgtype') != 'text':
        return None
    if body.get('aibotid') not in {None, bot_id}:
        return None
    text = body.get('text', {})
    source = body.get('from', {})
    if not isinstance(text, dict) or not isinstance(source, dict):
        return None
    content = text.get('content')
    if not isinstance(content, str) or content.strip() != phrase:
        return None
    user_id = source.get('userid')
    return user_id if _user_identifier(user_id) else None


def _save_binding(path: Path, bot_id: str, user_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.wecom-binding-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'bot_id': bot_id, 'user_id': user_id}, stream, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def listen_for_binding(config: BotConfig, path: Path, phrase: str, *, timeout: float = 90,
                       on_ready=None) -> dict:
    """Bind only an exact one-time phrase in a direct text message; never reply."""
    if not isinstance(phrase, str) or not 4 <= len(phrase.strip()) <= 128:
        return {'status': 'failed', 'error': 'invalid_binding_phrase'}
    timeout = max(1, min(120, timeout))
    try:
        with _bot_lock(config), _connect() as ws:
            _subscribe(ws, config)
            if on_ready:
                on_ready()
            deadline = time.monotonic() + timeout
            heartbeat = time.monotonic() + 30
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= heartbeat:
                    _send(ws, 'ping')
                    heartbeat = now + 30
                try:
                    message = _receive(ws, min(deadline, heartbeat) - now)
                except TimeoutError:
                    continue
                user_id = _binding_user(message, phrase.strip(), config.bot_id)
                if user_id:
                    _save_binding(path, config.bot_id, user_id)
                    return {'status': 'bound', 'binding_file': str(path), 'permissions': '0600'}
        return {'status': 'failed', 'error': 'binding_timed_out'}
    except AIBotError as exc:
        return {'status': 'failed', 'error': exc.code}
    except Exception:
        return {'status': 'failed', 'error': 'binding_connection_failed'}
