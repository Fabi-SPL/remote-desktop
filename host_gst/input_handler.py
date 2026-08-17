"""
Input injection via Windows SendInput API.
Converts browser event.code values to Windows scan codes, preserving
physical key position so the host's QWERTZ layout handles character mapping.
Mouse events use virtual-desktop absolute coordinates for multi-monitor support.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging

logger = logging.getLogger("lucid-remote.input")

user32 = ctypes.windll.user32

# GetSystemMetrics indices for virtual desktop geometry
SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# SendInput type codes
INPUT_MOUSE    = 0
INPUT_KEYBOARD = 1

# Mouse event flags
MOUSEEVENTF_MOVE        = 0x0001
MOUSEEVENTF_LEFTDOWN    = 0x0002
MOUSEEVENTF_LEFTUP      = 0x0004
MOUSEEVENTF_RIGHTDOWN   = 0x0008
MOUSEEVENTF_RIGHTUP     = 0x0010
MOUSEEVENTF_MIDDLEDOWN  = 0x0020
MOUSEEVENTF_MIDDLEUP    = 0x0040
MOUSEEVENTF_WHEEL       = 0x0800
MOUSEEVENTF_ABSOLUTE    = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

# Keyboard event flags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP       = 0x0002
KEYEVENTF_SCANCODE    = 0x0008

# ---------------------------------------------------------------------------
# Scan code table: browser event.code → Windows Set-1 scan code
# Keys are physical-position based, so QWERTZ layout on Windows host works.
# ---------------------------------------------------------------------------
CODE_TO_SCANCODE: dict[str, int] = {
    "Escape":           0x01,
    "Digit1":           0x02, "Digit2":       0x03, "Digit3":    0x04,
    "Digit4":           0x05, "Digit5":       0x06, "Digit6":    0x07,
    "Digit7":           0x08, "Digit8":       0x09, "Digit9":    0x0A,
    "Digit0":           0x0B, "Minus":        0x0C, "Equal":     0x0D,
    "Backspace":        0x0E,
    "Tab":              0x0F,
    "KeyQ":             0x10, "KeyW":         0x11, "KeyE":      0x12,
    "KeyR":             0x13, "KeyT":         0x14, "KeyY":      0x15,
    "KeyU":             0x16, "KeyI":         0x17, "KeyO":      0x18,
    "KeyP":             0x19, "BracketLeft":  0x1A, "BracketRight": 0x1B,
    "Enter":            0x1C,
    "ControlLeft":      0x1D,
    "KeyA":             0x1E, "KeyS":         0x1F, "KeyD":      0x20,
    "KeyF":             0x21, "KeyG":         0x22, "KeyH":      0x23,
    "KeyJ":             0x24, "KeyK":         0x25, "KeyL":      0x26,
    "Semicolon":        0x27, "Quote":        0x28,
    "Backquote":        0x29,
    "ShiftLeft":        0x2A,
    "Backslash":        0x2B,
    "KeyZ":             0x2C, "KeyX":         0x2D, "KeyC":      0x2E,
    "KeyV":             0x2F, "KeyB":         0x30, "KeyN":      0x31,
    "KeyM":             0x32, "Comma":        0x33, "Period":    0x34,
    "Slash":            0x35,
    "ShiftRight":       0x36,
    "NumpadMultiply":   0x37,
    "AltLeft":          0x38,
    "Space":            0x39,
    "CapsLock":         0x3A,
    "F1":  0x3B, "F2":  0x3C, "F3":  0x3D, "F4":  0x3E,
    "F5":  0x3F, "F6":  0x40, "F7":  0x41, "F8":  0x42,
    "F9":  0x43, "F10": 0x44,
    "NumLock":          0x45,
    "ScrollLock":       0x46,
    "Numpad7":          0x47, "Numpad8":      0x48, "Numpad9":   0x49,
    "NumpadSubtract":   0x4A,
    "Numpad4":          0x4B, "Numpad5":      0x4C, "Numpad6":   0x4D,
    "NumpadAdd":        0x4E,
    "Numpad1":          0x4F, "Numpad2":      0x50, "Numpad3":   0x51,
    "Numpad0":          0x52, "NumpadDecimal": 0x53,
    "IntlBackslash":    0x56,  # German keyboard key between LShift and Z
    "F11":              0x57, "F12":          0x58,
    # Extended keys (KEYEVENTF_EXTENDEDKEY flag required)
    "NumpadEnter":      0x1C,
    "ControlRight":     0x1D,
    "NumpadDivide":     0x35,
    "PrintScreen":      0x37,
    "AltRight":         0x38,
    "Home":             0x47,
    "ArrowUp":          0x48,
    "PageUp":           0x49,
    "ArrowLeft":        0x4B,
    "ArrowRight":       0x4D,
    "End":              0x4F,
    "ArrowDown":        0x50,
    "PageDown":         0x51,
    "Insert":           0x52,
    "Delete":           0x53,
    "MetaLeft":         0x5B,
    "MetaRight":        0x5C,
    "ContextMenu":      0x5D,
    "Pause":            0x45,
}

EXTENDED_KEYS: frozenset[str] = frozenset({
    "NumpadEnter", "ControlRight", "NumpadDivide", "PrintScreen",
    "AltRight", "Home", "ArrowUp", "PageUp", "ArrowLeft", "ArrowRight",
    "End", "ArrowDown", "PageDown", "Insert", "Delete",
    "MetaLeft", "MetaRight", "ContextMenu",
})


# ---------------------------------------------------------------------------
# Windows input structures
# ---------------------------------------------------------------------------

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type",   ctypes.c_ulong),
        ("_input", _INPUT_UNION),
    ]


_ZERO_PTR = ctypes.pointer(ctypes.c_ulong(0))


def _send(*inputs: INPUT):
    n   = len(inputs)
    arr = (INPUT * n)(*inputs)
    user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        _input=_INPUT_UNION(
            mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data,
                          dwFlags=flags, time=0, dwExtraInfo=_ZERO_PTR)
        ),
    )


def _key_input(scancode: int, flags: int) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        _input=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=0, wScan=scancode,
                          dwFlags=flags, time=0, dwExtraInfo=_ZERO_PTR)
        ),
    )


# ---------------------------------------------------------------------------
# InputHandler
# ---------------------------------------------------------------------------

class InputHandler:
    """
    Translates JSON input events from the browser data channel into
    Windows SendInput calls.

    Mouse coordinates arrive as (x, y) ∈ [0, 1] normalised to the
    currently-active monitor.  They are converted to virtual-desktop
    absolute space (0–65535) for MOUSEEVENTF_ABSOLUTE|VIRTUALDESK.
    """

    def __init__(self, monitors: list[dict]):
        self.monitors     = monitors
        self.monitor_idx  = 0
        # Virtual desktop geometry (covers all monitors)
        self._virt_left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        self._virt_top  = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        self._virt_w    = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        self._virt_h    = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        logger.info(
            f"Virtual desktop: {self._virt_w}×{self._virt_h} "
            f"@ ({self._virt_left}, {self._virt_top})"
        )

    def set_monitor(self, idx: int):
        if 0 <= idx < len(self.monitors):
            self.monitor_idx = idx

    @property
    def _mon(self) -> dict:
        return self.monitors[self.monitor_idx]

    def _norm_to_virt(self, nx: float, ny: float) -> tuple[int, int]:
        """Normalised [0,1] on current monitor → virtual desktop [0,65535]."""
        m  = self._mon
        px = m.get("left", 0) + nx * m["width"]
        py = m.get("top",  0) + ny * m["height"]
        ax = int((px - self._virt_left) * 65535 / max(self._virt_w,  1))
        ay = int((py - self._virt_top)  * 65535 / max(self._virt_h, 1))
        return ax, ay

    # ------------------------------------------------------------------

    def handle(self, data: dict):
        t = data.get("type", "")
        try:
            if t == "mousemove":
                ax, ay = self._norm_to_virt(data["x"], data["y"])
                _send(_mouse_input(
                    MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                    dx=ax, dy=ay,
                ))

            elif t == "mousedown":
                flag = {0: MOUSEEVENTF_LEFTDOWN,
                        1: MOUSEEVENTF_MIDDLEDOWN,
                        2: MOUSEEVENTF_RIGHTDOWN}.get(data.get("button", 0),
                                                       MOUSEEVENTF_LEFTDOWN)
                _send(_mouse_input(flag))

            elif t == "mouseup":
                flag = {0: MOUSEEVENTF_LEFTUP,
                        1: MOUSEEVENTF_MIDDLEUP,
                        2: MOUSEEVENTF_RIGHTUP}.get(data.get("button", 0),
                                                     MOUSEEVENTF_LEFTUP)
                _send(_mouse_input(flag))

            elif t == "wheel":
                dy = int(data.get("deltaY", 0))
                if dy != 0:
                    # Browser: positive = scroll down.  Windows WHEEL: positive = up.
                    wheel = max(-1200, min(1200, -dy))
                    _send(_mouse_input(MOUSEEVENTF_WHEEL, data=wheel))

                dx = int(data.get("deltaX", 0))
                if dx != 0:
                    hwheel = max(-1200, min(1200, dx))
                    _send(_mouse_input(0x01000, data=hwheel))  # MOUSEEVENTF_HWHEEL

            elif t in ("keydown", "keyup"):
                code = data.get("code", "")
                sc   = CODE_TO_SCANCODE.get(code)
                if sc is None:
                    logger.debug(f"Unknown key code: {code!r}")
                    return
                flags = KEYEVENTF_SCANCODE
                if t == "keyup":
                    flags |= KEYEVENTF_KEYUP
                if code in EXTENDED_KEYS:
                    flags |= KEYEVENTF_EXTENDEDKEY
                _send(_key_input(sc, flags))

        except Exception as e:
            logger.warning(f"Input error [{t}]: {e}")
