"""Render the setup screens, the settings and History windows, the pairing prompt and the
mouth pill with FAKE data and save PNGs of them (for docs and reviews). Nothing real is
touched: a throw-away config dir, fake hardware, model choices and dictations, no hub, no
microphone, no clipboard, no network.

Usage (Windows, from the repo root):
    .venv\\Scripts\\python.exe scripts\\capture_ui.py OUT_DIR [NAME ...]

Each window is shown for about two seconds without taking the focus (WS_EX_NOACTIVATE),
painted into a bitmap with PrintWindow, saved, and closed. The pill ("overlay-top") isn't a
window here: its real frames (the same bitmaps the overlay window shows) are drawn at the
top-centre of a mock screen, where ``overlay.place`` puts them.
"""

from __future__ import annotations

import ctypes
import shutil
import sys
import tempfile
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from shoutabout import brand, ui_kit
from shoutabout.app_services import AppServices
from shoutabout.config import Paths
from shoutabout.devices import DeviceStore
from shoutabout.identity import IdentityStore
from shoutabout.pair_requests import PairingManager
from shoutabout.winstartup import StartupManager

POSITION = (60, 60)
PHONE_ID = "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d"


@dataclass(frozen=True)
class FakeChoice:
    tier: str
    stt_model: str
    device: str
    compute_type: str
    download_mb: int
    cleanup: bool
    speed: str
    label: str


GPU = SimpleNamespace(name="NVIDIA GeForce RTX 4080 SUPER", vram_gb=16.0, cuda_usable=True)
HW = SimpleNamespace(gpus=(GPU,), best_gpu=GPU, cpu_name="AMD Ryzen 7 7800X3D", ram_gb=32)
TIERS = {
    "best": FakeChoice("best", "large-v3-turbo", "cuda", "float16", 1600, True, "fast", "Best"),
    "balanced": FakeChoice(
        "balanced", "distil-large-v3", "cuda", "int8_float16", 1500, True, "ok", "Balanced"
    ),
    "light": FakeChoice("light", "base.en", "cpu", "int8", 150, False, "slow", "Light"),
}
HOTKEYS = {"right_ctrl": "Right Ctrl", "right_alt": "Right Alt", "f8": "F8"}


def fake_services(home: Path) -> AppServices:
    paths = Paths(home)
    home.mkdir(parents=True, exist_ok=True)
    devices = DeviceStore(paths.devices_file)
    identity = IdentityStore(paths.identity_file, default_name=lambda: "DESKTOP-SHOUT")
    pairing = PairingManager(devices, code_factory=lambda: "4821")
    svc = AppServices(
        paths=paths,
        devices=devices,
        identity=identity,
        pairing=pairing,
        detect_hardware=lambda **_k: HW,
        resolve_model=lambda _cfg, _hw=None: TIERS["best"],
        auto_model=lambda _hw: TIERS["best"],
        tier_choice=lambda tier, _hw: TIERS[tier],
        model_ready=lambda model: model == "large-v3-turbo",
        download_model=lambda *_a, **_k: None,
        hotkey_choices=lambda: dict(HOTKEYS),
        hint_text=lambda _cfg: "Press Right Ctrl to talk, and again to stop.",
        has_microphone=lambda: True,
        copy_text=lambda _text: None,  # never the real clipboard
        open_history=lambda: None,
        version="0.2.0",
        open_teach=lambda: None,
        open_logs=lambda: None,
        copy_hub_token=lambda: None,
        # A source checkout started by the v1 logon task: shown read-only.
        startup=StartupManager(
            run_key=SimpleNamespace(get=lambda _name: None), frozen=False, task_exists=lambda: True
        ),
    )
    # A made-up payload: the real one would hold the hub token.
    svc.legacy_qr_text = lambda: (  # type: ignore[method-assign]
        '{"shoutabout":1,"lan_url":"http://192.168.1.50:47821","public_url":null,'
        '"token":"FAKE-TOKEN-FOR-SCREENSHOTS-ONLY-xxxxxxxxxxxx","ssid":null}'
    )
    return svc


# ------------------------------------------------------------------ capture


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def capture(window, out: Path) -> None:
    user32, gdi32, dwmapi = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.dwmapi
    hwnd = int(window.wm_frame(), 16)
    full = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(full))
    vis = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(vis), ctypes.sizeof(vis)) != 0:
        vis = full
    w, h = full.right - full.left, full.bottom - full.top
    screen = user32.GetDC(0)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    gdi32.SelectObject(dc, bmp)
    user32.PrintWindow(hwnd, dc, 2)  # PW_RENDERFULLCONTENT
    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(0, screen)
    x0, y0 = vis.left - full.left, vis.top - full.top
    cw, ch = vis.right - vis.left, vis.bottom - vis.top
    rows = []
    for y in range(y0, y0 + ch):
        row = bytearray(buf.raw[(y * w + x0) * 4 : (y * w + x0 + cw) * 4])
        row[0::4], row[2::4] = row[2::4], row[0::4]
        row[3::4] = b"\xff" * cw
        rows.append(bytes(row))
    brand.write_png(out, cw, ch, b"".join(rows), alpha=False)
    print("saved", out, f"{cw}x{ch}")


