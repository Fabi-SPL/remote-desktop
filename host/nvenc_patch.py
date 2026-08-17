"""
Monkey-patch aiortc's H264Encoder to use h264_nvenc (NVIDIA hardware
encode) instead of libx264 — plus a minimal RTP send pacer that yields
the asyncio event loop every N packets.

Why this file exists:
  - aiortc's default libx264 software encoder takes 50-150ms per frame
    at 1080p30 in single-threaded Python, building backlog.
  - aiortc has no RTP send pacer — keyframes (150+ packets) are blasted
    in a tight for-loop, starving ICE heartbeats and causing 7-second
    RTT spikes on the client side.
  - Research source: aiortc Discussion #965 (no pacer confirmed),
    rtcrtpsender.py source inspection.

Apply at server startup BEFORE creating RTCPeerConnection:
    from nvenc_patch import apply_nvenc_patch, install_send_pacer
    apply_nvenc_patch()
    # then for each new pc:
    install_send_pacer(pc)
"""

import asyncio
import fractions
import logging

import av
import aiortc.codecs.h264 as _h264
from aiortc.codecs.h264 import H264Encoder, MAX_FRAME_RATE

logger = logging.getLogger("lucid-remote.nvenc")

_NVENC_TESTED = False
_NVENC_WORKS  = False


def _nvenc_available() -> bool:
    global _NVENC_TESTED, _NVENC_WORKS
    if _NVENC_TESTED:
        return _NVENC_WORKS
    _NVENC_TESTED = True
    try:
        ctx = av.CodecContext.create("h264_nvenc", "w")
        ctx.width  = 640
        ctx.height = 480
        ctx.pix_fmt = "yuv420p"
        ctx.time_base = fractions.Fraction(1, 30)
        ctx.options = {"preset": "p1", "tune": "ull"}
        ctx.open()
        _NVENC_WORKS = True
        logger.info("h264_nvenc probe: OK")
    except Exception as e:
        logger.warning(f"h264_nvenc probe failed: {e}")
        _NVENC_WORKS = False
    return _NVENC_WORKS


# ── Tunables ────────────────────────────────────────────────────────────────
FIXED_BITRATE = 3_000_000     # 3 Mbps — fits keyframes in MTU window faster
GOP_SIZE      = 120           # keyframe every 4s → smaller bursts (~50 packets)
TARGET_FPS    = 30
# Leaky-bucket pacer: spread RTP packets across inter-frame time
# Byte budget per second = bitrate / 8.  Time per 1200-byte packet:
#   packet_time_s = 1200 / (bitrate / 8) = 9600 / bitrate
# For 3 Mbps that's ~3.2ms per packet — plenty of time for other coros.
PACE_PACKET_INTERVAL_S = 9600 / FIXED_BITRATE


