"""Daemon self-heals a codex path removed by a desktop-app update."""

import copy
import os
import tempfile
import unittest
from unittest import mock

from codex_autoresume import daemon as daemon_mod
from codex_autoresume.config import DEFAULTS
from codex_autoresume.daemon import Daemon


def _stub(codex_bin):
    d = object.__new__(Daemon)
    d.cfg = copy.deepcopy(DEFAULTS)
    d.codex_bin = codex_bin
    return d


class TestCodexBinRefresh(unittest.TestCase):
    def test_existing_binary_is_kept(self):
        root = tempfile.mkdtemp(prefix="cxr-bin-")
        bin_path = os.path.join(root, "codex.exe")
        open(bin_path, "wb").close()
        d = _stub(bin_path)
        with mock.patch.object(daemon_mod, "find_codex_bin") as find:
            d._refresh_codex_bin()
        find.assert_not_called()
        self.assertEqual(d.codex_bin, bin_path)

    def test_removed_binary_is_reresolved(self):
        root = tempfile.mkdtemp(prefix="cxr-bin-")
        old = os.path.join(root, "old", "codex.exe")
        new = os.path.join(root, "new", "codex.exe")
        d = _stub(old)
        with mock.patch.object(daemon_mod, "find_codex_bin",
                               return_value=new) as find:
            d._refresh_codex_bin()
        find.assert_called_once_with(DEFAULTS["general"]["codex_bin"])
        self.assertEqual(d.codex_bin, new)

    def test_unresolvable_keeps_cached_path(self):
        root = tempfile.mkdtemp(prefix="cxr-bin-")
        old = os.path.join(root, "old", "codex.exe")
        d = _stub(old)
        with mock.patch.object(
                daemon_mod, "find_codex_bin",
                side_effect=daemon_mod.CodexEnvError("not found")):
            d._refresh_codex_bin()
        self.assertEqual(d.codex_bin, old)


if __name__ == "__main__":
    unittest.main()
