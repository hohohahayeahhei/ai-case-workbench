"""Notification integration must preserve offline safety and collection backups."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_collection


class NotificationCLITests(unittest.TestCase):
    def run_cli(self, argv, pipeline, **overrides):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(run_collection, 'runtime_dir', return_value=Path(directory)), \
                patch.object(run_collection, 'CollectionPipeline', return_value=pipeline), \
                patch.object(run_collection, 'export_collection', return_value={}), \
                patch.object(run_collection, 'retry_pending', return_value=[]) as retry, \
                patch.object(run_collection, 'notify_collection', **overrides) as notify, \
                patch('sys.argv', ['run_collection.py', *argv]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = 0
            try:
                run_collection.main()
            except SystemExit as exc:
                exit_code = exc.code
            return json.loads(output.getvalue()), exit_code, notify, retry

    def test_manual_collection_does_not_send_when_env_is_enabled(self):
        pipeline = Mock()
        pipeline.run.return_value = {'run_id': 'manual', 'status': 'completed', 'items': []}
        with patch.dict(os.environ, {'AI_CASE_NOTIFICATIONS_ENABLED': 'true'}):
            _, code, notify, retry = self.run_cli([], pipeline)
        self.assertEqual(code, 0)
        notify.assert_not_called()
        retry.assert_not_called()

    def test_partial_run_notifies_only_after_result_is_saved(self):
        pipeline = Mock()
        pipeline.run.return_value = {'run_id': 'partial', 'status': 'partial', 'items': []}
        def send(db, result):
            pipeline.db.save_collection_run.assert_called()
            self.assertEqual(result['status'], 'partial')
            return {'status': 'sent'}
        result, code, notify, _ = self.run_cli(['--notify'], pipeline, side_effect=send)
        self.assertEqual(code, 0)
        self.assertEqual(result['notification']['status'], 'sent')
        notify.assert_called_once()
        pipeline.close.assert_called_once()

    def test_notification_exception_does_not_fail_collection(self):
        pipeline = Mock()
        pipeline.run.return_value = {'run_id': 'ok', 'status': 'completed', 'items': []}
        result, code, _, _ = self.run_cli(['--notify'], pipeline, side_effect=RuntimeError('secret-url'))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('secret-url', json.dumps(result))

    def test_collection_exception_still_notifies_failure(self):
        pipeline = Mock()
        pipeline.run.side_effect = RuntimeError('private-details')
        pipeline.db.collection_run.return_value = None
        result, code, notify, _ = self.run_cli(['--notify'], pipeline, return_value={'status': 'sent'})
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('private-details', json.dumps(result))
        notify.assert_called_once()

    def test_fixture_cannot_opt_into_notifications(self):
        with patch('sys.argv', ['run_collection.py', '--source-mode', 'fixture', '--notify']), \
                patch.object(run_collection, 'CollectionPipeline') as pipeline, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                run_collection.main()
        self.assertEqual(raised.exception.code, 2)
        pipeline.assert_not_called()

    def test_daily_backs_up_after_collection_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / 'python-test'
            calls = Path(directory) / 'calls'
            runner.write_text('#!/bin/bash\nprintf "%s\\n" "$1" >> "$TEST_CALLS"\n'
                              'if [[ "$1" == scripts/run_collection.py ]]; then exit 7; fi\n')
            runner.chmod(0o700)
            result = subprocess.run(['bash', str(run_collection.ROOT / 'scripts/daily_collection.sh')],
                                    env={**os.environ, 'PYTHON_BIN': str(runner), 'TEST_CALLS': str(calls)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(calls.read_text().splitlines(),
                             ['scripts/run_collection.py', 'scripts/backup_catalog.py'])


if __name__ == '__main__':
    unittest.main()
