#!/usr/bin/env python3
"""Durability of the desktop-widgets state file (manage-positions.sh).

Two failure modes this guards against:

* **Silent reset on a bad read.** ``load_settings`` used to swallow any JSON
  error, return a defaults dict, and the next ``save_settings`` wrote that
  over the file - a transient corruption became permanent loss of every
  layout, preset and position. Now a bad file is moved aside
  (``.corrupt-<ts>``), the rolling ``.bak`` is tried, and only then does it
  fall back to defaults.

* **Lost updates under concurrency.** The QML side fires this script as
  detached processes (``Quickshell.execDetached``), so several run at once,
  each doing a full read/modify/write of the whole file. Without a lock an
  interleaved pair drops one side's change. Now the whole run holds an
  exclusive ``flock``, and writes are atomic (temp file + ``os.replace``).
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "manage-positions.sh")


class StatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.home = self._home.name
        self.state_dir = os.path.join(self.home, ".local", "state", "omarchy")
        self.state_file = os.path.join(self.state_dir, "dagyr.desktop-widgets.json")
        self.addCleanup(self._home.cleanup)

    def run_script(self, *args, env_extra=None, expect_ok=True):
        env = dict(os.environ, HOME=self.home)
        if env_extra:
            env.update(env_extra)
        p = subprocess.run([sys.executable, SCRIPT, *args],
                           env=env, capture_output=True, text=True, timeout=30)
        if expect_ok:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def state(self):
        with open(self.state_file) as f:
            return json.load(f)

    def tmp_leftovers(self):
        return glob.glob(os.path.join(self.state_dir, ".*.tmp"))

    # -- atomic write ----------------------------------------------------

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        self.run_script("switch_profile", "Default")
        self.run_script("save_geometry", "clock", "1", "2", "100", "100", "DP-1")
        self.assertEqual(self.tmp_leftovers(), [])
        self.state()  # parses as valid JSON

    def test_rolling_backup_holds_the_previous_version(self):
        self.run_script("switch_profile", "Default")               # write 1
        self.run_script("save_geometry", "clock", "5", "5", "80", "80", "DP-1")  # write 2
        after_two = self.state()
        self.run_script("save_geometry", "clock", "9", "9", "80", "80", "DP-1")  # write 3
        bak = self.state_file + ".bak"
        self.assertTrue(os.path.exists(bak))
        with open(bak) as f:
            self.assertEqual(json.load(f)["positions"]["clock"],
                             after_two["positions"]["clock"])

    # -- corruption handling -------------------------------------------

    def test_corrupt_file_is_quarantined_not_overwritten(self):
        os.makedirs(self.state_dir)
        with open(self.state_file, "w") as f:
            f.write('{"layout_profiles": {"Horizon": {"positions": {"clock":')  # truncated
        self.run_script("load")
        corrupt = glob.glob(self.state_file + ".corrupt-*")
        self.assertEqual(len(corrupt), 1, "bad file was not quarantined")
        self.assertIn("Horizon", open(corrupt[0]).read())   # original preserved
        json.loads(open(self.state_file).read())            # a fresh valid file exists

    def test_corrupt_file_recovers_earlier_layout_from_rolling_backup(self):
        # .bak is always the state as of the *previous* write, so corruption
        # can cost at most the single most-recent change - everything before
        # it survives instead of the whole file resetting.
        self.run_script("switch_profile", "Default")
        self.run_script("save_geometry", "gallery", "11", "22", "300", "200", "DP-1")
        self.run_script("save_geometry", "media", "33", "44", "50", "50", "DP-1")
        committed = self.state()  # .bak now holds the gallery drag

        with open(self.state_file, "w") as f:
            f.write("}}} not json")
        self.run_script("toggle_widget", "weather", "true")

        recovered = self.state()
        self.assertEqual(recovered["positions"].get("gallery"),
                         committed["positions"]["gallery"],
                         "did not recover the earlier layout from .bak")
        self.assertEqual(sorted(recovered["layout_profiles"]),
                         sorted(committed["layout_profiles"]))
        self.assertTrue(recovered["enabled_widgets"])  # not an empty default
        self.assertEqual(len(glob.glob(self.state_file + ".corrupt-*")), 1)

    def test_reset_factory_still_writes_defaults_and_keeps_a_backup(self):
        self.run_script("switch_profile", "Default")
        self.run_script("save_geometry", "clock", "3", "3", "50", "50", "DP-1")
        self.run_script("reset_factory")
        self.assertEqual(self.state()["positions"], {})
        self.assertEqual(self.state()["active_profile"], "Default")
        with open(self.state_file + ".bak") as f:
            self.assertIn("clock", json.load(f)["positions"])  # pre-reset state kept

    # -- concurrency --------------------------------------------------

    def test_concurrent_writes_do_not_lose_updates(self):
        self.run_script("switch_profile", "Default")
        n = 15
        errors = []

        def worker(i):
            try:
                self.run_script("save_geometry", "w%d" % i,
                                str(i), str(i), "40", "40", "DP-1")
            except Exception as e:  # noqa: BLE001 - surface it to the assert
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        pos = self.state()["positions"]
        missing = [("w%d" % i) for i in range(n) if ("w%d" % i) not in pos]
        self.assertEqual(missing, [], "lost writes under concurrency: %s" % missing)

    def test_proceeds_when_the_lock_is_held_elsewhere(self):
        self.run_script("switch_profile", "Default")
        lock_path = self.state_file + ".lock"
        import fcntl
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            p = self.run_script("save_geometry", "clock", "7", "7", "60", "60", "DP-1",
                                env_extra={"MANAGE_POSITIONS_LOCK_TIMEOUT": "0.3"})
            self.assertIn("clock", self.state()["positions"])
            self.assertIn("timed out", p.stderr)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
