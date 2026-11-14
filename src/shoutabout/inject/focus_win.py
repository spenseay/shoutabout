"""The Windows focus probe: Win32 facts plus UI Automation, through plain ctypes.

Two sources, combined by :func:`shoutabout.inject.focus.classify_windows`:

* **Win32** (instant, on the calling thread): the foreground window, its class and
  process, and ``GetGUIThreadInfo`` for its thread: the focused child window, whether a
  text caret is showing, whether a menu is open. A classic edit control's read-only style.
* **UI Automation** (cross-process, so on its own thread with a deadline): the focused
  element's control type, ValuePattern (and whether it's read-only), TextPattern,
  TextEditPattern, keyboard focus. This is what recognises browser text boxes and
  ``contenteditable`` (Gmail, Google Docs, Slack), Electron apps and modern controls.

UI Automation runs on one long-lived worker thread (COM multithreaded apartment, as
Microsoft recommends for UIA clients), with UIA's own connection and transaction timeouts
set short. The caller waits at most the probe's budget (``timeout_s``) and answers
``unknown`` if it is late; a call stuck behind a hung app makes later probes answer
``unknown`` at once until it returns. Nothing here raises.

Plain ctypes COM, in the style of :mod:`shoutabout.mfdecode`: no comtypes, no pywin32.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import sys
import time
import uuid
from collections.abc import Callable
from ctypes import POINTER, Structure, byref, c_long, c_ubyte, c_uint32, c_ushort, c_void_p
from dataclasses import dataclass, replace
from typing import Any

from shoutabout.inject.focus import (
    EDIT_WINDOW_CLASSES,
    TERMINAL_PROCESSES,
    UNKNOWN,
    Focus,
    LookWorker,
    UiaFacts,
    WinFacts,
    classify_windows,
    quick_answer,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 0.15  # the whole probe; past it, "unknown"
UIA_CONNECTION_TIMEOUT_MS = 150
UIA_TRANSACTION_TIMEOUT_MS = 300

# ----------------------------------------------------------------------- Win32

GUI_INMENUMODE = 0x4
GUI_SYSTEMMENUMODE = 0x8
GUI_POPUPMENUMODE = 0x10
GWL_STYLE = -16
ES_READONLY = 0x0800


class RECT(Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long)]


class GUITHREADINFO(Structure):
    _fields_ = [
        ("cbSize", c_uint32),
        ("flags", c_uint32),
        ("hwndActive", c_void_p),
        ("hwndFocus", c_void_p),
        ("hwndCapture", c_void_p),
        ("hwndMenuOwner", c_void_p),
        ("hwndMoveSize", c_void_p),
        ("hwndCaret", c_void_p),
        ("rcCaret", RECT),
    ]


_user32: Any = None


def _u32() -> Any:
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetGUIThreadInfo.argtypes = [c_uint32, POINTER(GUITHREADINFO)]
        u.GetGUIThreadInfo.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [c_void_p, POINTER(c_uint32)]
        u.GetWindowThreadProcessId.restype = c_uint32
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = c_void_p
        u.GetWindowLongW.argtypes = [c_void_p, ctypes.c_int]
        u.GetWindowLongW.restype = c_long
        _user32 = u
    return _user32


def class_name(hwnd: int | None) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    n = _u32().GetClassNameW(hwnd, buf, len(buf))
    return buf.value[:n] if n > 0 else ""


_kernel32: Any = None


def _k32() -> Any:
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = c_void_p
        k.OpenProcess.argtypes = [c_uint32, ctypes.c_int, c_uint32]
        k.QueryFullProcessImageNameW.argtypes = [
            c_void_p,
            c_uint32,
            ctypes.c_wchar_p,
            POINTER(c_uint32),
        ]
        k.QueryFullProcessImageNameW.restype = ctypes.c_int
        k.CloseHandle.argtypes = [c_void_p]
        _kernel32 = k
    return _kernel32


def exe_name(pid: int) -> str:
    """The process's exe name, lower-case ("" if it can't be opened)."""
    k32 = _k32()
    hproc = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not hproc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = c_uint32(len(buf))
        if not k32.QueryFullProcessImageNameW(hproc, 0, buf, byref(size)):
            return ""
        return ntpath.basename(buf.value[: size.value]).lower()
    finally:
        k32.CloseHandle(hproc)


def win32_facts() -> WinFacts:
    """The instant part: foreground window, process, focused child, caret, menu."""
    u = _u32()
    hwnd = u.GetForegroundWindow() or None
    if not hwnd:
        return WinFacts()
    pid = c_uint32(0)
    tid = u.GetWindowThreadProcessId(hwnd, byref(pid))
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    focus = caret = None
    flags = 0
    if tid and u.GetGUIThreadInfo(tid, byref(info)):
        focus, caret, flags = info.hwndFocus, info.hwndCaret, info.flags
    focus_class = class_name(focus)
    read_only = False
    if focus and focus_class.lower() in EDIT_WINDOW_CLASSES:
        read_only = bool(u.GetWindowLongW(focus, GWL_STYLE) & ES_READONLY)
    return WinFacts(
        hwnd=int(hwnd),
        window_class=class_name(hwnd),
        exe=exe_name(pid.value) if pid.value else "",
        pid=int(pid.value) or None,
        focus_class=focus_class,
        focus_read_only=read_only,
        caret=bool(caret),
        menu=bool(flags & (GUI_INMENUMODE | GUI_SYSTEMMENUMODE | GUI_POPUPMENUMODE)),
    )


# ----------------------------------------------------------------- UI Automation


class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]

    @classmethod
    def of(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class VARIANT(Structure):
    # vt + 3 reserved words + a 16-byte union (24 bytes on x64).
    _fields_ = [
        ("vt", c_ushort),
        ("r1", c_ushort),
        ("r2", c_ushort),
        ("r3", c_ushort),
        ("value", ctypes.c_longlong),
        ("value2", ctypes.c_longlong),
    ]


CLSID_CUIAutomation = GUID.of("ff48dba4-60ef-4201-aa87-54103eef594e")
CLSID_CUIAutomation8 = GUID.of("e22ad333-b25f-460c-83d0-0581107395c9")  # Windows 8+: timeouts
IID_IUIAutomation = GUID.of("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")
IID_IUIAutomation2 = GUID.of("34723aff-0c9d-49d0-9896-7ab52df8cd8a")
CLSCTX_INPROC_SERVER = 0x1
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = 0x80010106

VT_EMPTY, VT_I4, VT_BSTR, VT_BOOL = 0, 3, 8, 11

# vtable slots (IUnknown 0-2; UIAutomationClient.h)
_QUERY_INTERFACE, _RELEASE = 0, 2
_AUTOMATION_GET_FOCUSED_ELEMENT = 8
_AUTOMATION2_PUT_CONNECTION_TIMEOUT = 61
_AUTOMATION2_PUT_TRANSACTION_TIMEOUT = 63
_ELEMENT_GET_CURRENT_PROPERTY_VALUE = 10

# UIA_*PropertyId
PROCESS_ID = 30002
CONTROL_TYPE = 30003
HAS_KEYBOARD_FOCUS = 30008
CLASS_NAME = 30012
IS_PASSWORD = 30019
FRAMEWORK_ID = 30024
IS_TEXT_PATTERN_AVAILABLE = 30040
IS_VALUE_PATTERN_AVAILABLE = 30043
VALUE_IS_READ_ONLY = 30046
IS_TEXT_EDIT_PATTERN_AVAILABLE = 30149


def _hr(value: int) -> int:
    return value & 0xFFFFFFFF


def _failed(hr: int) -> bool:
    return _hr(hr) & 0x80000000 != 0


class _Com:
    """A raw COM interface pointer with explicit vtable calls and Release()."""

    __slots__ = ("ptr",)

    def __init__(self, ptr: int | None = None) -> None:
        self.ptr = c_void_p(ptr)

    def __bool__(self) -> bool:
        return bool(self.ptr.value)

    def call(self, index: int, argtypes: tuple, *args: Any) -> int:
        vtbl = ctypes.cast(self.ptr, POINTER(POINTER(c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])
        return fn(self.ptr, *args)

    def release(self) -> None:
        if self.ptr.value:
            self.call(_RELEASE, ())
            self.ptr = c_void_p()


@dataclass
class _Ole:
    ole32: Any
    oleaut32: Any


_ole: _Ole | None = None


def _load_ole() -> _Ole:
    global _ole
    if _ole is None:
        ole32 = ctypes.WinDLL("ole32")
        oleaut32 = ctypes.WinDLL("oleaut32")
        ole32.CoInitializeEx.argtypes = [c_void_p, c_uint32]
        ole32.CoInitializeEx.restype = c_long
        ole32.CoCreateInstance.argtypes = [
            POINTER(GUID),
            c_void_p,
            c_uint32,
            POINTER(GUID),
            POINTER(c_void_p),
        ]
        ole32.CoCreateInstance.restype = c_long
        oleaut32.VariantClear.argtypes = [POINTER(VARIANT)]
        oleaut32.VariantClear.restype = c_long
        oleaut32.SysStringLen.argtypes = [c_void_p]
        oleaut32.SysStringLen.restype = ctypes.c_uint
        _ole = _Ole(ole32, oleaut32)
    return _ole


class UiaClient:
    """IUIAutomation on the thread that made it (multithreaded apartment)."""

    def __init__(self) -> None:
        ole = _load_ole()
        self._ole = ole
        hr = _hr(ole.ole32.CoInitializeEx(None, COINIT_MULTITHREADED))
        if _failed(hr) and hr != RPC_E_CHANGED_MODE:
            raise OSError(f"COM didn't start (0x{hr:08X})")
        out = c_void_p()
        hr = 0
        for clsid in (CLSID_CUIAutomation8, CLSID_CUIAutomation):
            hr = ole.ole32.CoCreateInstance(
                byref(clsid), None, CLSCTX_INPROC_SERVER, byref(IID_IUIAutomation), byref(out)
            )
            if not _failed(hr) and out.value:
                break
        if _failed(hr) or not out.value:
            raise OSError(f"UI Automation isn't available (0x{_hr(hr):08X})")
        self._uia = _Com(out.value)
        self._timeouts()

    def _timeouts(self) -> None:
        """Short UIA timeouts (IUIAutomation2, Windows 8+), so a hung app can't hold the
        worker for UIA's default 20 s."""
        out = c_void_p()
        hr = self._uia.call(
            _QUERY_INTERFACE,
            (POINTER(GUID), POINTER(c_void_p)),
            byref(IID_IUIAutomation2),
            byref(out),
        )
        if _failed(hr) or not out.value:
            log.debug("IUIAutomation2 isn't available; default UIA timeouts")
            return
        uia2 = _Com(out.value)
        try:
            uia2.call(_AUTOMATION2_PUT_CONNECTION_TIMEOUT, (c_uint32,), UIA_CONNECTION_TIMEOUT_MS)
            uia2.call(_AUTOMATION2_PUT_TRANSACTION_TIMEOUT, (c_uint32,), UIA_TRANSACTION_TIMEOUT_MS)
        finally:
            uia2.release()

    def focused(self) -> UiaFacts:
        out = c_void_p()
        hr = self._uia.call(_AUTOMATION_GET_FOCUSED_ELEMENT, (POINTER(c_void_p),), byref(out))
        if _failed(hr) or not out.value:
            raise OSError(f"no focused element (0x{_hr(hr):08X})")
        element = _Com(out.value)
        try:
            prop = self._property
            return UiaFacts(
                control_type=prop(element, CONTROL_TYPE),
                class_name=prop(element, CLASS_NAME),
                framework=prop(element, FRAMEWORK_ID),
                has_focus=prop(element, HAS_KEYBOARD_FOCUS),
                value_pattern=prop(element, IS_VALUE_PATTERN_AVAILABLE),
                value_read_only=prop(element, VALUE_IS_READ_ONLY),
                text_pattern=prop(element, IS_TEXT_PATTERN_AVAILABLE),
                text_edit_pattern=prop(element, IS_TEXT_EDIT_PATTERN_AVAILABLE),
                password=prop(element, IS_PASSWORD),
                pid=prop(element, PROCESS_ID),
            )
        finally:
            element.release()

    def _property(self, element: _Com, prop_id: int) -> Any:
        """An int, bool or str property; None when not supported or the call failed."""
        var = VARIANT()
        hr = element.call(
            _ELEMENT_GET_CURRENT_PROPERTY_VALUE,
            (ctypes.c_int, POINTER(VARIANT)),
            prop_id,
            byref(var),
        )
        if _failed(hr):
            return None
        try:
            if var.vt == VT_I4:
                return int(ctypes.c_int32(var.value & 0xFFFFFFFF).value)
            if var.vt == VT_BOOL:
                return (var.value & 0xFFFF) != 0
            if var.vt == VT_BSTR:
                if not var.value:
                    return ""
                n = self._ole.oleaut32.SysStringLen(c_void_p(var.value))
                return ctypes.wstring_at(var.value, n)
            return None  # VT_EMPTY, or UIA's "not supported" object (VT_UNKNOWN)
        finally:
            self._ole.oleaut32.VariantClear(byref(var))


