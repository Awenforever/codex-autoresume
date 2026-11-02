"""Windows runner: clear schtasks' default 72h execution cap."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_autoresume import installer


def _cp(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class TaskLimitTest(unittest.TestCase):
    def test_clears_or_reports_unlimited(self):
        with mock.patch.object(installer, "_powershell",
                               return_value=_cp("OK\n")):
            self.assertIs(installer._ensure_task_unlimited_nt(), True)

    def test_absent_task(self):
        with mock.patch.object(installer, "_powershell",
                               return_value=_cp("ABSENT\n")):
            self.assertIsNone(installer._ensure_task_unlimited_nt())

    def test_failure_token(self):
        with mock.patch.object(installer, "_powershell",
                               return_value=_cp("FAIL:PT72H\n")):
            self.assertIs(installer._ensure_task_unlimited_nt(), False)

    def test_powershell_error(self):
        with mock.patch.object(installer, "_powershell",
                               side_effect=OSError("no powershell")):
            self.assertIs(installer._ensure_task_unlimited_nt(), False)

    def test_read_limit(self):
        with mock.patch.object(installer, "_powershell",
                               return_value=_cp("PT72H\n")):
            self.assertEqual(installer._task_time_limit_nt(), "PT72H")

    def test_read_absent(self):
        with mock.patch.object(installer, "_powershell",
                               return_value=_cp("ABSENT\n")):
            self.assertIsNone(installer._task_time_limit_nt())

    def test_script_zeroes_the_cap(self):
        script = installer._TASK_LIMIT_PS % ((installer.TASK_NAME,) * 3)
        self.assertIn("ExecutionTimeLimit", script)
        self.assertIn("PT0S", script)
        self.assertIn(installer.TASK_NAME, script)


class RepairGateTest(unittest.TestCase):
    def test_canonical_instance_repairs(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("codex_autoresume.config.default_state_dir",
                           return_value=d), \
                mock.patch.object(installer, "ensure_task_unlimited",
                                  return_value=True) as ensure:
            self.assertIs(installer.repair_runner_limit(d), True)
            ensure.assert_called_once_with()

    def test_sandbox_instance_does_not_touch_the_task(self):
        with tempfile.TemporaryDirectory() as d, \
                tempfile.TemporaryDirectory() as other, \
                mock.patch("codex_autoresume.config.default_state_dir",
                           return_value=other), \
                mock.patch.object(installer, "ensure_task_unlimited") as ensure:
            self.assertIsNone(installer.repair_runner_limit(d))
            ensure.assert_not_called()


class WindowsInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sd = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _install(self, create_rc=0, uncap=True, shortcut=True):
        calls = []

        def fake_run(argv, *a, **kw):
            calls.append(list(argv))
            if list(argv[:2]) == ["schtasks", "/Create"]:
                return _cp(returncode=create_rc)
            return _cp()

        with mock.patch.object(
                installer, "_write_launcher",
                return_value=Path(self.sd) / "daemon_launcher.py"), \
                mock.patch.object(installer, "_pythonw",
                                  return_value="pythonw.exe"), \
                mock.patch.object(installer, "_start_detached") as detached, \
                mock.patch.object(installer, "_create_startup_shortcut",
                                  return_value=shortcut) as lnk, \
                mock.patch.object(installer, "ensure_task_unlimited",
                                  return_value=uncap) as ensure, \
                mock.patch("codex_autoresume.installer.subprocess.run",
                           side_effect=fake_run):
            msg = installer._install_windows(self.sd)
        return msg, calls, detached, lnk, ensure

    def test_success_clears_cap_before_start(self):
        msg, calls, detached, lnk, _ = self._install()
        self.assertIn("72h execution cap cleared", msg)
        self.assertIn(["schtasks", "/Run", "/TN", installer.TASK_NAME], calls)
        self.assertNotIn(["schtasks", "/Delete", "/TN", installer.TASK_NAME,
                          "/F"], calls)
        detached.assert_not_called()
        lnk.assert_not_called()

    def test_uncappable_task_falls_back_to_startup_shortcut(self):
        msg, calls, detached, lnk, _ = self._install(uncap=False)
        self.assertIn("Startup shortcut", msg)
        self.assertIn(["schtasks", "/Delete", "/TN", installer.TASK_NAME,
                       "/F"], calls)
        detached.assert_called_once()
        lnk.assert_called_once()

    def test_both_fallbacks_fail_recreates_task_and_warns(self):
        msg, calls, detached, lnk, _ = self._install(uncap=False, shortcut=False)
        self.assertIn("re-run `codex-autoresume install`", msg)
        self.assertEqual(
            len([c for c in calls if c[:2] == ["schtasks", "/Create"]]), 2)
        self.assertIn(["schtasks", "/Run", "/TN", installer.TASK_NAME], calls)
        detached.assert_called_once()
        lnk.assert_called_once()

    def test_denied_task_never_probes_the_cap(self):
        msg, calls, detached, lnk, ensure = self._install(create_rc=1)
        self.assertIn("denied", msg)
        ensure.assert_not_called()
        detached.assert_called_once()
        lnk.assert_called_once()


if __name__ == "__main__":
    unittest.main()
