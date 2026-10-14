"""Watchers start the herdr server again when it is down, instead of retrying forever."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cos import herdr as herdr_mod
from cos import interactive
from cos.herdr import Herdr, HerdrError


class EnsureServerTest(unittest.TestCase):
    def setUp(self):
        herdr_mod._START_FAILED_AT.clear()
        self.herdr = Herdr(session="neo-test", binary="/nonexistent/herdr")

    def tearDown(self):
        herdr_mod._START_FAILED_AT.clear()

    def test_many_watchers_start_the_server_once(self):
        started = threading.Event()
        calls = []

        def start(_wait):
            calls.append(1)
            started.set()

        errors = []

        def ensure():
            try:
                self.herdr.ensure_server()
            except Exception as exc:  # surfaced by the assertion below
                errors.append(exc)

        with patch.object(Herdr, "running", side_effect=lambda timeout=15: started.is_set()), \
                patch.object(Herdr, "_start_server", side_effect=start):
            threads = [threading.Thread(target=ensure) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
                self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)

    def test_hung_tmux_becomes_a_start_failure(self):
        import subprocess
        with patch.object(Herdr, "running", return_value=False), \
                patch.object(herdr_mod.shutil, "which", return_value="/usr/bin/tmux"), \
                patch.object(herdr_mod.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 10)):
            with self.assertRaises(HerdrError) as ctx:
                self.herdr.ensure_server()
        self.assertEqual(ctx.exception.code, "server_start_failed")
        self.assertIn("neo-test", herdr_mod._START_FAILED_AT)

    def test_failed_start_waits_before_trying_again(self):
        fail = HerdrError("server_start_timeout", "did not start")
        with patch.object(Herdr, "running", return_value=False), \
                patch.object(Herdr, "_start_server", side_effect=fail) as start:
            with self.assertRaises(HerdrError):
                self.herdr.ensure_server()
            with self.assertRaises(HerdrError) as ctx:
                self.herdr.ensure_server()
            self.assertEqual(ctx.exception.code, "server_start_cooldown")
            self.assertEqual(start.call_count, 1)
            herdr_mod._START_FAILED_AT["neo-test"] -= herdr_mod.START_RETRY_SECONDS + 1
            with self.assertRaises(HerdrError):
                self.herdr.ensure_server()
            self.assertEqual(start.call_count, 2)


class WatcherRestartTest(unittest.TestCase):
    def _irun(self, run_dir, herdr):
        irun = object.__new__(interactive.InteractiveRun)
        irun.run = SimpleNamespace(id="run-1", dir=run_dir)
        irun.herdr = herdr
        irun._stop = threading.Event()
        irun._transcript = run_dir / "t.jsonl"
        irun._polled_ok = False
        return irun

    def test_server_not_running_starts_herdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            herdr = MagicMock(session="neo")
            irun = self._irun(Path(tmp), herdr)
            outcomes = [HerdrError("server_not_running", "no herdr server"), True]

            def watch():
                result = outcomes.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

            irun._watch = watch
            with patch.object(interactive, "POLL_SECONDS", 0.001):
                irun._loop()
            herdr.ensure_server.assert_called_once_with()
            self.assertEqual(outcomes, [])

    def test_second_outage_warns_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            herdr = MagicMock(session="neo")
            irun = self._irun(Path(tmp), herdr)
            down = HerdrError("server_not_running", "no herdr server")
            outcomes = [down, "ok", True]

            def watch():
                result = outcomes.pop(0)
                if result == "ok":  # herdr answered once, then went down again
                    irun._polled_ok = True
                    raise down
                if isinstance(result, Exception):
                    raise result
                return result

            irun._watch = watch
            with patch.object(interactive, "POLL_SECONDS", 0.001), \
                    self.assertLogs("cos.interactive", "WARNING") as logs:
                irun._loop()
            self.assertEqual(sum("is down; starting it" in line for line in logs.output), 2)

    def test_other_herdr_errors_do_not_start_herdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            herdr = MagicMock(session="neo")
            irun = self._irun(Path(tmp), herdr)
            outcomes = [HerdrError("timeout", "slow"), True]

            def watch():
                result = outcomes.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

            irun._watch = watch
            with patch.object(interactive, "POLL_SECONDS", 0.001):
                irun._loop()
            herdr.ensure_server.assert_not_called()

    def test_unexpected_start_error_keeps_watching(self):
        with tempfile.TemporaryDirectory() as tmp:
            herdr = MagicMock(session="neo")
            herdr.ensure_server.side_effect = OSError("tmux exploded")
            irun = self._irun(Path(tmp), herdr)
            outcomes = [HerdrError("server_not_running", "no herdr server"), True]

            def watch():
                result = outcomes.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

            irun._watch = watch
            with patch.object(interactive, "POLL_SECONDS", 0.001), self.assertLogs("cos.interactive", "WARNING"):
                irun._loop()
            self.assertEqual(outcomes, [])

    def test_start_failure_keeps_watching(self):
        with tempfile.TemporaryDirectory() as tmp:
            herdr = MagicMock(session="neo")
            herdr.ensure_server.side_effect = HerdrError("server_start_timeout", "did not start")
            irun = self._irun(Path(tmp), herdr)
            down = HerdrError("server_not_running", "no herdr server")
            outcomes = [down, down, True]

            def watch():
                result = outcomes.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

            irun._watch = watch
            with patch.object(interactive, "POLL_SECONDS", 0.001):
                irun._loop()
            self.assertEqual(herdr.ensure_server.call_count, 2)
            self.assertEqual(outcomes, [])


if __name__ == "__main__":
    unittest.main()
