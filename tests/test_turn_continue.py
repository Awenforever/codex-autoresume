"""Continue a stopped turn: the RPC twin of the app's Continue button.

Continue is turn/start with an EMPTY input on an idle thread -- never a
prompt injection. Writer conflicts must map to TurnBusyError so the daemon
can fall back to clicking the app's own button.
"""
import unittest
from unittest import mock

import codex_autoresume.ratelimits as rl


class _Fake:
    calls = []
    turn_error = None
    resume_error = None
    turn_result = {"turn": {"id": "turn-new", "status": "inProgress"}}

    def __init__(self, codex_bin):
        pass

    def _rpc(self, method, params=None, timeout=None):
        _Fake.calls.append((method, params))
        if method == "thread/resume" and _Fake.resume_error:
            raise rl.RateLimitError(_Fake.resume_error)
        if method == "turn/start" and _Fake.turn_error:
            raise rl.RateLimitError(_Fake.turn_error)
        if method == "turn/start":
            return _Fake.turn_result
        return {}

    def close(self):
        pass


def _run(**kw):
    _Fake.calls = []
    _Fake.resume_error = kw.get("resume_error")
    _Fake.turn_error = kw.get("turn_error")
    with mock.patch.object(rl, "AppServerClient", _Fake):
        return rl.turn_continue("codex.exe", "t1")


class TurnContinueTest(unittest.TestCase):
    def test_continue_sends_empty_input(self):
        res = _run()
        self.assertEqual(res, _Fake.turn_result)
        self.assertEqual([c[0] for c in _Fake.calls],
                         ["thread/resume", "turn/start"])
        self.assertEqual(_Fake.calls[0][1], {"threadId": "t1"})
        # the app's Continue button injects nothing; so do we
        self.assertEqual(_Fake.calls[1][1], {"threadId": "t1", "input": []})

    def test_writer_conflict_on_turn_start_is_busy(self):
        with self.assertRaises(rl.TurnBusyError):
            _run(turn_error="turn/start failed: thread t1 already has an "
                            "active writer")

    def test_writer_conflict_on_resume_is_busy(self):
        with self.assertRaises(rl.TurnBusyError):
            _run(resume_error="thread/resume failed: thread is busy")

    def test_quota_error_stays_rate_limit(self):
        with self.assertRaises(rl.RateLimitError) as cm:
            _run(turn_error="turn/start failed: usage limit reached")
        self.assertNotIsInstance(cm.exception, rl.TurnBusyError)


if __name__ == "__main__":
    unittest.main()