def apply_nvenc_patch() -> bool:
    """
    Replace aiortc.codecs.h264.H264Encoder._encode_frame with a version
    that uses h264_nvenc with ultra-low-latency flags + extended GOP.
    Also locks target_bitrate so aiortc's broken BWE can't churn the
    encoder with bitrate changes.
    """
    if not _nvenc_available():
        return False

    _orig_encode_frame = H264Encoder._encode_frame

    def _patched_encode_frame(self, frame, force_keyframe):
        if self.codec and (
            frame.width  != self.codec.width
            or frame.height != self.codec.height
        ):
            self.codec = None

        if self.codec is None:
            try:
                self.codec = av.CodecContext.create("h264_nvenc", "w")
                self.codec.width      = frame.width
                self.codec.height     = frame.height
                self.codec.bit_rate   = FIXED_BITRATE
                self.codec.pix_fmt    = "yuv420p"
                self.codec.framerate  = fractions.Fraction(MAX_FRAME_RATE, 1)
                self.codec.time_base  = fractions.Fraction(1, MAX_FRAME_RATE)
                self.codec.options    = {
                    # ─── Latency-critical flags (from research report) ───
                    "preset":       "p1",     # fastest latency preset
                    "tune":         "ull",    # ultra-low-latency
                    "rc":           "cbr",    # constant bitrate
                    "bf":           "0",      # no B-frames
                    "g":            str(GOP_SIZE),   # 300 = keyframe every 10s
                    "delay":        "0",      # zero output delay
                    "zerolatency":  "1",
                    "profile":      "high",
                    "level":        "40",
                    # ─── Minimum pipeline depth (added per research) ───
                    "rc-lookahead": "0",      # zero lookahead
                    "surfaces":     "2",      # driver clamps '1' on consumer GPUs; '2' is lowest real value
                    "2pass":        "0",      # disable 2-pass
                    "spatial-aq":   "0",      # disable adaptive quantization
                    "temporal-aq":  "0",
                    "multipass":    "0",      # disable multipass
                    "forced-idr":   "1",      # honor IDR on PLI requests
                }
                self.codec.open()
                logger.info(
                    f"h264_nvenc created: {frame.width}×{frame.height} "
                    f"@ {FIXED_BITRATE} bps, GOP={GOP_SIZE}"
                )
            except Exception as e:
                logger.warning(f"NVENC create failed ({e}), falling back")
                self.codec = None
                return _orig_encode_frame(self, frame, force_keyframe)

        data_to_send = b""
        for package in self.codec.encode(frame):
            data_to_send += bytes(package)

        if data_to_send:
            yield from self._split_bitstream(data_to_send)

    H264Encoder._encode_frame = _patched_encode_frame

    # ── Patch _split_bitstream to respect NAL unit boundaries ─────────────
    # aiortc's default splits H.264 bytes at 1200-byte chunks regardless of
    # where NAL units start (aiortc issue #1082). This corrupts large
    # keyframes that contain multiple NAL units (SPS+PPS+IDR slice), causing
    # the browser to request PLI → new keyframe → corrupt again → loop.
    #
    # Fix: always split on NAL unit boundaries first, then fragment each NAL
    # into MTU-sized pieces using FU-A (which is NAL-aware).
    def _nal_aware_split_bitstream(self, buf: bytes):
        # Find NAL unit boundaries using Annex-B start codes
        i, start_code_len = _find_next_start_code(buf, 0)
        if i < 0:
            # No start code at all — fall back to treating whole thing as one NAL
            yield buf
            return
        while True:
            nal_start = i + start_code_len
            # Find next start code
            next_i, next_len = _find_next_start_code(buf, nal_start)
            if next_i < 0:
                nalu = buf[nal_start:]
            else:
                nalu = buf[nal_start:next_i]
            if nalu:
                yield nalu
            if next_i < 0:
                break
            i = next_i
            start_code_len = next_len

    H264Encoder._split_bitstream = _nal_aware_split_bitstream

    logger.info("H264Encoder._split_bitstream patched → NAL-aware (fixes keyframe corruption)")

    # ── Lock target_bitrate: prevent aiortc's broken BWE from churning NVENC ──
    _orig_init = H264Encoder.__init__
    def _locked_init(self):
        _orig_init(self)
        self._fixed_bitrate = FIXED_BITRATE
    H264Encoder.__init__ = _locked_init

    # Replace the target_bitrate property to ignore setter calls
    def _get_br(self):
        return getattr(self, "_fixed_bitrate", FIXED_BITRATE)
    def _set_br(self, value):
        # Swallow — don't let BWE change the bitrate on a LAN
        pass
    H264Encoder.target_bitrate = property(_get_br, _set_br)

    logger.info(f"aiortc H264Encoder patched → h264_nvenc (bitrate locked @ {FIXED_BITRATE})")
    return True


# ════════════════════════════════════════════════════════════════════════════
#   H.264 Annex-B start code finder
# ════════════════════════════════════════════════════════════════════════════

def _find_next_start_code(buf: bytes, offset: int) -> tuple[int, int]:
    """
    Find next Annex-B NAL start code (0x000001 or 0x00000001) at or after
    offset.  Returns (index, start_code_length), or (-1, 0) if none found.
    """
    # Search for the 3-byte start code; also accept 4-byte (extra leading zero)
    i = buf.find(b"\x00\x00\x01", offset)
    if i < 0:
        return -1, 0
    if i >= 1 and buf[i - 1] == 0x00:
        return i - 1, 4   # 4-byte start code 0x00000001
    return i, 3


# ════════════════════════════════════════════════════════════════════════════
#   RTP send pacer
# ════════════════════════════════════════════════════════════════════════════

def install_send_pacer(pc) -> int:
    """
    DISABLED — was causing 5fps / 750ms keyframe lockout.

    Diagnosis (2026-04-18):
      The pacer called asyncio.sleep(3.2ms) per RTP packet to space a
      3 Mbps stream.  On Windows, the default timer resolution is ~15.6ms,
      so each sleep(0.0032) actually sleeps ~15ms.  A 50-packet keyframe
      was locked out for 50 × 15ms = 750ms.  Measured empirically:
          50x asyncio.sleep(3.2ms) → 756.7ms real time

      Result: aiortc's send path couldn't keep up → pipeline_gap of
      222-504ms per frame → delivered fps collapsed to 2-3.

      On LAN/Tailscale with sub-1ms baseline, blasting a keyframe in a
      tight for-loop takes ~1ms wall-clock and doesn't starve anything.
      The "pacer" was solving a theoretical congested-WAN problem while
      creating a much worse real one.

    Kept as a callable no-op so server.py doesn't break; also bumps the
    Windows multimedia timer resolution to 1ms so any OTHER asyncio.sleep
    in aiortc (RTCP timing, retries, etc.) is accurate.
    """
    # ── Bump Windows timer resolution to 1ms ───────────────────────────
    # Default 15.6ms resolution poisons all short asyncio.sleep calls.
    # This is the standard trick used by games, Discord, browsers, etc.
    try:
        import ctypes
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
        logger.info("Windows timer resolution bumped to 1ms (timeBeginPeriod)")
    except Exception as e:
        logger.warning(f"timeBeginPeriod failed: {e}")

    logger.info("RTP pacer: DISABLED (was causing 5fps lockout on Windows)")
    return 0
