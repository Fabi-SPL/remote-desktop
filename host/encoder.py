"""
ScreenCaptureTrack — aiortc VideoStreamTrack backed by dxcam.

Architecture (per research report Finding 9):
  - Dedicated OS thread runs dxcam capture continuously
  - Latest frame stored in a deque(maxlen=1) — old frames discarded at source
  - recv() on the asyncio event loop just grabs the newest frame (non-blocking)
  - NO cv2 conversion in recv() — native BGRA format (research Finding 8)
  - FFmpeg/NVENC does BGRA→NV12 conversion in the executor thread

This eliminates event-loop blocking from capture + color conversion, which
the research report identified as 60-150ms of per-second event-loop
starvation.
"""

import asyncio
import fractions
import logging
import threading
import time
from collections import deque

import numpy as np
from aiortc import VideoStreamTrack
from av     import VideoFrame

logger = logging.getLogger("lucid-remote.encoder")

VIDEO_CLOCK_RATE = 90000
VIDEO_TIME_BASE  = fractions.Fraction(1, VIDEO_CLOCK_RATE)


class ScreenCaptureTrack(VideoStreamTrack):
    """
    Producer-consumer pipeline:
      Capture thread  →  deque(maxlen=1)  →  recv() (asyncio)  →  aiortc encoder thread
    """

    kind = "video"

    def __init__(self, monitor_index: int = 0, fps: int = 30):
        super().__init__()
        self.fps            = fps
        self.monitor_index  = monitor_index
        self.width          = 1920
        self.height         = 1080

        # Last known mouse pos (for remote overlay — unused now but
        # kept for compatibility with existing input handler).
        self.mouse_x: float = 0.5
        self.mouse_y: float = 0.5

        # Stats counters
        self.frames_captured  = 0
        self.frames_delivered = 0
        # Rolling timing diagnostics
        self._recv_time_sum   = 0.0   # time spent in recv() body
        self._recv_gap_sum    = 0.0   # gap between recv() calls (external pipeline time)
        self._last_recv_exit  = 0.0
        self._recv_samples    = 0

        self._camera = None
        self._use_dxcam = True

        # Shared single-slot buffer — old frames auto-dropped (key insight!)
        self._frame_deque: deque = deque(maxlen=1)
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None

        self._init_camera()
        self._start_capture_thread()

    # ──────────────────────────────────────────────────────────────────

    def _init_camera(self):
        """
        Initialise dxcam with BGRA output (native DXGI format — no cv2
        conversion needed per research Finding 8).
        """
        try:
            import dxcam
            self._camera = dxcam.create(
                output_idx=self.monitor_index,
                output_color="BGRA",         # native DXGI format — no cv2 cvtColor
                processor_backend="cv2",     # backend still needed for internal ops
            )
            # target_fps tells dxcam its internal capture rate
            self._camera.start(target_fps=self.fps, video_mode=True)

            # First-frame probe for dimensions
            for _ in range(60):
                f = self._camera.get_latest_frame()
                if f is not None:
                    self.height, self.width = f.shape[:2]
                    break
                time.sleep(0.05)

            self._use_dxcam = True
            logger.info(
                f"dxcam BGRA ready: monitor {self.monitor_index} "
                f"{self.width}×{self.height} @ {self.fps}fps"
            )
            return
        except Exception as e:
            logger.warning(f"dxcam BGRA failed ({e}), falling back to mss")
            try:
                if self._camera:
                    self._camera.stop()
            except Exception:
                pass
            self._camera = None

        # mss fallback
        try:
            import mss
            with mss.mss() as sct:
                idx = min(self.monitor_index + 1, len(sct.monitors) - 1)
                shot = np.array(sct.grab(sct.monitors[idx]))  # BGRA
                self.height, self.width = shot.shape[:2]
            self._use_dxcam = False
            logger.info(f"mss fallback: {self.width}×{self.height}")
        except Exception as e:
            logger.error(f"mss also failed: {e}")
            self.width, self.height = 1920, 1080

    # ──────────────────────────────────────────────────────────────────

    def _start_capture_thread(self):
        """Start the dedicated OS thread that feeds the deque."""
        self._stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="lucid-capture",
        )
        self._capture_thread.start()

    def _capture_loop(self):
        """
        Dedicated capture thread — runs at display refresh rate.
        Pushes every new frame into the single-slot deque.  The deque
        automatically drops the previous frame if nothing consumed it.

        Zero-cost frame dropping happens HERE, not at the encoder.
        """
        poll_interval = 1.0 / (self.fps * 2)   # poll at 2x target fps
        while not self._stop_event.is_set():
            raw = None
            if self._use_dxcam and self._camera:
                raw = self._camera.get_latest_frame()
            else:
                try:
                    import mss
                    with mss.mss() as sct:
                        idx = min(self.monitor_index + 1, len(sct.monitors) - 1)
                        raw = np.array(sct.grab(sct.monitors[idx]))
                except Exception:
                    pass

            if raw is not None:
                # deque(maxlen=1).append() auto-discards the previous slot
                self._frame_deque.append(raw)
                self.frames_captured += 1
            time.sleep(poll_interval)

    # ──────────────────────────────────────────────────────────────────

    def switch_monitor(self, idx: int):
        logger.info(f"Switching capture to monitor {idx}")
        self._stop_event.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=1.0)
        try:
            if self._camera and self._use_dxcam:
                self._camera.stop()
        except Exception:
            pass
        self._camera = None
        self.monitor_index = idx
        self._frame_deque.clear()
        self._init_camera()
        self._start_capture_thread()

    def stop(self):
        self._stop_event.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=1.0)
            self._capture_thread = None
        try:
            if self._camera and self._use_dxcam:
                self._camera.stop()
        except Exception:
            pass
        self._camera = None
        try:
            super().stop()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    #   recv() — runs on asyncio event loop.  MUST be fast and non-
    #   blocking.  Just grab the newest frame from the deque.
    # ──────────────────────────────────────────────────────────────────

    async def recv(self) -> VideoFrame:
        # Measure external pipeline gap (time aiortc spent since our last return)
        entry_t = time.perf_counter()
        if self._last_recv_exit:
            self._recv_gap_sum += entry_t - self._last_recv_exit

        pts, time_base = await self.next_timestamp()

        # Spin briefly waiting for the first frame — after that the deque
        # always has something.
        while not self._frame_deque and not self._stop_event.is_set():
            await asyncio.sleep(0.002)

        try:
            raw = self._frame_deque[-1]        # peek, don't pop
        except IndexError:
            raw = np.zeros((self.height, self.width, 4), dtype=np.uint8)

        # BGRA native — FFmpeg does SIMD BGRA→YUV420P in executor thread
        if raw.ndim == 3 and raw.shape[2] == 4:
            frame = VideoFrame.from_ndarray(raw, format="bgra")
        else:
            frame = VideoFrame.from_ndarray(raw, format="bgr24")

        frame.pts       = pts
        frame.time_base = time_base
        self.frames_delivered += 1

        exit_t = time.perf_counter()
        self._recv_time_sum += exit_t - entry_t
        self._last_recv_exit = exit_t
        self._recv_samples  += 1
        return frame

    def timing_report(self) -> dict:
        n = max(self._recv_samples, 1)
        report = {
            "samples":       self._recv_samples,
            "recv_ms_avg":   self._recv_time_sum * 1000 / n,
            "gap_ms_avg":    self._recv_gap_sum  * 1000 / n,
        }
        self._recv_time_sum  = 0.0
        self._recv_gap_sum   = 0.0
        self._recv_samples   = 0
        return report
