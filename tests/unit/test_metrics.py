import contextlib
import io
import unittest
from unittest import mock

from animemachine import metrics
from animemachine.catalog import service


class ConsoleTests(unittest.TestCase):
    def test_noninteractive_progress_keeps_first_and_final_results(self):
        stream = io.StringIO()
        with mock.patch.object(metrics.time, 'monotonic', return_value=100):
            metrics.progress('started', stream=stream)
            for number in range(100):
                metrics.progress(f'processed {number}', stream=stream)
            metrics.progress('finished', final=True, stream=stream)
        self.assertEqual(['started', 'finished'], stream.getvalue().splitlines())

    def test_phase_end_flushes_the_latest_progress_and_debug_timings_are_opt_in(self):
        stream = io.StringIO()
        with mock.patch.object(metrics.time, 'monotonic', return_value=100):
            metrics.progress('started', stream=stream)
            metrics.progress('last result', stream=stream)
            metrics.end_progress(stream)
        self.assertEqual(['started', 'last result'], stream.getvalue().splitlines())
        with mock.patch.dict('os.environ', {'ANM_LOG_LEVEL': ''}), contextlib.redirect_stdout(io.StringIO()) as output:
            with metrics.stage('test'):
                pass
        self.assertEqual('', output.getvalue())
        with mock.patch.dict('os.environ', {'ANM_LOG_LEVEL': 'DEBUG'}), contextlib.redirect_stdout(io.StringIO()) as output:
            with metrics.stage('test'):
                pass
        self.assertIn('[timing] test=', output.getvalue())

    def test_repeated_warnings_stay_in_web_logs_without_flooding_console(self):
        with mock.patch.dict(service._CONSOLE_LAST, {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as output:
            for _ in range(3):
                service.log_event('WARNING', 'test_connection_failed', secret='do-not-print-this')
        self.assertEqual(1, len(output.getvalue().splitlines()))
        self.assertNotIn('do-not-print-this', output.getvalue())
        self.assertIn('do-not-print-this', service.RECENT_LOGS[0]['details'])

    def test_each_manual_completion_is_printed(self):
        with mock.patch.dict(service._CONSOLE_LAST, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            for _ in range(2):
                service.log_event('INFO', 'library_placeholders_complete', created=3)
        self.assertEqual(2, len(output.getvalue().splitlines()))
        self.assertIn('created=3', output.getvalue())
