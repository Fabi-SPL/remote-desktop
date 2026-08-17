"""
Windows cursor capture via GetCursorInfo + GetIconInfo + GetDIBits.

Mirrors Chrome Remote Desktop's approach:
- Capture cursor bitmap only when shape changes (HCURSOR handle changes)
- Send BGRA bytes + hotspot offset + dimensions
- Position is tracked separately via mouse events on the client side

All Win32 calls have explicit argtypes/restype so 64-bit pointers don't
get truncated to 32 bits (which is what was breaking the previous version).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging

import numpy as np

logger = logging.getLogger("lucid-remote.cursor")

# ── Win32 constants ─────────────────────────────────────────────────────────
CURSOR_SHOWING  = 0x00000001
BI_RGB          = 0
DIB_RGB_COLORS  = 0


# ── Structures ──────────────────────────────────────────────────────────────

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",      wt.DWORD),
        ("flags",       wt.DWORD),
        ("hCursor",     wt.HANDLE),
        ("ptScreenPos", _POINT),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon",    wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        ("hbmMask",  wt.HBITMAP),
        ("hbmColor", wt.HBITMAP),
    ]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType",       wt.LONG),
        ("bmWidth",      wt.LONG),
        ("bmHeight",     wt.LONG),
        ("bmWidthBytes", wt.LONG),
        ("bmPlanes",     wt.WORD),
        ("bmBitsPixel",  wt.WORD),
        ("bmBits",       wt.LPVOID),
    ]


class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize",          wt.DWORD),
        ("biWidth",         wt.LONG),
        ("biHeight",        wt.LONG),
        ("biPlanes",        wt.WORD),
        ("biBitCount",      wt.WORD),
        ("biCompression",   wt.DWORD),
        ("biSizeImage",     wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed",       wt.DWORD),
        ("biClrImportant",  wt.DWORD),
    ]


class _BMI(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BMIH),
        ("bmiColors", wt.DWORD * 3),
    ]


# ── Bind Win32 with proper signatures ──────────────────────────────────────
user32 = ctypes.WinDLL("user32",  use_last_error=True)
gdi32  = ctypes.WinDLL("gdi32",   use_last_error=True)

user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
user32.GetCursorInfo.restype  = wt.BOOL

user32.GetIconInfo.argtypes   = [wt.HICON, ctypes.POINTER(_ICONINFO)]
user32.GetIconInfo.restype    = wt.BOOL

gdi32.GetObjectW.argtypes     = [wt.HANDLE, ctypes.c_int, wt.LPVOID]
gdi32.GetObjectW.restype      = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype  = wt.HDC

gdi32.DeleteDC.argtypes       = [wt.HDC]
gdi32.DeleteDC.restype        = wt.BOOL

gdi32.DeleteObject.argtypes   = [wt.HGDIOBJ]
gdi32.DeleteObject.restype    = wt.BOOL

gdi32.GetDIBits.argtypes      = [
    wt.HDC,                     # hdc
    wt.HBITMAP,                 # hbm
    wt.UINT,                    # uStartScan
    wt.UINT,                    # cScanLines
    wt.LPVOID,                  # lpvBits
    ctypes.POINTER(_BMI),       # lpbmi
    wt.UINT,                    # uUsage
]
gdi32.GetDIBits.restype       = ctypes.c_int


# ── Public API ─────────────────────────────────────────────────────────────

def get_cursor_state() -> tuple[int, int, int] | None:
    """Return (screen_x, screen_y, hcursor) of the visible cursor, or None."""
    ci = _CURSORINFO()
    ci.cbSize = ctypes.sizeof(_CURSORINFO)
    if user32.GetCursorInfo(ctypes.byref(ci)):
        if ci.flags & CURSOR_SHOWING:
            return ci.ptScreenPos.x, ci.ptScreenPos.y, ci.hCursor
    return None


def _get_dibits(hbm: int, w: int, h: int) -> np.ndarray | None:
    """Extract `h` rows of 32-bit BGRA pixels from a Windows bitmap handle."""
    hdc = gdi32.CreateCompatibleDC(None)
    if not hdc:
        return None

    bmi = _BMI()
    bmi.bmiHeader.biSize        = ctypes.sizeof(_BMIH)
    bmi.bmiHeader.biWidth       = w
    bmi.bmiHeader.biHeight      = -h          # negative = top-down
    bmi.bmiHeader.biPlanes      = 1
    bmi.bmiHeader.biBitCount    = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bmi.bmiHeader.biSizeImage   = w * h * 4

    buf = (ctypes.c_ubyte * (w * h * 4))()
    rows = gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)

    gdi32.DeleteDC(hdc)

    if rows == 0:
        return None
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()


def capture_cursor_bitmap(hcursor: int) -> dict | None:
    """
    Return {"width", "height", "hotspot_x", "hotspot_y", "bgra": bytes}
    or None on failure.

    Handles BOTH cursor formats:

    * **Colour cursor** — `hbmColor` is set, contains 32-bit BGRA pixels.
      `hbmMask` is the AND mask (1 bit per pixel as 32-bit DIB).

    * **Monochrome cursor** — `hbmColor` is NULL.  `hbmMask` is double-
      height: top half = AND mask, bottom half = XOR mask.
    """
    ii = _ICONINFO()
    if not user32.GetIconInfo(hcursor, ctypes.byref(ii)):
        return None

    hx = ii.xHotspot
    hy = ii.yHotspot

    try:
        bmp = _BITMAP()
        if ii.hbmColor:
            # Colour cursor
            gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp))
            w, h = bmp.bmWidth, bmp.bmHeight

            color = _get_dibits(ii.hbmColor, w, h)
            mask  = _get_dibits(ii.hbmMask,  w, h)
            if color is None or mask is None:
                return None

            out = np.zeros((h, w, 4), dtype=np.uint8)
            out[:, :, :3] = color[:, :, :3]   # BGR

            # If colour bitmap already has alpha, use it
            if color[:, :, 3].max() > 0:
                out[:, :, 3] = color[:, :, 3]
            else:
                # Use mask: AND=0 → opaque, AND=255 → transparent
                out[:, :, 3] = 255 - mask[:, :, 0]

        else:
            # Monochrome cursor — mask is 2× the height
            gdi32.GetObjectW(ii.hbmMask, ctypes.sizeof(bmp), ctypes.byref(bmp))
            w = bmp.bmWidth
            h = bmp.bmHeight // 2

            full = _get_dibits(ii.hbmMask, w, bmp.bmHeight)
            if full is None:
                return None

            and_mask = full[:h, :, 0]   # 0 = opaque, 255 = transparent
            xor_mask = full[h:, :, 0]   # 0 = black, 255 = white

            out = np.zeros((h, w, 4), dtype=np.uint8)
            opaque = (and_mask == 0)
            white  = (xor_mask != 0)
            out[opaque,        3] = 255       # alpha
            out[opaque & white, :3] = 255     # white pixels
            # opaque & ~white stays black (already zero)

    finally:
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)

    return {
        "width":     w,
        "height":    h,
        "hotspot_x": hx,
        "hotspot_y": hy,
        "bgra":      out.tobytes(),
    }


# ── High-level tracker ─────────────────────────────────────────────────────

class CursorTracker:
    """Returns a shape update only when the cursor handle changes."""

    def __init__(self):
        self._last_handle: int | None = None

    def poll(self) -> dict | None:
        state = get_cursor_state()
        if state is None:
            self._last_handle = None
            return None

        x, y, h = state
        payload = {"x": x, "y": y, "shape_update": False}

        if h != self._last_handle:
            shape = capture_cursor_bitmap(h)
            if shape:
                payload.update(shape)
                payload["shape_update"] = True
                self._last_handle = h
                logger.debug(f"cursor shape changed: {shape['width']}×{shape['height']}")

        return payload
