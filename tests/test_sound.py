"""Notification sounds: config gating and non-blocking playback."""
import unittest
from unittest import mock

from codex_autoresume import sound
from codex_autoresume.daemon import Daemon


class SilentThread:
    instances = []

    def __init__(self, target=None, args=(), daemon=None):
        self.args = args
        self.daemon = daemon
        self.started = False
        SilentThread.instances.append(self)

    def start(self):
        self.started = True


class SoundModuleTest(unittest.TestCase):
    def setUp(self):
        SilentThread.instances = []

    def test_empty_spec_is_a_no_op(self):
        with mock.patch.object(sound.threading, "Thread", SilentThread):
            sound.play("")
        self.assertEqual(SilentThread.instances, [])

    def test_non_windows_is_a_no_op(self):
        with mock.patch.object(sound, "_WINDOWS", False), \
             mock.patch.object(sound.threading, "Thread", SilentThread):
            sound.play("exclamation")
        self.assertEqual(SilentThread.instances, [])

    def test_windows_spec_spawns_a_daemon_thread(self):
        with mock.patch.object(sound, "_WINDOWS", True), \
             mock.patch.object(sound.threading, "Thread", SilentThread):
            sound.play("exclamation")
        self.assertEqual(len(SilentThread.instances), 1)
        thread = SilentThread.instances[0]
        self.assertEqual(thread.args, ("exclamation",))
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.started)


class DaemonSoundGatingTest(unittest.TestCase):
    def _daemon(self):
        d = object.__new__(Daemon)
        d.cfg = {"sound": {"enabled": False, "resume": "asterisk",
                           "interrupt": "exclamation"}}
        return d

    def test_disabled_plays_nothing(self):
        d = self._daemon()
        with mock.patch("codex_autoresume.sound.play") as play:
            d._play_sound("resume")
        play.assert_not_called()

    def test_enabled_plays_the_configured_spec(self):
        d = self._daemon()
        d.cfg["sound"]["enabled"] = True
        with mock.patch("codex_autoresume.sound.play") as play:
            d._play_sound("resume")
        play.assert_called_once_with("asterisk")

    def test_empty_spec_plays_nothing(self):
        d = self._daemon()
        d.cfg["sound"] = {"enabled": True, "resume": ""}
        with mock.patch("codex_autoresume.sound.play") as play:
            d._play_sound("resume")
        play.assert_not_called()

    def test_interrupt_alerts_are_throttled(self):
        d = self._daemon()
        d.cfg["sound"]["enabled"] = True
        with mock.patch("codex_autoresume.sound.play") as play:
            d._alert_quota_interrupt()
            d._alert_quota_interrupt()
            d._alert_quota_interrupt()
        self.assertEqual(play.call_count, 1)
        play.assert_called_once_with("exclamation")


if __name__ == "__main__":
    unittest.main()
