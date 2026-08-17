"""
Audio bridge: receives a WebRTC audio track from the browser and plays
the decoded PCM to a Windows virtual audio device (VB-Cable).

Usage from server.py:
    audio_bridge = AudioBridge()
    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            audio_bridge.start(track)
"""

import asyncio
import logging
import numpy as np

logger = logging.getLogger("lucid-remote.audio")

DEFAULT_DEVICE = "CABLE Input"
DEFAULT_RATE   = 48000
BLOCK_SIZE     = 1024


class AudioBridge:

    def __init__(self, device_name: str = DEFAULT_DEVICE):
        self.device_name  = device_name
        self._sample_rate = DEFAULT_RATE
        self._stream      = None
        self._task        = None
        self._device_id   = self._find_device()
        self._open_stream()

    # ------------------------------------------------------------------ #

    def _find_device(self) -> int | None:
        try:
            import sounddevice as sd
            for i, d in enumerate(sd.query_devices()):
                if (self.device_name.lower() in d["name"].lower()
                        and d["max_output_channels"] > 0):
                    logger.info(f"Audio device: {d['name']} (id={i})")
                    return i
            logger.warning(
                f"VB-Cable '{self.device_name}' not found. "
                "Install from https://vb-audio.com/Cable/"
            )
        except ImportError:
            logger.warning("sounddevice not installed – mic bridge disabled")
        return None

    def _open_stream(self):
        if self._device_id is None:
            return
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(
                device=self._device_id,
                samplerate=self._sample_rate,
                channels=2,
                dtype="float32",
                blocksize=BLOCK_SIZE,
            )
            self._stream.start()
            logger.info(f"Audio bridge active @ {self._sample_rate}Hz → VB-Cable")
        except Exception as e:
            logger.warning(f"Audio bridge failed to open: {e}")
            self._stream = None

    # ------------------------------------------------------------------ #
    # WebRTC track consumer
    # ------------------------------------------------------------------ #

    def start(self, track):
        """Start consuming an aiortc audio track."""
        if self._stream is None:
            logger.info("Audio bridge skipped (no VB-Cable)")
            return
        self._task = asyncio.ensure_future(self._forward(track))
        logger.info("Audio bridge consuming WebRTC track")

    async def _forward(self, track):
        try:
            while True:
                frame = await track.recv()           # av.AudioFrame
                data  = frame.to_ndarray()           # (channels, samples) int16 or float

                # Normalise to float32 [-1, 1]
                if data.dtype == np.int16:
                    data = data.astype(np.float32) / 32768.0
                elif data.dtype != np.float32:
                    data = data.astype(np.float32)

                # Mono → stereo
                if data.shape[0] == 1:
                    data = np.repeat(data, 2, axis=0)

                stereo = np.ascontiguousarray(data[:2].T)   # (samples, 2)
                if self._stream:
                    self._stream.write(stereo)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Audio bridge stopped: {e}")

    # ------------------------------------------------------------------ #

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
