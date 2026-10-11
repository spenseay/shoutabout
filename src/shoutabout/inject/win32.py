"""Shared ctypes declarations for the Windows-only modules (clipboard, injector, tray, icons).

Pure ctypes, no pywin32. Structures are built from fixed-size ctypes primitives so this
module imports on any OS (CI runs Linux); the DLL prototypes are only declared on Windows.

Every function gets explicit ``argtypes``/``restype``: ctypes' default ``int`` return
type silently truncates 64-bit handles. We use private ``WinDLL`` instances so these
declarations never clash with other code that uses ``ctypes.windll``.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import (
    POINTER,
    Structure,
    Union,
    c_char_p,
    c_int,
    c_int32,
    c_short,
    c_size_t,
    c_ssize_t,
    c_ubyte,
    c_uint,
    c_uint16,
    c_uint32,
    c_void_p,
    c_wchar,
    c_wchar_p,
)
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# ----------------------------------------------------------------------------- types

BOOL = c_int
INT = c_int
UINT = c_uint
WORD = c_uint16
DWORD = c_uint32
LONG = c_int32
SHORT = c_short
BYTE = c_ubyte
ATOM = c_uint16
HANDLE = c_void_p
HWND = c_void_p
HICON = c_void_p
HMENU = c_void_p
HINSTANCE = c_void_p
HMODULE = c_void_p
HGLOBAL = c_void_p
HDESK = c_void_p
WPARAM = c_size_t
LPARAM = c_ssize_t
LRESULT = c_ssize_t
ULONG_PTR = c_size_t
UINT_PTR = c_size_t
LPCWSTR = c_wchar_p
LPWSTR = POINTER(c_wchar)

# ------------------------------------------------------------------------- constants

# Memory / clipboard
GMEM_MOVEABLE = 0x0002
CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17
CF_OWNERDISPLAY = 0x0080
CF_DSPTEXT = 0x0081
CF_DSPBITMAP = 0x0082
CF_DSPMETAFILEPICT = 0x0083
CF_DSPENHMETAFILE = 0x008E
CF_PRIVATEFIRST = 0x0200
CF_PRIVATELAST = 0x02FF
CF_GDIOBJFIRST = 0x0300
CF_GDIOBJLAST = 0x03FF
CF_REGISTERED_FIRST = 0xC000

# Windows
HWND_MESSAGE = -3
WS_OVERLAPPED = 0x00000000
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CONTEXTMENU = 0x007B
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
WM_APP = 0x8000
GA_ROOT = 2

# Keyboard input
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_INSERT = 0x2D
VK_V = 0x56
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# Processes / tokens / desktops
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25  # TOKEN_INFORMATION_CLASS.TokenIntegrityLevel
SECURITY_MANDATORY_LOW_RID = 0x1000
SECURITY_MANDATORY_MEDIUM_RID = 0x2000
SECURITY_MANDATORY_HIGH_RID = 0x3000
SECURITY_MANDATORY_SYSTEM_RID = 0x4000
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
ASFW_ANY = 0xFFFFFFFF

# Shell notify icon
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIF_SHOWTIP = 0x80
NIIF_INFO = 0x01
NOTIFYICON_VERSION_4 = 4
NIN_SELECT = WM_USER + 0
NIN_KEYSELECT = NIN_SELECT | 0x1

# Menus
MF_STRING = 0x0000
MF_GRAYED = 0x0001
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

# Metrics / icons
SM_CXSMICON = 49
ICON_RESOURCE_VERSION = 0x00030000

ERROR_CLASS_ALREADY_EXISTS = 1410

# ----------------------------------------------------------------------- structures


class POINT(Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MSG(Structure):
    _fields_ = [
        ("hwnd", HWND),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
        ("lPrivate", DWORD),
    ]


class KEYBDINPUT(Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(Structure):
    """sizeof == 40 on x64 (the union is at offset 8). SendInput rejects any other cbSize."""

    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


class GUID(Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", BYTE * 8)]


class NOTIFYICONDATAW(Structure):
    """sizeof == 976 on x64 (with c_wchar == 2 bytes, i.e. on Windows)."""

    _fields_ = [
        ("cbSize", DWORD),
        ("hWnd", HWND),
        ("uID", UINT),
        ("uFlags", UINT),
        ("uCallbackMessage", UINT),
        ("hIcon", HICON),
        ("szTip", c_wchar * 128),
        ("dwState", DWORD),
        ("dwStateMask", DWORD),
        ("szInfo", c_wchar * 256),
        ("uVersion", UINT),  # union { uTimeout; uVersion; }
        ("szInfoTitle", c_wchar * 64),
        ("dwInfoFlags", DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", HICON),
    ]


class WNDCLASSEXW(Structure):
    _fields_ = [
        ("cbSize", UINT),
        ("style", UINT),
        ("lpfnWndProc", c_void_p),  # a WNDPROC, cast to void* (keep the callback referenced!)
        ("cbClsExtra", INT),
        ("cbWndExtra", INT),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HANDLE),
        ("hbrBackground", HANDLE),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
        ("hIconSm", HICON),
    ]


# ------------------------------------------------------------------------ functions

user32: Any = None
kernel32: Any = None
advapi32: Any = None
shell32: Any = None
WNDPROC: Any = None


def _proto(dll: Any, name: str, restype: Any, *argtypes: Any) -> None:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


def _declare() -> None:
    global user32, kernel32, advapi32, shell32, WNDPROC

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

    u, k, a, s = user32, kernel32, advapi32, shell32

    # kernel32: memory, processes
    _proto(k, "GlobalAlloc", HGLOBAL, UINT, c_size_t)
    _proto(k, "GlobalLock", c_void_p, HGLOBAL)
    _proto(k, "GlobalUnlock", BOOL, HGLOBAL)
    _proto(k, "GlobalFree", HGLOBAL, HGLOBAL)
    _proto(k, "GlobalSize", c_size_t, HGLOBAL)
    _proto(k, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
    _proto(k, "CloseHandle", BOOL, HANDLE)
    _proto(k, "GetCurrentProcess", HANDLE)
    _proto(k, "GetCurrentProcessId", DWORD)
    _proto(k, "GetCurrentThreadId", DWORD)
    _proto(k, "QueryFullProcessImageNameW", BOOL, HANDLE, DWORD, LPWSTR, POINTER(DWORD))
    _proto(k, "GetModuleHandleW", HMODULE, LPCWSTR)

    # advapi32: token integrity level
    _proto(a, "OpenProcessToken", BOOL, HANDLE, DWORD, POINTER(HANDLE))
    _proto(a, "GetTokenInformation", BOOL, HANDLE, c_int, c_void_p, DWORD, POINTER(DWORD))
    _proto(a, "GetSidSubAuthorityCount", POINTER(c_ubyte), c_void_p)
    _proto(a, "GetSidSubAuthority", POINTER(DWORD), c_void_p, DWORD)

    # user32: clipboard
    _proto(u, "OpenClipboard", BOOL, HWND)
    _proto(u, "CloseClipboard", BOOL)
    _proto(u, "EmptyClipboard", BOOL)
    _proto(u, "GetClipboardData", HANDLE, UINT)
    _proto(u, "SetClipboardData", HANDLE, UINT, HANDLE)
    _proto(u, "EnumClipboardFormats", UINT, UINT)
    _proto(u, "CountClipboardFormats", c_int)
    _proto(u, "IsClipboardFormatAvailable", BOOL, UINT)
    _proto(u, "GetClipboardFormatNameW", c_int, UINT, LPWSTR, c_int)
    _proto(u, "RegisterClipboardFormatW", UINT, LPCWSTR)
    _proto(u, "GetClipboardSequenceNumber", DWORD)
    _proto(u, "GetOpenClipboardWindow", HWND)

    # user32: foreground, desktop, keyboard
    _proto(u, "GetForegroundWindow", HWND)
    _proto(u, "SetForegroundWindow", BOOL, HWND)
    _proto(u, "AllowSetForegroundWindow", BOOL, DWORD)
    _proto(u, "GetAncestor", HWND, HWND, UINT)
    _proto(u, "GetWindowThreadProcessId", DWORD, HWND, POINTER(DWORD))
    _proto(u, "OpenInputDesktop", HDESK, DWORD, BOOL, DWORD)
    _proto(u, "CloseDesktop", BOOL, HDESK)
    _proto(u, "GetUserObjectInformationW", BOOL, HANDLE, c_int, c_void_p, DWORD, POINTER(DWORD))
    _proto(u, "GetAsyncKeyState", SHORT, c_int)
    _proto(u, "MapVirtualKeyW", UINT, UINT, UINT)
    _proto(u, "SendInput", UINT, UINT, POINTER(INPUT), c_int)

    # user32: windows, messages, menus, icons
    _proto(
        u,
        "CreateWindowExW",
        HWND,
        DWORD,
        LPCWSTR,
        LPCWSTR,
        DWORD,
        c_int,
        c_int,
        c_int,
        c_int,
        HWND,
        HMENU,
        HINSTANCE,
        c_void_p,
    )
    _proto(u, "DestroyWindow", BOOL, HWND)
    _proto(u, "IsWindow", BOOL, HWND)
    _proto(u, "RegisterClassExW", ATOM, POINTER(WNDCLASSEXW))
    _proto(u, "UnregisterClassW", BOOL, LPCWSTR, HINSTANCE)
    _proto(u, "DefWindowProcW", LRESULT, HWND, UINT, WPARAM, LPARAM)
    _proto(u, "GetMessageW", BOOL, POINTER(MSG), HWND, UINT, UINT)
    _proto(u, "TranslateMessage", BOOL, POINTER(MSG))
    _proto(u, "DispatchMessageW", LRESULT, POINTER(MSG))
    _proto(u, "PostMessageW", BOOL, HWND, UINT, WPARAM, LPARAM)
    _proto(u, "PostQuitMessage", None, c_int)
    _proto(u, "RegisterWindowMessageW", UINT, LPCWSTR)
    _proto(u, "SetTimer", UINT_PTR, HWND, UINT_PTR, UINT, c_void_p)
    _proto(u, "KillTimer", BOOL, HWND, UINT_PTR)
    _proto(u, "CreatePopupMenu", HMENU)
    _proto(u, "AppendMenuW", BOOL, HMENU, UINT, UINT_PTR, LPCWSTR)
    _proto(u, "DestroyMenu", BOOL, HMENU)
    _proto(u, "TrackPopupMenu", BOOL, HMENU, UINT, c_int, c_int, c_int, HWND, c_void_p)
    _proto(u, "GetCursorPos", BOOL, POINTER(POINT))
    _proto(u, "GetSystemMetrics", c_int, c_int)
    _proto(u, "CreateIconFromResourceEx", HICON, c_char_p, DWORD, BOOL, DWORD, c_int, c_int, UINT)
    _proto(u, "DestroyIcon", BOOL, HICON)

    # shell32: notification area
    _proto(s, "Shell_NotifyIconW", BOOL, DWORD, POINTER(NOTIFYICONDATAW))


if IS_WINDOWS:
    _declare()


def require_windows() -> None:
    if not IS_WINDOWS:
        raise OSError("this operation needs Windows")


def last_error() -> int:
    return ctypes.get_last_error() if IS_WINDOWS else 0
