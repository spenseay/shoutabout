"""macOS keys for "Talk to this Mac": the hotkey choices, key codes and modifier logic.

Pure (no PyObjC), so the config schema can import it and every rule is unit-tested on any
OS.

**The default is a tap of Right ⌘ Command.** MacBook keyboards have no Right Ctrl (the
Windows default), fn/Globe already starts macOS dictation or the emoji picker, and Right
Option types special characters on many layouts while it's held. Right Command does nothing
on its own, and every Mac keyboard has one. Right Option and Right Control (external
keyboards) are offered too.

**Key events become "virtual keys"** in the same number space :class:`shoutabout.hotkey.
HotkeyMachine` uses on Windows, so the state machine is shared unchanged: Esc maps to
``VK_ESCAPE`` (it cancels), every other Mac key code ``k`` to ``KEY_BASE + k``, and mouse
buttons to ``MOUSE_BASE + n`` (⌘-click is a shortcut, so a click while the hotkey is down
must not count as a clean tap).

**Modifier keys** never send key-down/key-up on macOS, only "flags changed" with the new
modifier state. Each side of each modifier has its own device bit (IOKit's
``NX_DEVICE*KEYMASK``), which says whether *this* key went down or up; when a keyboard (or a
remapping tool) doesn't set those bits, the generic bit and our own record decide.
"""

from __future__ import annotations

# id -> label shown in Settings. Order = order in the picker. Names shared with Windows
# ("right_alt", "right_ctrl") mean the same physical key (Option is the Alt key).
MAC_HOTKEY_CHOICES: dict[str, str] = {
    "right_cmd": "Right ⌘ Command",
    "right_alt": "Right ⌥ Option",
    "right_ctrl": "Right ⌃ Control",
}
DEFAULT_MAC_HOTKEY = "right_cmd"
DEFAULT_MAC_MODE = "toggle"  # press to start, press again to stop (no holding)

# Carbon virtual key codes (HIToolbox/Events.h, kVK_*).
KC_ANSI_V = 0x09
KC_ESCAPE = 0x35
KC_RIGHT_COMMAND = 0x36
KC_COMMAND = 0x37
KC_SHIFT = 0x38
KC_CAPS_LOCK = 0x39
KC_OPTION = 0x3A
KC_CONTROL = 0x3B
KC_RIGHT_SHIFT = 0x3C
KC_RIGHT_OPTION = 0x3D
KC_RIGHT_CONTROL = 0x3E
KC_FUNCTION = 0x3F

HOTKEY_KEYCODES: dict[str, int] = {
    "right_cmd": KC_RIGHT_COMMAND,
    "right_alt": KC_RIGHT_OPTION,
    "right_ctrl": KC_RIGHT_CONTROL,
}

# CGEventFlags (device independent).
FLAG_ALPHA_SHIFT = 0x00010000
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_ALTERNATE = 0x00080000
FLAG_COMMAND = 0x00100000
FLAG_SECONDARY_FN = 0x00800000

# Device-dependent bits in the low word of CGEventFlags (IOKit/hidsystem/IOLLEvent.h).
DEV_LCTL = 0x00000001
DEV_LSHIFT = 0x00000002
DEV_RSHIFT = 0x00000004
DEV_LCMD = 0x00000008
DEV_RCMD = 0x00000010
DEV_LALT = 0x00000020
DEV_RALT = 0x00000040
DEV_RCTL = 0x00002000

# keycode -> (generic flag, this key's device bit, both sides' device bits)
MODIFIERS: dict[int, tuple[int, int, int]] = {
    KC_COMMAND: (FLAG_COMMAND, DEV_LCMD, DEV_LCMD | DEV_RCMD),
    KC_RIGHT_COMMAND: (FLAG_COMMAND, DEV_RCMD, DEV_LCMD | DEV_RCMD),
    KC_SHIFT: (FLAG_SHIFT, DEV_LSHIFT, DEV_LSHIFT | DEV_RSHIFT),
    KC_RIGHT_SHIFT: (FLAG_SHIFT, DEV_RSHIFT, DEV_LSHIFT | DEV_RSHIFT),
    KC_OPTION: (FLAG_ALTERNATE, DEV_LALT, DEV_LALT | DEV_RALT),
    KC_RIGHT_OPTION: (FLAG_ALTERNATE, DEV_RALT, DEV_LALT | DEV_RALT),
    KC_CONTROL: (FLAG_CONTROL, DEV_LCTL, DEV_LCTL | DEV_RCTL),
    KC_RIGHT_CONTROL: (FLAG_CONTROL, DEV_RCTL, DEV_LCTL | DEV_RCTL),
    KC_FUNCTION: (FLAG_SECONDARY_FN, 0, 0),
}