# ----------------------------------------------------------------------- probe


class WindowsFocusProbe:
    """:class:`shoutabout.inject.focus.FocusProbe` for Windows."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        extra_terminals: frozenset[str] = frozenset(),
        facts: Callable[[], WinFacts] = win32_facts,
        worker: LookWorker | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.timeout_s = timeout_s
        self.terminals = TERMINAL_PROCESSES | frozenset(n.lower() for n in extra_terminals)
        self._facts = facts
        self._worker = worker if worker is not None else LookWorker(UiaClient, "UI Automation")
        self._clock = clock
        self.last_facts: WinFacts | None = None  # for scripts/focus_probe.py and the log

    def probe(self) -> Focus:
        t0 = self._clock()
        try:
            facts = self._facts()
            answer = quick_answer(facts, self.terminals)
            if answer is None:  # ask UI Automation, within what's left of the budget
                budget = self.timeout_s - (self._clock() - t0)
                uia, error = self._worker.ask(budget)
                facts = replace(facts, uia=uia, uia_error=error)
                answer = classify_windows(facts, self.terminals)
            self.last_facts = facts
            return Focus(answer.kind, answer.reason, (self._clock() - t0) * 1000)
        except Exception as exc:
            log.debug("focus probe failed", exc_info=True)
            return Focus(
                UNKNOWN, f"probe failed: {type(exc).__name__}", (self._clock() - t0) * 1000
            )


def create_probe(extra_terminals: list[str] | None = None) -> WindowsFocusProbe | None:
    """The probe on Windows, else None."""
    if sys.platform != "win32":
        return None
    from shoutabout.inject.windows import normalize_process_names

    return WindowsFocusProbe(extra_terminals=normalize_process_names(extra_terminals or []))