def no_activate(window) -> None:
    """Show the window without activating it (it never takes the keyboard focus)."""
    user32 = ctypes.windll.user32
    hwnd = int(window.wm_frame(), 16)
    get, put = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
    get.restype, get.argtypes = ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int]
    put.restype = ctypes.c_ssize_t
    put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    put(hwnd, -20, get(hwnd, -20) | 0x08000000)  # GWL_EXSTYLE |= WS_EX_NOACTIVATE


def no_run(_work, _name) -> None:
    """Runner that does nothing: screenshots set the flow's state by hand."""


def setup_shot(name: str, svc: AppServices, root) -> None:
    from shoutabout.setup_flow import SetupFlow
    from shoutabout.setup_ui import SetupWindow

    mode = "phone" if name == "setup-add-phone" else "full"
    if name == "setup-2-no-mic":
        svc.has_microphone = lambda: False
    flow = SetupFlow(svc, mode=mode, runner=no_run)
    flow.hw, flow.choice = HW, TIERS["best"]
    flow.hardware_text = "Your NVIDIA GeForce RTX 4080 SUPER can run our most accurate model."
    if name == "setup-1-downloading":
        flow.phase, flow.done_bytes, flow.total_bytes = "downloading", 672_000_000, 1_600_000_000
    elif name == "setup-1-ready":
        flow.phase = "ready"
    else:
        flow.phase = "ready"
        flow.screen = "use"
        flow.pc_on = True
        flow.phone_on = True
    if name == "setup-2-no-mic":
        flow.pc_on = False
    if name == "setup-2-approve":
        prompt = svc.pairing.request("Sam's iPhone", PHONE_ID, "192.168.1.23")
        flow.offer(prompt)
    elif name == "setup-2-qr":
        flow.toggle_qr()
    elif name == "setup-3-try":
        flow.screen = "try"
    elif name == "setup-4-done":
        flow.screen = "done"
    flow.version += 1
    SetupWindow(root, flow)


SAMPLE_DICTATIONS = [
    (
        0,
        "local",
        None,
        "Okay so here is the plan for today. I want to finish the History window, then build the "
        "installer and run the self-test, and after lunch go through the laptop feedback one item "
        "at a time so nothing slips. If the build is green by four we ship it to the laptop.",
        True,
    ),
    (14, "lan", "Sam's iPhone", "Pick up oat milk and the good coffee on the way home.", True),
    (
        47,
        "local",
        None,
        "Thanks for the quick review. I moved the pill to the top of the screen and made it "
        "show up instantly.",
        False,
    ),
    (60 * 20, "local", None, "Meeting notes: Better Shell release moves to Thursday.", True),
    (60 * 26, "tunnel", None, "Remind me to call the dentist tomorrow morning.", True),
]