# The "virtual key" space shared with shoutabout.hotkey.HotkeyMachine.
VK_ESCAPE = 0x1B  # == shoutabout.hotkey.VK_ESCAPE (checked by a test)
KEY_BASE = 0x1000
MOUSE_BASE = 0x1100

# CGEventType values the tap listens to.
EV_LEFT_MOUSE_DOWN = 1
EV_RIGHT_MOUSE_DOWN = 3
EV_KEY_DOWN = 10
EV_KEY_UP = 11
EV_FLAGS_CHANGED = 12
EV_OTHER_MOUSE_DOWN = 25
EV_TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
EV_TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFF
LISTENED_EVENTS = (
    EV_KEY_DOWN,
    EV_KEY_UP,
    EV_FLAGS_CHANGED,
    EV_LEFT_MOUSE_DOWN,
    EV_RIGHT_MOUSE_DOWN,
    EV_OTHER_MOUSE_DOWN,
)

# Marks the Cmd+V we post ourselves (kCGEventSourceUserData) so the tap ignores it.
OWN_EVENT_MARK = 0x5A5A0D1C


def event_mask(events: tuple[int, ...] = LISTENED_EVENTS) -> int:
    """CGEventMask for the given event types (CGEventMaskBit)."""
    mask = 0
    for kind in events:
        mask |= 1 << kind
    return mask


def hotkey_label(hotkey: str) -> str:
    return MAC_HOTKEY_CHOICES.get(hotkey, hotkey)


def vk_for_keycode(keycode: int) -> int:
    """A Mac key code in the shared virtual-key space."""
    if keycode == KC_ESCAPE:
        return VK_ESCAPE
    return KEY_BASE + (int(keycode) & 0xFF)


def hotkey_vk(hotkey: str) -> int:
    """The virtual key :class:`HotkeyMachine` watches for a Mac hotkey id."""
    if hotkey not in HOTKEY_KEYCODES:
        raise ValueError(f"unknown hotkey {hotkey!r}; use one of: {', '.join(HOTKEY_KEYCODES)}")
    return vk_for_keycode(HOTKEY_KEYCODES[hotkey])


def modifier_down(keycode: int, flags: int, was_down: bool) -> bool:
    """Is modifier ``keycode`` down after a flags-changed event with ``flags``?"""
    generic, device, family = MODIFIERS[keycode]
    if not flags & generic:
        return False  # no key of this kind is down at all
    if device and flags & device:
        return True
    if device and flags & family:
        return False  # device bits are reported, and it's the other side that is down
    return not was_down  # no device bits: the event means this key changed


class KeyTranslator:
    """Turns tap events into ``(virtual key, down)`` pairs for the hotkey machine.

    Not thread-safe: the tap thread is its only caller.
    """

    def __init__(self) -> None:
        self._down: set[int] = set()  # modifier key codes we believe are down

    def translate(
        self, kind: int, keycode: int = 0, flags: int = 0, button: int = 0
    ) -> list[tuple[int, bool]]:
        if kind == EV_KEY_DOWN:
            return [(vk_for_keycode(keycode), True)]
        if kind == EV_KEY_UP:
            return [(vk_for_keycode(keycode), False)]
        if kind == EV_FLAGS_CHANGED:
            if keycode == KC_CAPS_LOCK:  # a toggle: one change = one press
                vk = vk_for_keycode(keycode)
                return [(vk, True), (vk, False)]
            if keycode not in MODIFIERS:
                return []
            down = modifier_down(keycode, flags, keycode in self._down)
            if down:
                self._down.add(keycode)
            else:
                self._down.discard(keycode)
            return [(vk_for_keycode(keycode), down)]
        if kind in (EV_LEFT_MOUSE_DOWN, EV_RIGHT_MOUSE_DOWN, EV_OTHER_MOUSE_DOWN):
            vk = MOUSE_BASE + (int(button) & 0x1F)
            return [(vk, True), (vk, False)]  # a click is a whole "other key" press
        return []

    def reset(self) -> None:
        self._down.clear()


def hint_text(cfg: object) -> str:
    """One line for the menu and the setup window, e.g. "Press Right ⌘ Command to start
    talking, and again to stop." (the Mac twin of local_dictation.hint_text)."""
    d = getattr(cfg, "dictation", None)
    if d is not None and not getattr(d, "enabled", True):
        return "Talk to this Mac is off."
    hotkey = str(getattr(d, "hotkey", DEFAULT_MAC_HOTKEY) or DEFAULT_MAC_HOTKEY)
    mode = str(getattr(d, "mode", DEFAULT_MAC_MODE) or DEFAULT_MAC_MODE)
    key = hotkey_label(hotkey)
    if mode == "toggle":
        return f"Press {key} to start talking, and again to stop."
    return f"Hold {key} to talk."
