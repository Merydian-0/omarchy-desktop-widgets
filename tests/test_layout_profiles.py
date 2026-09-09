#!/usr/bin/env python3
"""Layout-profile persistence tests for manage-positions.sh.

These drive the real backend script as a subprocess against a throwaway
``$HOME`` so nothing touches the user's state file.

Regression under test: switching layout presets did not reflect the saved
layout one-for-one. ``manage-positions.sh`` keeps two position layers -

  * ``positions``          - the base layout every monitor falls back to
  * ``monitor_positions``  - a per-monitor override layer

and the QML resolver (``DesktopWidgets.qml`` -> ``savedPos``) reads the
override layer *first*. A profile only carries base ``positions``, so after a
user had dragged a widget once (which writes an override), switching presets
replaced ``positions`` but left the stale override in place - and the override
kept winning, so the widget stayed where it was dragged instead of moving to
the preset position.
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

    # -- the regression -----------------------------------------------------

    def test_switch_profile_reflects_preset_after_a_drag(self):
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
        self.assertEqual(resolved["x"], 700)
        self.assertEqual(resolved["y"], 20)

    def test_switch_profile_clears_monitor_override_layer(self):
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

    # -- same bug class on the other wholesale-load paths ------------------

    def test_import_profile_clears_monitor_override(self):
        self.run_script("switch_profile", "Default")
        self.drag("clock", 55, 66, 200, 200, "HDMI-A-1")

        prof = {
            "type": "omarchy-desktop-widgets-profile",
            "profile": {
                "name": "Imported",
                "enabled_widgets": ["clock"],
                "positions": {"clock": {"x": 900, "y": 100}},
            },
        }
        path = os.path.join(self.home, "imported.json")
        with open(path, "w") as f:
            json.dump(prof, f)

        res = self.run_script("import_profile", path)
        self.assertEqual(res["status"], "imported")
        self.assertEqual(self.state()["monitor_positions"], {})
        self.assertEqual(resolve_saved_pos(self.state(), "HDMI-A-1", "clock"),
                         {"x": 900, "y": 100})

    def test_revert_layout_clears_monitor_override(self):
        self.run_script("switch_profile", "Default")
        self.run_script("save_layout_backup")
        self.drag("clock", 12, 34, 100, 100, "DP-1")

        res = self.run_script("revert_layout")
        self.assertEqual(self.state()["monitor_positions"], {})
        resolved = resolve_saved_pos(self.state(), "DP-1", "clock")
        self.assertEqual((resolved["x"], resolved["y"]), (700, 20))

    def test_factory_reset_clears_monitor_override(self):
        self.run_script("switch_profile", "Default")
        self.drag("clock", 1, 2, 100, 100, "DP-1")
        self.run_script("reset_factory")
        self.assertEqual(self.state()["monitor_positions"], {})

    # -- guardrails: the fix must not break the normal path ---------------

    def test_switch_profile_still_applies_positions_and_enabled(self):
        res = self.run_script("switch_profile", "Gaming")
        self.assertEqual(res["status"], "profile_switched")
        self.assertEqual(set(res["enabled_widgets"]),
                         {"hardware_telemetry", "system", "network", "media"})
        self.assertEqual(res["positions"]["network"],
                         {"x": 1520, "y": 40, "w": 360, "h": 180})

    def test_untouched_widget_resolves_to_base_position(self):
        self.run_script("switch_profile", "Default")
        # never dragged -> no override -> resolver uses base positions
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


if __name__ == "__main__":
    unittest.main()