def history_services(svc: AppServices) -> None:
    """Made-up dictations and one failed recording in the throw-away config dir."""
    import json
    from datetime import datetime, timedelta

    import numpy as np

    now = datetime.now().astimezone()
    lines = []
    for minutes, route, source, text, pasted in reversed(SAMPLE_DICTATIONS):
        lines.append(
            json.dumps(
                {
                    "ts": (now - timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                    "id": f"{minutes:012d}",
                    "route": route,
                    "target": "pc",
                    "raw_text": text,
                    "text": text,
                    "pasted": pasted,
                    "warning": None if pasted else "The PC is locked — text is on your clipboard.",
                    "source": source,
                }
            )
        )
    svc.paths.history_file.write_text("\n".join(lines) + "\n", "utf-8")
    svc.failed_recordings().keep_samples(
        np.zeros(16_000 * 125, dtype=np.float32) + 0.01,
        error="The speech engine ran out of graphics memory (CUDA out of memory).",
    )
    svc.retry_failed = lambda _id: ""  # the button is live; nothing is clicked


def overlay_shots(out_dir: Path, scale: float = 1.5) -> None:
    """The pill's real frames at the top-centre of a mock screen (no window is shown)."""
    import numpy as np

    from shoutabout import overlay

    def rgba(color: tuple[int, int, int]) -> np.ndarray:
        return np.array([*color, 255], dtype=np.float32)

    def screen(w: int, h: int, taskbar_top: bool) -> tuple[np.ndarray, tuple[int, int, int, int]]:
        img = np.empty((h, w, 4), dtype=np.float32)
        img[:] = rgba((214, 226, 236))  # desktop
        bar = 48
        if taskbar_top:
            img[:bar] = rgba((32, 32, 36))
            work = (0, bar, w, h)
        else:
            img[h - bar :] = rgba((32, 32, 36))
            work = (0, 0, w, h - bar)
        # an app window under the pill (the pill never takes the focus from it)
        x0, y0, x1, y1 = 60, work[1] + 30, w - 60, work[3] - 30
        img[y0:y1, x0:x1] = rgba((255, 255, 255))
        img[y0 : y0 + 34, x0:x1] = rgba((243, 243, 243))
        for i, width in enumerate((0.62, 0.71, 0.55, 0.68, 0.4)):
            ty = y0 + 70 + i * 30
            img[ty : ty + 10, x0 + 40 : x0 + 40 + int((x1 - x0 - 80) * width)] = rgba(
                (205, 205, 205)
            )
        return img, work

    def put(img: np.ndarray, look: object, work: tuple[int, int, int, int]) -> None:
        bmp = overlay.bitmap(look, scale, overlay.gdi_text_mask)  # type: ignore[arg-type]
        arr = np.frombuffer(bmp.bgra, dtype=np.uint8).reshape(bmp.height, bmp.width, 4)
        src = arr[..., [2, 1, 0, 3]].astype(np.float32)  # premultiplied RGBA
        x_mid, top = overlay.place(work, 0, 0, scale)
        x, y = x_mid - bmp.width // 2, top
        region = img[y : y + bmp.height, x : x + bmp.width]
        a = src[..., 3:4] / 255.0
        region[..., :3] = src[..., :3] + region[..., :3] * (1 - a)

    def save(img: np.ndarray, name: str) -> None:
        out = np.clip(np.rint(img), 0, 255).astype(np.uint8)
        out[..., 3] = 255
        h, w = out.shape[:2]
        brand.write_png(out_dir / name, w, h, out.tobytes(), alpha=False)
        print("saved", out_dir / name, f"{w}x{h}")

    frames = overlay.state_frames()
    img, work = screen(1440, 900, taskbar_top=False)
    put(img, frames["listening-loud"].look, work)
    save(img, "overlay-top-listening.png")
    img, work = screen(1440, 900, taskbar_top=True)
    put(img, frames["listening-quiet"].look, work)
    save(img, "overlay-top-docked-taskbar.png")
    strips = []
    for name in (
        "listening-quiet",
        "listening-loud",
        "thinking",
        "done",
        "error",
        "notice",
    ):
        img, work = screen(900, 260, taskbar_top=False)
        put(img, frames[name].look, work)
        strips.append(img[: round(100 * scale)])
        strips.append(np.zeros((6, 900, 4), dtype=np.float32) + rgba((120, 120, 120)))
    save(np.concatenate(strips[:-1]), "overlay-top-states.png")
    # The bare pill of every state on transparency, at 4x: the website's pill image
    # (scripts/site/make_assets.py takes overlay-listening-loud.png).
    for path in overlay.write_state_pngs(out_dir, scale=4.0):
        print("saved", path)


def main() -> int:
    out_dir = Path(sys.argv[1])
    wanted = sys.argv[2:] or [
        "setup-1-downloading",
        "setup-1-ready",
        "setup-2-use",
        "setup-2-no-mic",
        "setup-2-approve",
        "setup-2-qr",
        "setup-3-try",
        "setup-4-done",
        "setup-add-phone",
        "settings",
        "history",
        "approval-prompt",
        "overlay-top",
    ]
    ui_kit.enable_dpi_awareness()
    out_dir.mkdir(parents=True, exist_ok=True)
    if "overlay-top" in wanted:
        overlay_shots(out_dir)
        wanted = [n for n in wanted if n != "overlay-top"]
    import tkinter as tk

    tmp = Path(tempfile.mkdtemp(prefix="shoutabout-screens-"))
    try:
        for name in wanted:
            svc = fake_services(tmp / name)
            root = tk.Tk()
            root.withdraw()
            if name.startswith("setup"):
                setup_shot(name, svc, root)
            elif name == "settings":
                from shoutabout.settings_model import SettingsModel
                from shoutabout.settings_ui import SettingsWindow

                svc.devices.add("Sam's iPhone", "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d")
                svc.devices.add("Work iPhone", "0d5e9b1a-7c3f-4e2d-8a6b-1f0e9d8c7b6a")
                key = svc.devices.list()[0].key
                svc.devices.touch(key)
                model = SettingsModel(svc)
                model.load_models()
                model.message = "Saved."
                SettingsWindow(root, model, load_in_background=False)
            elif name == "history":
                from shoutabout.history_model import HistoryModel
                from shoutabout.history_ui import HistoryWindow

                history_services(svc)
                HistoryWindow(root, HistoryModel(svc), load_in_background=False)
            elif name == "approval-prompt":
                from shoutabout.approval_ui import ApprovalPrompt

                pairing = svc.pairing
                prompt = pairing.request("Sam's iPhone", PHONE_ID, "192.168.1.23")
                ApprovalPrompt(
                    root,
                    prompt,
                    approve=lambda _rid: False,
                    deny=lambda _rid: False,
                    seconds_left=pairing.seconds_left,
                )
            root.geometry(f"+{POSITION[0]}+{POSITION[1]}")
            root.update_idletasks()
            no_activate(root)
            root.deiconify()
            done = threading.Event()

            def shoot(root=root, name=name, done=done) -> None:
                try:
                    capture(root, out_dir / f"{name}.png")
                finally:
                    done.set()
                    root.destroy()

            root.after(1800, shoot)
            root.mainloop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
