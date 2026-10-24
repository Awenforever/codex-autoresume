import os
import tempfile
import unittest
from datetime import datetime, timedelta

from codex_autoresume.state import (InstanceLock, ThreadState, WatcherState,
                                    load_state, save_state)


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        d = tempfile.mkdtemp(prefix="cxr-state-")
        path = os.path.join(d, "state.json")
        st = WatcherState(state_path=path)
        t = st.thread("t1")
        t.tracked = True
        t.is_goal = True
        t.append_history({"at": "now", "kind": "success"})
        st.seen_sources.add("turn:x")
        st.turns_watermark = 42
        save_state(st)
        st2 = load_state(path)
        self.assertTrue(st2.threads["t1"].tracked)
        self.assertTrue(st2.threads["t1"].is_goal)
        self.assertEqual(st2.threads["t1"].history[0]["kind"], "success")
        self.assertIn("turn:x", st2.seen_sources)
        self.assertEqual(st2.turns_watermark, 42)

    def test_counters(self):
        t = ThreadState(resume_count_day="2026-10-24", resume_count_today=2)
        self.assertTrue(t.bump_resume_counter("2026-10-24", cap=3))
        self.assertFalse(t.bump_resume_counter("2026-10-24", cap=3))
        self.assertTrue(t.bump_resume_counter("2026-10-25", cap=3))
        self.assertEqual(t.resume_count_today, 1)

    def test_history_cap(self):
        t = ThreadState()
        for i in range(60):
            t.append_history({"i": i})
        self.assertEqual(len(t.history), 50)
        self.assertEqual(t.history[-1]["i"], 59)

    def test_lock(self):
        d = tempfile.mkdtemp(prefix="cxr-lock-")
        lock = InstanceLock(os.path.join(d, "daemon.lock"))
        self.assertTrue(lock.acquire())
        lock2 = InstanceLock(os.path.join(d, "daemon.lock"))
        self.assertFalse(lock2.acquire())
        lock.release()
        self.assertTrue(lock2.acquire())
        lock2.release()

    def test_lock_stale_pid_reaped(self):
        d = tempfile.mkdtemp(prefix="cxr-lock-")
        p = os.path.join(d, "daemon.lock")
        os.makedirs(d, exist_ok=True)
        with open(p, "w") as f:
            f.write("99999999")          # definitely dead pid
        os.utime(p, (0, 0))              # ancient mtime
        lock = InstanceLock(p)
        self.assertTrue(lock.acquire())
        lock.release()


if __name__ == "__main__":
    unittest.main()
