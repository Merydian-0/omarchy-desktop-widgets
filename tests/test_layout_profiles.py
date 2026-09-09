#!/usr/bin/env python3
"""Layout-profile persistence tests for manage-positions.sh.

These drive the real backend script as a subprocess against a throwaway
``$HOME`` so nothing touches the user's state file.

Regression under test: switching layout presets did not reflect the saved
layout one-for-one. ``manage-positions.sh`` keeps two position layers -

  * ``positions``          - the base layout every monitor falls back to
  * ``monitor_positions``  - a per-monitor override layer

and the QML resolver (``DesktopWidgets.qml`` -> ``savedPos``) reads the
override layer *first*. Switching presets replaced ``positions`` but left a
stale override from an earlier drag in place, and the override kept winning -
so a dragged widget stayed put instead of moving to the preset position.

The fix: a whole-layout load replaces *both* layers. A preset saved on a
multi-monitor setup carries its own ``monitor_positions`` and it round-trips;
a preset without one (built-ins, presets saved before the field existed,
imports from another machine) loads an empty override layer, which clears the
stale entry.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "manage-positions.sh")


def resolve_saved_pos(state, monitor_name, widget_id):
    """Reference implementation of the QML ``savedPos`` resolver.

    Mirrors DesktopWidgets.qml: the per-monitor override is consulted before
    the base ``positions`` map. Kept in sync with that binding by hand.
    """
    mon = state.get("monitor_positions", {})
    monitor_pos = None
    if monitor_name and monitor_name in mon and widget_id in mon[monitor_name]:
        monitor_pos = mon[monitor_name][widget_id]
    if monitor_pos:
        return monitor_pos
    return state.get("positions", {}).get(widget_id)


class LayoutProfileTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.home = self._home.name
        self.state_file = os.path.join(
            self.home, ".local", "state", "omarchy", "dagyr.desktop-widgets.json")
        self.addCleanup(self._home.cleanup)

    def run_script(self, *args):
        env = dict(os.environ, HOME=self.home)
        proc = subprocess.run([sys.executable, SCRIPT, *args],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        last = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
        return json.loads(last)

    def state(self):
        with open(self.state_file) as f:
            return json.load(f)

    def drag(self, widget_id, x, y, w, h, monitor):
        """Simulate a user drag/resize: writes both position layers."""
        return self.run_script("save_geometry", widget_id,
                               str(x), str(y), str(w), str(h), monitor)

    # -- the regression: unsaved drag must not survive a preset switch -----

    def test_switch_builtin_preset_reflects_it_after_a_drag(self):
        self.run_script("switch_profile", "Default")
        # user nudges the clock somewhere else on their monitor
        self.drag("clock", 111, 222, 400, 300, "DP-1")
        self.assertEqual(resolve_saved_pos(self.state(), "DP-1", "clock"),
                         {"x": 111, "y": 222, "w": 400, "h": 300})

        self.run_script("switch_profile", "Minimal")
        self.run_script("switch_profile", "Default")

        # Default preset puts the clock at (700, 20) - that is what must show,
        # not the dragged (111, 222).
        resolved = resolve_saved_pos(self.state(), "DP-1", "clock")
        self.assertEqual((resolved["x"], resolved["y"]), (700, 20))

    def test_switch_builtin_preset_loads_empty_override_layer(self):
        self.run_script("switch_profile", "Default")
        self.drag("clock", 111, 222, 400, 300, "DP-1")
        self.assertIn("DP-1", self.state()["monitor_positions"])

        self.run_script("switch_profile", "Minimal")
        self.assertEqual(self.state()["monitor_positions"], {})

    def test_switch_profile_response_carries_monitor_positions(self):
        self.run_script("switch_profile", "Default")
        self.drag("clock", 10, 10, 100, 100, "DP-1")
        res = self.run_script("switch_profile", "Gaming")
        self.assertIn("monitor_positions", res)
        self.assertEqual(res["monitor_positions"], {})

    # -- saved presets round-trip per-monitor layout ---------------------

    def test_saved_preset_round_trips_per_monitor_positions(self):
        self.run_script("switch_profile", "Default")
        # a multi-monitor user tunes the clock on their second output only
        self.drag("clock", 1500, 900, 300, 200, "DP-2")
        saved = self.run_script("save_profile", "Default")
        self.assertIn("monitor_positions",
                      saved["profile"])  # preset now stores the override layer

        # leave and come back
        self.run_script("switch_profile", "Gaming")
        self.assertEqual(self.state()["monitor_positions"], {})  # Gaming has none
        self.run_script("switch_profile", "Default")

        st = self.state()
        # the DP-2 tuning is back...
        self.assertEqual(resolve_saved_pos(st, "DP-2", "clock"),
                         {"x": 1500, "y": 900, "w": 300, "h": 200})
        # ...and an output that was never tuned still falls back to base
        self.assertEqual(resolve_saved_pos(st, "DP-1", "clock"),
                         st["positions"]["clock"])

    def test_new_preset_dialog_captures_monitor_positions(self):
        # save_profile_dialog needs a name from a GUI prompt we can't drive, so
        # exercise the equivalent named-save path and assert the stored shape.
        self.run_script("switch_profile", "Default")
        self.drag("clock", 40, 50, 100, 100, "eDP-1")
        prof = self.run_script("save_profile", "WorkDualHead")["profile"]
        self.assertEqual(prof["monitor_positions"]["eDP-1"]["clock"],
                         {"x": 40, "y": 50, "w": 100, "h": 100})

    # -- import / revert / factory-reset ---------------------------------

    def test_import_without_monitor_positions_clears_override(self):
        self.run_script("switch_profile", "Default")
        self.drag("clock", 55, 66, 200, 200, "HDMI-A-1")
        path = self._write_profile(
            "Imported", {"clock": {"x": 900, "y": 100}}, monitor_positions=None)

        res = self.run_script("import_profile", path)
        self.assertEqual(res["status"], "imported")
        self.assertEqual(self.state()["monitor_positions"], {})
        self.assertEqual(resolve_saved_pos(self.state(), "HDMI-A-1", "clock"),
                         {"x": 900, "y": 100})

    def test_import_with_monitor_positions_restores_them(self):
        self.run_script("switch_profile", "Default")
        path = self._write_profile(
            "ImportedDual", {"clock": {"x": 900, "y": 100}},
            monitor_positions={"DP-3": {"clock": {"x": 12, "y": 34}}})

        self.run_script("import_profile", path)
        self.assertEqual(resolve_saved_pos(self.state(), "DP-3", "clock"),
                         {"x": 12, "y": 34})

    def test_revert_layout_restores_saved_monitor_positions(self):
        self.run_script("switch_profile", "Default")
        self.drag("clock", 111, 111, 100, 100, "DP-1")
        self.run_script("save_layout_backup")          # snapshot with the override
        self.drag("clock", 999, 999, 100, 100, "DP-1")  # move it again

        self.run_script("revert_layout")
        self.assertEqual(resolve_saved_pos(self.state(), "DP-1", "clock"),
                         {"x": 111, "y": 111, "w": 100, "h": 100})

    def test_factory_reset_clears_monitor_override(self):
        self.run_script("switch_profile", "Default")
        self.drag("clock", 1, 2, 100, 100, "DP-1")
        self.run_script("reset_factory")
        self.assertEqual(self.state()["monitor_positions"], {})

    # -- guardrails: the fix must not break the normal path --------------

    def test_switch_profile_still_applies_positions_and_enabled(self):
        res = self.run_script("switch_profile", "Gaming")
        self.assertEqual(res["status"], "profile_switched")
        self.assertEqual(set(res["enabled_widgets"]),
                         {"hardware_telemetry", "system", "network", "media"})
        self.assertEqual(res["positions"]["network"],
                         {"x": 1520, "y": 40, "w": 360, "h": 180})

    def test_untouched_widget_resolves_to_base_position(self):
        self.run_script("switch_profile", "Default")
        self.assertEqual(resolve_saved_pos(self.state(), "DP-1", "gallery"),
                         {"x": 20, "y": 40, "w": 360, "h": 220})

    def test_per_monitor_override_survives_a_plain_drag(self):
        """A drag still writes a per-monitor override (feature intact)."""
        self.run_script("switch_profile", "Default")
        self.drag("clock", 77, 88, 300, 200, "DP-2")
        self.assertEqual(resolve_saved_pos(self.state(), "DP-2", "clock"),
                         {"x": 77, "y": 88, "w": 300, "h": 200})
        # a different monitor with no override falls back to base
        self.assertEqual(resolve_saved_pos(self.state(), "DP-9", "clock"),
                         self.state()["positions"]["clock"])

    # -- helpers --------------------------------------------------------

    def _write_profile(self, name, positions, monitor_positions=None):
        prof = {"name": name, "enabled_widgets": ["clock"], "positions": positions}
        if monitor_positions is not None:
            prof["monitor_positions"] = monitor_positions
        payload = {"type": "omarchy-desktop-widgets-profile", "profile": prof}
        path = os.path.join(self.home, f"{name}.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        return path


if __name__ == "__main__":
    unittest.main()
