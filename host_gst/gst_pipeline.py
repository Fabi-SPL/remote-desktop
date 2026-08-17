"""
GstWebRTCPipeline
=================
A GStreamer/webrtcbin-driven WebRTC pipeline that replaces the aiortc stack.

Pipeline:
    d3d11screencapturesrc → nvd3d11h264enc (zero-copy D3D11) → rtph264pay
        → webrtcbin.sink  (video, sendonly)
    browser → webrtcbin → on-pad-added → opusdec → wasapi2sink ("CABLE Input")
    browser → webrtcbin → on-data-channel (label="input")

Threading:
    webrtcbin fires its signals on GStreamer worker threads.  We hold a
    reference to the asyncio loop and marshal everything back with
    `asyncio.run_coroutine_threadsafe` or `call_soon_threadsafe`.

Consumer API (used by server.py):
    pipe = GstWebRTCPipeline(loop, monitor_index=0, fps=30,
                              vb_cable_device="CABLE Input",
                              on_local_ice=async_cb,
                              on_input_message=sync_cb)
    pipe.start()
    answer_sdp = await pipe.handle_remote_offer(offer_sdp)
    pipe.add_remote_ice_candidate(sdp_mline_index, candidate)
    pipe.switch_monitor(idx)
    pipe.stop()
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Awaitable, Callable, Optional

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

logger = logging.getLogger("lucid-remote.gst")

# ─── Tunables (same ballpark as nvenc_patch.py) ───────────────────────────
FPS_DEFAULT        = 30
BITRATE_KBPS       = 8000     # 8 Mbps — LAN, so we can afford it
GOP_SIZE           = 120      # keyframe every 4s at 30fps
MTU                = 1200
VB_CABLE_DEFAULT   = "CABLE Input"


# ─── Pipeline description ─────────────────────────────────────────────────
# Video sender: d3d11 capture → zero-copy D3D11 NVENC → rtph264pay → webrtcbin
# We leave audio recvonly pad to be created by the remote offer.

def _build_pipeline_desc(monitor_index: int, fps: int) -> str:
    # RTP caps MUST fully specify clock-rate, packetization-mode and
    # profile-level-id so webrtcbin can match the SDP offer from Chrome.
    #
    # CRITICAL: encoder chain is NOT linked to webrtcbin at build time.
    # It terminates in a fakesink.  If we pre-link encoder → sendrecv.,
    # the CAPS event propagates downstream into webrtcbin's sink_0 at
    # pipeline-start time; webrtcbin rejects it because no transport
    # chain exists yet (pre-SDP), and the upstream cascade errors out
    # d3d11screencapturesrc with "Internal data stream error", or
    # silently segfaults webrtcbin.
    #
    # We dynamically swap the encoder tail onto webrtcbin.sink_0 AFTER
    # set-local-description, at which point webrtcbin has its DTLS/SRTP
    # transport wired up and will accept caps cleanly.
    return f"""
        webrtcbin name=sendrecv bundle-policy=max-bundle latency=0
        d3d11screencapturesrc show-cursor=true monitor-index={monitor_index}
            ! video/x-raw(memory:D3D11Memory),framerate={fps}/1
            ! nvd3d11h264enc preset=p1 tune=ultra-low-latency zerolatency=true
                rc-mode=cbr bitrate={BITRATE_KBPS} gop-size={GOP_SIZE}
            ! video/x-h264,profile=constrained-baseline,stream-format=byte-stream
            ! h264parse config-interval=-1
            ! rtph264pay name=videopay pt=96 aggregate-mode=zero-latency mtu={MTU} config-interval=-1
            ! application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000,packetization-mode=(string)1,profile-level-id=(string)42e01f,level-asymmetry-allowed=(string)1
            ! queue name=vidq max-size-buffers=3 max-size-bytes=0 max-size-time=0 leaky=downstream
            ! fakesink name=vidsink async=false sync=false
    """


class GstWebRTCPipeline:
    """
    One pipeline per WebRTC peer.  Owned by the ws_handler coroutine.

    `on_local_ice(mline_index, candidate_str)` is an async callback
    invoked from the asyncio loop whenever webrtcbin emits a local ICE
    candidate that needs to go to the browser.

    `on_input_message(dict)` is a synchronous callback invoked from the
    GStreamer thread when the "input" data channel receives a message.
    It must be cheap / non-blocking (Windows SendInput is fine).
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        monitor_index: int = 0,
        fps: int = FPS_DEFAULT,
        vb_cable_device: str = VB_CABLE_DEFAULT,
        on_local_ice: Optional[Callable[[int, str], Awaitable[None]]] = None,
        on_input_message: Optional[Callable[[dict], None]] = None,
    ):
        self._loop = loop
        self._monitor_index = monitor_index
        self._fps = fps
        self._vb_cable = vb_cable_device
        self._on_local_ice = on_local_ice
        self._on_input_message = on_input_message

        self._pipeline: Optional[Gst.Pipeline] = None
        self._webrtc: Optional[Gst.Element] = None
        self._input_channel = None  # GObject for the "input" data channel
        self._bus_watch_id: Optional[int] = None
        self._closed = threading.Event()

        # Stats
        self.frames_sent = 0
        self._sent_probe_id: Optional[int] = None

    # ─── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        desc = _build_pipeline_desc(self._monitor_index, self._fps)
        logger.info(f"Building pipeline (monitor={self._monitor_index}, fps={self._fps})")
        try:
            self._pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            logger.error(f"parse_launch failed: {e}")
            raise
        self._webrtc = self._pipeline.get_by_name("sendrecv")
        if self._webrtc is None:
            raise RuntimeError("webrtcbin element 'sendrecv' not found in pipeline")

        # ── webrtcbin signals (fire on GStreamer threads) ──────────
        self._webrtc.connect("on-ice-candidate", self._on_gst_ice_candidate)
        self._webrtc.connect("pad-added", self._on_gst_pad_added)
        self._webrtc.connect("on-data-channel", self._on_gst_data_channel)
        self._webrtc.connect("notify::ice-connection-state",
                             self._on_ice_conn_state_change)
        self._webrtc.connect("notify::connection-state",
                             self._on_peer_conn_state_change)

        # ── Bus watch for pipeline errors ──────────────────────────
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch_id = bus.connect("message", self._on_bus_message)

        # ── Stats probe on rtph264pay src ──────────────────────────
        pay = _find_first_by_factory(self._pipeline, "rtph264pay")
        if pay:
            src_pad = pay.get_static_pad("src")
            if src_pad:
                self._sent_probe_id = src_pad.add_probe(
                    Gst.PadProbeType.BUFFER, self._count_sent_frames)

        # Pipeline → PLAYING immediately.  The encoder chain terminates
        # in a fakesink (not webrtcbin), so there's no caps-negotiation
        # hazard.  Encoded H264/RTP buffers are produced and dropped by
        # the fakesink until we dynamically swap the tail onto
        # webrtcbin.sink_0 post-SDP (see _attach_video_sendpath).
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Pipeline failed to enter PLAYING")
        logger.info("Pipeline → PLAYING (encoder → fakesink, awaiting offer)")

    def stop(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            if self._bus_watch_id is not None:
                bus = self._pipeline.get_bus()
                bus.remove_signal_watch()
                try:
                    bus.disconnect(self._bus_watch_id)
                except Exception:
                    pass
                self._bus_watch_id = None
            self._pipeline = None
        self._webrtc = None
        self._input_channel = None
        logger.info("Pipeline stopped")

    # ─── Monitor switching ───────────────────────────────────────────

    def switch_monitor(self, idx: int) -> None:
        """
        Change the captured monitor.  Easiest reliable path: set source
        to NULL, change its `monitor-index`, set back to PLAYING.
        """
        src = _find_first_by_factory(self._pipeline, "d3d11screencapturesrc") \
            if self._pipeline else None
        if src is None:
            logger.warning("switch_monitor: d3d11screencapturesrc not found")
            return
        logger.info(f"switch_monitor → {idx}")
        src.set_state(Gst.State.NULL)
        src.set_property("monitor-index", idx)
        self._monitor_index = idx
        src.sync_state_with_parent()

    # ─── Offer/Answer ────────────────────────────────────────────────

    async def handle_remote_offer(self, offer_sdp: str) -> str:
        """
        Apply an SDP offer from the browser, produce an answer, return
        the answer SDP.  Both set-remote-description and create-answer
        run inside GStreamer; we bridge back via a Future.
        """
        if self._webrtc is None:
            raise RuntimeError("Pipeline not started")

        ret, sdp_msg = GstSdp.SDPMessage.new_from_text(offer_sdp)
        if ret != GstSdp.SDPResult.OK:
            raise ValueError("Failed to parse offer SDP")
        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, sdp_msg)

        # 1. set-remote-description
        set_remote_done = asyncio.Future(loop=self._loop)

        def _set_remote_cb(promise, _udata):
            self._loop.call_soon_threadsafe(
                lambda: set_remote_done.set_result(True))

        promise = Gst.Promise.new_with_change_func(_set_remote_cb, None)
        self._webrtc.emit("set-remote-description", offer, promise)
        await set_remote_done

        # 2. create-answer
        answer_fut: asyncio.Future = asyncio.Future(loop=self._loop)

        def _answer_cb(promise, _udata):
            reply = promise.get_reply()
            if reply is None:
                self._loop.call_soon_threadsafe(
                    answer_fut.set_exception,
                    RuntimeError("create-answer gave no reply"))
                return
            answer = reply.get_value("answer")
            if answer is None:
                self._loop.call_soon_threadsafe(
                    answer_fut.set_exception,
                    RuntimeError("create-answer reply has no answer"))
                return
            self._loop.call_soon_threadsafe(answer_fut.set_result, answer)

        promise = Gst.Promise.new_with_change_func(_answer_cb, None)
        self._webrtc.emit("create-answer", None, promise)

        answer = await answer_fut  # GstWebRTCSessionDescription

        # 3. set-local-description — fire-and-forget.
        # webrtcbin's canonical pattern is an un-awaited promise here; the
        # operation is effectively synchronous and awaiting its change_func
        # callback hangs forever on some GStreamer builds.
        promise = Gst.Promise.new()
        self._webrtc.emit("set-local-description", answer, promise)
        promise.interrupt()

        answer_sdp = answer.sdp.as_text()
        logger.info("Local answer SDP produced (%d chars)" % len(answer_sdp))

        # NOW that webrtcbin has parsed the offer and produced the answer,
        # its sink_0 transceiver exists and the DTLS/SRTP transport chain
        # is being built.  Dynamically detach the encoder tail from
        # fakesink and link it into webrtcbin.sink_0.
        self._attach_video_sendpath()

        return answer_sdp

    # ─── Dynamic re-link: encoder tail → webrtcbin.sink_0 ────────────

    def _attach_video_sendpath(self) -> None:
        """
        Called once, right after set-local-description.  Uses a BLOCK pad
        probe on vidq.src to atomically:
          1. unlink vidq.src from fakesink.sink
          2. request/fetch webrtcbin.sink_0
          3. link vidq.src → webrtcbin.sink_0
          4. remove the fakesink from the pipeline
        """
        if self._pipeline is None or self._webrtc is None:
            return

        vidq     = self._pipeline.get_by_name("vidq")
        fakesink = self._pipeline.get_by_name("vidsink")
        if vidq is None or fakesink is None:
            logger.error("_attach_video_sendpath: vidq or fakesink missing")
            return

        queue_src = vidq.get_static_pad("src")
        if queue_src is None:
            logger.error("_attach_video_sendpath: no src pad on vidq")
            return

        def _do_link(pad, info):
            try:
                # Detach fakesink
                fs_sink = fakesink.get_static_pad("sink")
                if fs_sink and queue_src.is_linked():
                    queue_src.unlink(fs_sink)

                # Grab (or request) webrtcbin's video sink pad.
                sink_pad = self._webrtc.get_static_pad("sink_0")
                if sink_pad is None:
                    sink_pad = self._webrtc.request_pad_simple("sink_%u")
                if sink_pad is None:
                    logger.error("Could not obtain webrtcbin sink pad")
                    return Gst.PadProbeReturn.REMOVE

                link_ret = queue_src.link(sink_pad)
                if link_ret != Gst.PadLinkReturn.OK:
                    logger.error(f"Link vidq→webrtcbin sink_0 failed: {link_ret}")
                    return Gst.PadProbeReturn.REMOVE

                # Detach the fakesink element from the pipeline so it stops
                # consuming buffers.  Must be done on the GLib thread — we
                # already are (pad probe callbacks run on streaming thread).
                fakesink.set_state(Gst.State.NULL)
                self._pipeline.remove(fakesink)

                logger.info("Encoder tail linked → webrtcbin.sink_0")
            except Exception as e:
                logger.exception(f"_do_link failed: {e}")
            return Gst.PadProbeReturn.REMOVE

        queue_src.add_probe(
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER,
            _do_link,
        )

    def add_remote_ice_candidate(
        self, sdp_mline_index: int, candidate: str
    ) -> None:
        """Forward a remote (browser) ICE candidate into webrtcbin."""
        if self._webrtc is None:
            return
        # webrtcbin expects "add-ice-candidate" (mline_index, candidate string).
        # The candidate must start with "candidate:" (no "a=" prefix).
        if candidate.startswith("a="):
            candidate = candidate[2:]
        self._webrtc.emit("add-ice-candidate", sdp_mline_index, candidate)

    # ─── Internal GStreamer callbacks (NOT on asyncio thread) ────────

    def _on_gst_ice_candidate(self, _webrtc, mline_index: int,
                              candidate: str) -> None:
        """Local ICE candidate produced by webrtcbin → forward to browser."""
        if self._on_local_ice is None:
            return
        # run the async callback on the asyncio loop
        asyncio.run_coroutine_threadsafe(
            self._on_local_ice(int(mline_index), candidate), self._loop)

    def _on_gst_pad_added(self, _webrtc, pad) -> None:
        """
        An incoming RTP pad appeared (browser mic).  Build an inline
        `rtpopusdepay ! opusdec ! audioconvert ! audioresample
        ! wasapi2sink device-name="CABLE Input"` branch and link it.
        """
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None:
            logger.warning("pad-added with no caps")
            return
        s = caps.get_structure(0)
        media = s.get_string("media") or ""
        encoding = (s.get_string("encoding-name") or "").upper()
        logger.info(f"pad-added: media={media} encoding={encoding}")
        if media != "audio":
            # Unexpected — we only requested audio recvonly; drop.
            sink = Gst.ElementFactory.make("fakesink")
            if sink and self._pipeline:
                self._pipeline.add(sink)
                sink.sync_state_with_parent()
                pad.link(sink.get_static_pad("sink"))
            return

        branch = Gst.parse_bin_from_description(
            f"rtpopusdepay ! opusdec ! audioconvert ! audioresample "
            f"! wasapi2sink device-name=\"{self._vb_cable}\" async=false sync=false",
            True,
        )
        if branch is None:
            logger.warning("Failed to build audio branch")
            return
        self._pipeline.add(branch)
        branch.sync_state_with_parent()
        pad.link(branch.get_static_pad("sink"))
        logger.info(f"Audio route → VB-Cable '{self._vb_cable}'")

    def _on_gst_data_channel(self, _webrtc, channel) -> None:
        """Browser opened a RTCDataChannel to us — we want the one named 'input'."""
        try:
            label = channel.get_property("label")
        except Exception:
            label = "?"
        logger.info(f"data-channel received: {label}")
        if label != "input":
            return
        self._input_channel = channel
        channel.connect("on-message-string", self._on_gst_input_message)

    def _on_gst_input_message(self, _channel, message: str) -> None:
        if self._on_input_message is None:
            return
        try:
            import json
            data = json.loads(message)
        except Exception:
            logger.warning(f"bad input json: {message[:80]!r}")
            return
        try:
            self._on_input_message(data)
        except Exception as e:
            logger.warning(f"input handler raised: {e}")

    def _on_ice_conn_state_change(self, _webrtc, _pspec):
        try:
            state = self._webrtc.get_property("ice-connection-state")
            logger.info(f"ICE conn state → {state.value_nick}")
        except Exception:
            pass

    def _on_peer_conn_state_change(self, _webrtc, _pspec):
        try:
            state = self._webrtc.get_property("connection-state")
            logger.info(f"Peer conn state → {state.value_nick}")
        except Exception:
            pass

    def _on_bus_message(self, _bus, msg: Gst.Message) -> None:
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            logger.error(f"GST error: {err.message} (src={msg.src.name if msg.src else '?'})")
            if dbg:
                logger.debug(f"GST debug: {dbg}")
        elif t == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            logger.warning(f"GST warning: {err.message}")
        elif t == Gst.MessageType.EOS:
            logger.info("GST EOS")

    # ─── Pad probe: count sent RTP packets ────────────────────────────

    def _count_sent_frames(self, _pad, info: Gst.PadProbeInfo):
        self.frames_sent += 1
        return Gst.PadProbeReturn.OK


