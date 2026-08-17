"""
Monitor enumeration via Windows API.
In the host_gst build, capture is handled by GStreamer's
d3d11screencapturesrc — this module only enumerates monitors so the
server can advertise the list to the client.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging

logger = logging.getLogger("lucid-remote.capture")


def get_monitor_list() -> list[dict]:
    """Return [{index, left, top, width, height}, ...] via EnumDisplayMonitors."""
    monitors: list[dict] = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.c_double,
    )

    def _cb(hMon, hdcMon, lpRect, dwData):
        r = lpRect.contents
        monitors.append({
            "index":  len(monitors),
            "left":   r.left,
            "top":    r.top,
            "width":  r.right  - r.left,
            "height": r.bottom - r.top,
        })
        return True

    ctypes.windll.user32.EnumDisplayMonitors(
        None, None, MONITORENUMPROC(_cb), 0
    )

    if not monitors:
        monitors = [{"index": 0, "left": 0, "top": 0, "width": 1920, "height": 1080}]

    logger.info("Monitors: " + ", ".join(
        f"[{m['index']}] {m['width']}×{m['height']} @({m['left']},{m['top']})"
        for m in monitors
    ))
    return monitors
