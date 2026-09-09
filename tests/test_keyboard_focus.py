#!/usr/bin/env python3
"""Desktop-surface keyboard-focus policy + wiring.

Regression under test: typing into the Quick Notes scratchpad / task field
(and the App Launcher search) did nothing while the widgets sat on the
wallpaper. The desktop wlr-layer-shell surface declared
``WlrKeyboardFocus.None`` in every state except the widgets overlay / the
preferences dialog / the widget selector. A ``keyboardFocusRequested`` flag
existed in the branch expression but was **only ever declared and read, never
assigned**, so no widget could ever raise it. A focused ``TextInput`` showed a
cursor; the compositor routed every keystroke elsewhere.

The fix moves the decision into ``lib/keyboard_focus.js`` (pure, testable) and
drives it from the widget registry: whenever an enabled widget that accepts
typing is on screen, the surface asks for ``OnDemand`` - non-stealing focus
the compositor only grants on an actual click.

The layer-surface -> compositor handshake itself needs a running Wayland
session and can't be unit-tested; this covers the policy and that it is
actually wired to something.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_JS = os.path.join(ROOT, "lib", "keyboard_focus.js")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(shutil.which("node"), "node not available")
class PolicyTests(unittest.TestCase):
    """Exercise the real lib/keyboard_focus.js through node."""

    def decide(self, **state):
        script = (
            "const {desktopKeyboardFocus} = require(%s);"
            "process.stdout.write(desktopKeyboardFocus(%s));"
            % (json.dumps(POLICY_JS), json.dumps(state))
        )
        out = subprocess.run(["node", "-e", script],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_the_bug_typing_widget_on_wallpaper_with_windows_open(self):
        # widgets pinned visible ("always"/"manual" hide mode) + an editor open
        self.assertEqual(
            self.decide(widgetsShown=True, hasTypingWidget=True,
                        overlayActive=False, preferencesOpen=False,
                        selectorOpen=False),
            "OnDemand")

    def test_typing_widget_on_idle_wallpaper(self):
        self.assertEqual(
            self.decide(widgetsShown=True, hasTypingWidget=True), "OnDemand")

    def test_no_typing_widget_stays_none(self):
        # a wallpaper of clocks and gauges must not grab the keyboard
        self.assertEqual(
            self.decide(widgetsShown=True, hasTypingWidget=False), "None")

    def test_typing_widget_hidden_stays_none(self):
        # default "tiled" hide mode: widgets hidden whenever a window is open
        self.assertEqual(
            self.decide(widgetsShown=False, hasTypingWidget=True), "None")

    def test_overlay_and_preferences_and_selector_are_keyboard_capable(self):
        self.assertEqual(self.decide(overlayActive=True), "OnDemand")
        self.assertEqual(self.decide(preferencesOpen=True), "OnDemand")
        self.assertEqual(self.decide(selectorOpen=True), "OnDemand")

    def test_everything_off(self):
        self.assertEqual(self.decide(), "None")

    def test_missing_state_object_does_not_throw(self):
        script = ("const {desktopKeyboardFocus} = require(%s);"
                  "process.stdout.write(desktopKeyboardFocus());" % json.dumps(POLICY_JS))
        out = subprocess.run(["node", "-e", script],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "None")


class WiringTests(unittest.TestCase):
    """The policy must actually be connected to the layer surface + registry."""

    def test_policy_file_exists(self):
        self.assertTrue(os.path.isfile(POLICY_JS))

    def test_desktop_surface_uses_the_policy(self):
        src = read("DesktopWidgets.qml")
        self.assertIn('import "lib/keyboard_focus.js"', src)
        i = src.index("WlrLayershell.keyboardFocus:")
        binding = src[i:i + 400]
        self.assertIn("desktopKeyboardFocus(", binding,
                      "keyboardFocus is not driven by the extracted policy")
        self.assertIn("hasTypingWidget", binding)
        self.assertIn("widgetsShown", binding)

    def test_dead_keyboardFocusRequested_stub_is_gone(self):
        src = read("DesktopWidgets.qml")
        self.assertNotIn("keyboardFocusRequested", src,
                         "the never-assigned stub is still referenced")

    def test_hasTypingWidget_is_derived_from_the_registry(self):
        src = read("DesktopWidgets.qml")
        self.assertRegex(src, r"hasTypingWidget\b")
        self.assertIn("keyboardWidgetIds", src)

    def test_registry_marks_text_widgets_and_only_those(self):
        src = read("widgets/WidgetRegistry.qml")
        # crude per-entry scan: id -> whether that entry object has keyboard:true
        entries = re.findall(r'\{\s*id:\s*"([a-z_]+)".*?\}', src, re.S)
        self.assertIn("quick_notes", entries)
        for wid in ("quick_notes", "app_launcher"):
            block = re.search(r'id:\s*"%s".*?componentUrl[^\n]*\n(.*?)\n\s*\},' % wid,
                              src, re.S)
            self.assertIsNotNone(block, wid)
            self.assertIn("keyboard: true", block.group(1), wid)
        # a pure-display widget must not be flagged
        clock = re.search(r'id:\s*"clock".*?\n\s*\},', src, re.S).group(0)
        self.assertNotIn("keyboard: true", clock)

    def test_registry_exposes_keyboardWidgetIds(self):
        src = read("widgets/WidgetRegistry.qml")
        self.assertIn("property var keyboardWidgetIds", src)


if __name__ == "__main__":
    unittest.main()