# ─── Helpers ──────────────────────────────────────────────────────────────

def _find_first_by_factory(bin_: Gst.Bin, factory_name: str):
    """Walk a pipeline/bin and return first element whose factory name matches."""
    if bin_ is None:
        return None
    it = bin_.iterate_elements()
    while True:
        res, elem = it.next()
        if res == Gst.IteratorResult.OK:
            fac = elem.get_factory()
            if fac and fac.get_name() == factory_name:
                return elem
            if isinstance(elem, Gst.Bin):
                nested = _find_first_by_factory(elem, factory_name)
                if nested:
                    return nested
        elif res == Gst.IteratorResult.DONE:
            return None
        elif res == Gst.IteratorResult.RESYNC:
            it.resync()
        else:
            return None


# ─── Module-level Gst init + GLib main loop thread ────────────────────────

_gst_initialized = False
_glib_loop: Optional[GLib.MainLoop] = None
_glib_thread: Optional[threading.Thread] = None


def ensure_gst_initialized() -> None:
    """
    Gst.init() + a background thread running GLib.MainLoop so webrtcbin
    timers/DNS/etc. work.  aiohttp's asyncio loop does NOT drive GLib,
    so we need this dedicated thread.
    """
    global _gst_initialized, _glib_loop, _glib_thread
    if _gst_initialized:
        return
    Gst.init(None)
    _glib_loop = GLib.MainLoop()

    def _run():
        _glib_loop.run()

    _glib_thread = threading.Thread(target=_run, name="glib-main", daemon=True)
    _glib_thread.start()
    _gst_initialized = True
    logger.info("GStreamer initialized; GLib MainLoop running on bg thread")


def shutdown_gst() -> None:
    global _glib_loop, _glib_thread
    if _glib_loop is not None and _glib_loop.is_running():
        _glib_loop.quit()
    if _glib_thread is not None:
        _glib_thread.join(timeout=2.0)
    _glib_loop = None
    _glib_thread = None
