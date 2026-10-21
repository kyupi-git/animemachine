import os
import unittest
from pathlib import Path
from unittest import mock

from animemachine.catalog import service
from animemachine.integrations import qbt_runtime


class OptionalDownloaderTests(unittest.TestCase):
    def monitor(self, *, until=30, secret=lambda _now: '', responses=None, enabled=True):
        clock = [0.0]
        stop = mock.Mock()
        stop.is_set.side_effect = lambda: clock[0] >= until
        def wait(seconds):
            clock[0] += seconds
            return clock[0] >= until
        stop.wait.side_effect = wait
        store = mock.Mock()
        store.read.return_value = {'components': {'downloadClient': {
            'endpoint':'http://127.0.0.1:8080','category':'anm','submissionEnabled':enabled}}}
        calls = []
        def refresh(*_args):
            calls.append(clock[0])
            result = next(responses) if responses is not None else {'completedAnimeIds':[]}
            if isinstance(result, Exception):
                raise result
            return result
        callback = mock.Mock()
        with mock.patch.dict(os.environ, {'ANM_QBT_ALLOW_AUTH_BYPASS':'false'}), \
                mock.patch.object(qbt_runtime, '_secret', side_effect=lambda: secret(clock[0])), \
                mock.patch.object(qbt_runtime, 'refresh', side_effect=refresh), \
                mock.patch.object(service.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(service, 'log_event') as logs:
            service._monitor_qbt(Path('unused.sqlite3'), store, stop, callback, submission_enabled=True)
        return calls, callback, logs

    def test_fresh_template_does_not_poll_or_warn_about_absent_downloader(self):
        calls, health, logs = self.monitor()
        self.assertEqual([],calls)
        health.assert_called_with('qbittorrent',status='normal',detail='not_configured')
        logs.assert_not_called()

    def test_disabled_integration_does_not_connect_even_with_credentials(self):
        def inaccessible(_now):
            self.fail('Do not read credentials for a disabled downloader')
        calls, health, logs = self.monitor(secret=inaccessible,enabled=False)
        self.assertEqual([],calls)
        health.assert_called_with('qbittorrent',status='normal',detail='disabled')
        logs.assert_not_called()

    def test_unavailable_configured_service_backs_off_then_recovers(self):
        calls, health, logs = self.monitor(until=220,secret=lambda _: 'configured',
            responses=iter([ConnectionError('refused')]*3+[{'completedAnimeIds':[]}]))
        self.assertEqual([0,30,90,210],calls)
        health.assert_called_with('qbittorrent',status='normal',detail='ready')
        self.assertEqual(1,logs.call_count)
        self.assertEqual('WARNING',logs.call_args.args[0])

    def test_entering_or_correcting_credentials_retries_without_waiting_for_backoff(self):
        calls, health, _logs = self.monitor(until=40,
            secret=lambda now: '' if now<20 else 'first' if now<30 else 'corrected',
            responses=iter([ConnectionError('refused'),{'completedAnimeIds':[]}]))
        self.assertEqual([20,30],calls)
        health.assert_called_with('qbittorrent',status='normal',detail='ready')

    def test_authentication_bypass_requires_explicit_opt_in(self):
        cfg = {'components':{'downloadClient':{'endpoint':'http://localhost:8080'}}}
        with mock.patch.object(qbt_runtime,'_secret',return_value=''), mock.patch.dict(os.environ,{'ANM_QBT_ALLOW_AUTH_BYPASS':'false'}):
            self.assertFalse(qbt_runtime.configured(cfg))
        with mock.patch.object(qbt_runtime,'_secret',return_value=''), mock.patch.dict(os.environ,{'ANM_QBT_ALLOW_AUTH_BYPASS':'true'}):
            self.assertTrue(qbt_runtime.configured(cfg))


if __name__ == '__main__':
    unittest.main()
