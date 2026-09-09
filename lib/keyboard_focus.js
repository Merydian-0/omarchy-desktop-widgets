// Keyboard-focus policy for the desktop layer surface.
//
// A wlr-layer-shell surface only receives key events while its
// keyboard-interactivity is not `none`. The desktop-widgets surface lives on
// the bottom layer and defaults to `none` so it never steals keystrokes from
// real windows - but that also means a widget's TextInput/TextEdit can hold
// Qt focus (blinking cursor, highlighted border) while the compositor routes
// every keystroke elsewhere. Typing into Quick Notes or the App Launcher
// search silently does nothing.
//
// A reactive flip (grant focus once a field is focused) does not work: a
// surface with `keyboardFocus: none` never becomes the active window, so its
// items never gain *active* focus, so there is nothing to react to. The
// surface has to be keyboard-capable *before* the click. So: whenever a
// widget that accepts typing is actually on screen, ask for `OnDemand` -
// which the compositor only turns into real focus when the surface is
// clicked, and gives back the moment another surface is clicked. Nothing is
// stolen; the field just works.
//
// Pure and side-effect free so it can be unit-tested without a compositor.
// Loads both as a QML JS resource and under Node (see tests/).

/**
 * @param {object} s
 * @param {boolean} s.overlayActive     - the widgets overlay / layout editor is open
 * @param {boolean} s.preferencesOpen   - the preferences dialog is open
 * @param {boolean} s.selectorOpen      - the widget selector drawer is open
 * @param {boolean} s.widgetsShown      - widgets are currently visible on this screen
 * @param {boolean} s.hasTypingWidget   - an enabled widget accepts keyboard input
 * @returns {"OnDemand"|"None"}
 */
function desktopKeyboardFocus(s) {
  s = s || {};

  // Explicit, modal shell surfaces - always keyboard-capable while open.
  if (s.overlayActive || s.preferencesOpen || s.selectorOpen) {
    return "OnDemand";
  }

  // A typing widget sitting on the wallpaper needs the surface armed up front.
  if (s.widgetsShown && s.hasTypingWidget) {
    return "OnDemand";
  }

  return "None";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { desktopKeyboardFocus: desktopKeyboardFocus };
}
