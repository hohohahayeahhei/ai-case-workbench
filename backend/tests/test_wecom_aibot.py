import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import notifications as n
from backend.app import wecom_aibot as bot
from backend.app.db import Database


class FakeSocket:
    def __init__(self, frames=None, *, auth_code=0, send_code=0, fail_auth=False, fail_send=False):
        self.frames = list(frames or [])
        self.sent = []
        self.auth_code = auth_code
        self.send_code = send_code
        self.fail_auth = fail_auth
        self.fail_send = fail_send

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, timeout):
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, Exception):
                raise item
            return json.dumps(item)
        last = self.sent[-1]
        if ((last['cmd'] == 'aibot_subscribe' and self.fail_auth)
                or (last['cmd'] == 'aibot_send_msg' and self.fail_send)):
            raise TimeoutError('must-not-log-secret')
        return json.dumps({'headers': last['headers'],
                           'errcode': self.auth_code if last['cmd'] == 'aibot_subscribe' else self.send_code,
                           'errmsg': 'must-not-log-secret'})


class AIBotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = bot.BotConfig('test-bot-123', 'must-not-log-secret', 'test-user')
        environment = {'AI_CASE_NOTIFICATIONS_ENABLED': 'true', 'AI_CASE_NOTIFICATION_PROVIDER': 'wecom_aibot',
                       'AI_CASE_WECOM_BOT_ID': self.config.bot_id,
                       'AI_CASE_WECOM_BOT_SECRET': self.config.secret,
                       'AI_CASE_WECOM_USER_ID': self.config.user_id,
                       'AI_CASE_DATA_DIR': str(self.directory)}
        self.env = patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        loader = patch.object(n, 'load_local_env')
        loader.start()
        self.addCleanup(loader.stop)
        self.db = Database(self.directory / 'catalog.db')
        self.db.init_schema()
        self.addCleanup(self.db.close)

    def notify(self, run='test-run'):
        return n.notify_collection(self.db, {'run_id': run, 'status': 'completed', 'items': []})

    def test_sender_subscribes_then_sends_matching_ack(self):
        ws = FakeSocket(frames=[{'headers': {'req_id': 'other-request'}, 'errcode': 400}])
        with patch.object(bot, '_connect', return_value=ws):
            bot.send_markdown(self.config, '测试正文')
        self.assertEqual([message['cmd'] for message in ws.sent], ['aibot_subscribe', 'aibot_send_msg'])
        self.assertEqual(ws.sent[0]['body'], {'bot_id': self.config.bot_id, 'secret': self.config.secret})
        self.assertEqual(ws.sent[1]['body'], {'chatid': 'test-user', 'chat_type': 1,
                                           'msgtype': 'markdown', 'markdown': {'content': '测试正文'}})
        self.assertNotEqual(ws.sent[0]['headers']['req_id'], ws.sent[1]['headers']['req_id'])
        self.assertNotIn('must-not-log-secret', repr(self.config))

    def test_connect_and_auth_failures_are_safely_retryable(self):
        for ws in [FakeSocket(auth_code=40001), FakeSocket(fail_auth=True)]:
            with self.subTest(ws=ws), patch.object(bot, '_connect', return_value=ws):
                with self.assertRaises(bot.AIBotError) as caught:
                    bot.send_markdown(self.config, '正文')
                self.assertFalse(caught.exception.unknown)
                self.assertEqual(len(ws.sent), 1)
                self.assertNotIn('must-not-log-secret', str(caught.exception))
        with patch.object(bot, '_connect', side_effect=OSError('must-not-log-secret')):
            self.assertEqual(self.notify()['error'], 'aibot_connection_failed')

    def test_delivery_timeout_unknown_but_explicit_send_ack_failed(self):
        with patch.object(bot, '_connect', return_value=FakeSocket(fail_send=True)):
            result = self.notify()
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(n.retry_pending(self.db), [])
        with patch.object(bot, '_connect', return_value=FakeSocket(send_code=45009)):
            result = self.notify('reject-run')
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error'], 'aibot_platform_error_45009')
        rows = json.dumps([dict(row) for row in self.db.connection.execute('SELECT * FROM notification_outbox')])
        self.assertNotIn('must-not-log-secret', rows)
        self.assertNotIn('test-user', rows)

    def test_outbox_dedupes_same_run_after_secret_rotation(self):
        with patch.object(bot, '_connect', return_value=FakeSocket()) as connect:
            self.assertEqual(self.notify()['status'], 'sent')
            with patch.dict(os.environ, {'AI_CASE_WECOM_BOT_SECRET': 'rotated-secret'}):
                self.assertEqual(self.notify()['status'], 'sent')
            connect.assert_called_once()
            preview = n.preview_collection(self.db, {'run_id': 'test-run', 'status': 'completed', 'items': []})
            self.assertIn('今日暂无', preview['content'])

    def test_bot_lock_blocks_second_session_and_listener(self):
        with bot._bot_lock(self.config), patch.object(bot, '_connect') as connect:
            result = self.notify()
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error'], 'aibot_connection_busy')
            result = bot.listen_for_binding(self.config, self.directory / 'binding.json', '绑定日报1234')
            self.assertEqual(result['error'], 'aibot_connection_busy')
            connect.assert_not_called()

    def test_binding_accepts_exact_private_text_only(self):
        def message(**changes):
            body = {'chattype': 'single', 'msgtype': 'text', 'aibotid': self.config.bot_id,
                    'from': {'userid': 'bound-user'}, 'text': {'content': '绑定日报1234'}}
            body.update(changes)
            return {'cmd': 'aibot_msg_callback', 'body': body}
        for frame in [message(chattype='group'), message(msgtype='image'),
                      message(text={'content': '绑定日报12345'}), message(text={'content': None}),
                      message(aibotid='other-bot'),
                      {'cmd': 'aibot_event_callback', 'body': message()['body']}]:
            self.assertIsNone(bot._binding_user(frame, '绑定日报1234', self.config.bot_id))
        self.assertEqual(bot._binding_user(message(), '绑定日报1234', self.config.bot_id), 'bound-user')
        self.assertEqual(bot._binding_user(message(**{'from': {'userid': 'encrypted+/user='}}),
                                          '绑定日报1234', self.config.bot_id), 'encrypted+/user=')
        ws = FakeSocket()
        # Add callback frames only after subscribe has received its ACK.
        ready = Mock(side_effect=lambda: ws.frames.extend([message(chattype='group'), message()]))
        path = self.directory / 'binding.json'
        with patch.object(bot, '_connect', return_value=ws):
            output = bot.listen_for_binding(self.config, path, '绑定日报1234', on_ready=ready)
        self.assertEqual(output['status'], 'bound')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {'bot_id': self.config.bot_id, 'user_id': 'bound-user'})
        self.assertNotIn('must-not-log-secret', path.read_text())
        self.assertNotIn('bound-user', json.dumps(output))
        self.assertEqual([x['cmd'] for x in ws.sent], ['aibot_subscribe'])

    def test_configuration_binding_matches_bot_and_readiness_redacts_identity(self):
        os.environ.pop('AI_CASE_WECOM_USER_ID')
        self.assertEqual(n.notification_configuration(self.directory)['error'], 'aibot_recipient_not_bound')
        bot._save_binding(bot.binding_path(self.directory), self.config.bot_id, 'bound-user')
        self.assertEqual(bot.load_bot_config(self.directory).user_id, 'bound-user')
        output = n.notification_configuration(self.directory)
        self.assertTrue(output['configured'])
        self.assertEqual(output['provider'], 'wecom_aibot')
        self.assertNotIn('bound-user', json.dumps(output))
        self.assertNotIn(self.config.secret, json.dumps(output))
        bot._save_binding(bot.binding_path(self.directory), 'other-bot', 'bound-user')
        self.assertEqual(n.notification_configuration(self.directory)['error'], 'aibot_recipient_not_bound')


if __name__ == '__main__':
    unittest.main()
