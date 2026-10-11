"""ShoutAbout for macOS (Apple Silicon): the platform layer.

The hub itself (server, pipeline, pairing, model choice, discovery contract) is the same
code as on Windows. This package only holds what macOS does differently, so the Windows
modules stay untouched apart from a few delimited hooks marked ``# v2-macos``:

========================  ==============================================================
``keys``                  key codes, hotkey choices and modifier logic (pure)
``eventtap``              the "Talk to this Mac" hotkey: a CGEventTap feeding the same
                          :class:`~shoutabout.hotkey.HotkeyMachine` as Windows
``overlay_mac``           the mouth pill: a non-activating NSPanel showing the frames that
                          :mod:`shoutabout.overlay` renders
``paste``                 NSPasteboard (kept and restored) + Cmd+V through CGEventPost
``mic_mac``               the microphone recorder with macOS permission messages
``permissions``           Accessibility, Input Monitoring and Microphone checks
``menubar``               the menu bar icon and menu (Cocoa main thread), notifications
``ui_bridge``/``ui_child`` the Tk windows (setup, settings, pairing prompt) in a child
                          process, talking to the hub over two pipes
``discovery_mac``         Bonjour announcement with the built-in ``dns-sd``
``startup_mac``           "Open at login" (a LaunchAgent)
``decode_mac``            phone audio decoded by the built-in ``afconvert``
``sysinfo``               chip name and performance cores (``sysctl``)
``platform``              the glue :mod:`shoutabout.hub` calls on macOS
========================  ==============================================================

Everything that touches PyObjC imports it lazily, so every module imports (and its logic
is unit-tested) on Windows and Linux. See docs/macos.md.
"""

from __future__ import annotations

import sys

IS_MAC = sys.platform == "darwin"
BUNDLE_ID = "fyi.shoutabout.mac"
