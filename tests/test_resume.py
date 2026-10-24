import unittest

from codex_autoresume.resume import (build_exec_resume_command,
                                     build_probe_command, build_queue_command,
                                     classify_output)


class TestCommandBuild(unittest.TestCase):
    def test_exec_resume_full(self):
        argv = build_exec_resume_command(
            "codex", "TID", "continue", model="gpt-6.1-sol",
            reasoning_effort="high", extra_config=["model_verbosity=low"],
            codex_extra_args=["--flag"], cwd="C:/w")
        self.assertEqual(argv[:6], ["codex", "exec", "resume", "TID",
                                    "continue", "--skip-git-repo-check"])
        self.assertIn("gpt-6.1-sol", argv)
        self.assertIn("model_reasoning_effort=high", argv)
        self.assertIn("model_verbosity=low", argv)
        self.assertIn("--flag", argv)
        self.assertIn("C:/w", argv)

    def test_exec_resume_minimal(self):
        argv = build_exec_resume_command("codex", "TID", "p")
        self.assertEqual(argv, ["codex", "exec", "resume", "TID", "p",
                                "--skip-git-repo-check"])

    def test_queue(self):
        argv = build_queue_command("codex", "TID", "msg")
        self.assertEqual(argv, ["codex", "queue", "--thread", "TID",
                                "--message", "msg"])

    def test_probe(self):
        argv = build_probe_command("codex", "TID", model="m")
        self.assertIn("model_reasoning_effort=none", argv)
        self.assertIn("OK", " ".join(argv))


class TestClassify(unittest.TestCase):
    def test_success(self):
        out = classify_output(0, "done", "")
        self.assertTrue(out.ok)
        self.assertEqual(out.kind, "success")

    def test_usage_limit_with_time(self):
        out = classify_output(
            1, "",
            'Error: stream error: You\u2019ve hit your usage limit. '
            'Upgrade to Pro or try again at 1:51 PM.')
        self.assertEqual(out.kind, "usage_limit")
        self.assertIsNotNone(out.reset_at_iso)

    def test_usage_limit_code_info(self):
        out = classify_output(
            1, '{"codexErrorInfo":"usageLimitExceeded","message":"..."}', "")
        self.assertEqual(out.kind, "usage_limit")

    def test_overloaded(self):
        out = classify_output(1, "", "Error: Selected model is at capacity.")
        self.assertEqual(out.kind, "overloaded")

    def test_archived(self):
        out = classify_output(
            1, "", "Error: thread/resume: session X is archived. Run codex unarchive")
        self.assertEqual(out.kind, "archived")

    def test_no_rollout(self):
        out = classify_output(1, "", "Error: no rollout found for thread id X")
        self.assertEqual(out.kind, "no_rollout")

    def test_writer_conflict(self):
        out = classify_output(
            1, "",
            "Error: thread/resume: thread 01a05ddb already has an active writer (code -32600)")
        self.assertEqual(out.kind, "writer_conflict")

    def test_other(self):
        out = classify_output(1, "", "Error: something else")
        self.assertEqual(out.kind, "other_error")
        self.assertFalse(out.ok)


if __name__ == "__main__":
    unittest.main()
