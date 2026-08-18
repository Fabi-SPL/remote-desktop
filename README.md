# remote-desktop

A self-hosted remote desktop for Windows that streams the screen to a plain browser tab over WebRTC, with mouse, keyboard, cursor and microphone passthrough.

## Why it exists

I wanted to reach my desktop from a Chromebook without installing anything on the client, and I wanted to learn WebRTC by writing the sending half rather than reading about it. Chrome Remote Desktop already does this properly. What made the project worth finishing was the latency bug, which took considerably longer to understand than the feature set did to build.

## The interesting part

The symptom: round-trip time measured in the browser swung between 7 ms and 7000 ms on a Tailscale-direct path with 2 ms raw RTT and no packet loss. Server-side logs insisted capture and delivery were both a steady 30 fps. The wire was lying somewhere between the two.

**Replacing aiortc's encoder.** aiortc encodes H.264 with libx264 synchronously, inside the asyncio event loop that also runs ICE, DTLS, RTP send and the input data channel. `apply_nvenc_patch()` in `host/nvenc_patch.py` swaps `aiortc.codecs.h264.H264Encoder._encode_frame` for one that builds `av.CodecContext.create("h264_nvenc", "w")` with `preset=p1`, `tune=ull`, `rc-lookahead=0` and `surfaces=2`, probing NVENC once at startup and falling back to the original path if the probe fails. Measured in isolation, NVENC encodes a 1080p frame in 10.8 ms. The same patch replaces `_split_bitstream`, because aiortc fragments the bitstream at fixed 1200-byte offsets without looking for NAL start codes, which corrupts any keyframe carrying SPS, PPS and an IDR slice together. A corrupt keyframe earns a PLI from the browser, which produces another keyframe, which corrupts identically. `_nal_aware_split_bitstream` cuts on Annex-B start codes first and leaves MTU fragmentation to FU-A. The `target_bitrate` setter is also overridden to a no-op so aiortc's bandwidth estimator cannot rebuild the encoder mid-stream.

**The pacer, and why it is gone.** aiortc ships no RTP send pacer, which is confirmed in aiortc discussion #965 and visible in `rtcrtpsender.py`. A keyframe at 3 Mbps is 150 or more RTP packets, and they leave in a tight `for` loop that starves ICE heartbeats, which fits the 7-second RTT readings well. So I wrote a leaky-bucket pacer, `asyncio.sleep(9600 / bitrate)` between packets, roughly 3.2 ms at 3 Mbps. Delivered frame rate fell to 2 or 3 fps and keyframes stalled for most of a second. The cause is that Windows keeps its default timer granularity near 15.6 ms, so `asyncio.sleep(0.0032)` does not sleep for 3.2 ms, it sleeps for about 15. Timed directly, fifty consecutive `asyncio.sleep(3.2ms)` calls consumed 756.7 ms of wall clock, so a fifty-packet keyframe was locked out for three quarters of a second and per-frame `pipeline_gap` rose to between 222 and 504 ms. The pacer was fixing a congested-WAN problem this link does not have while creating a far worse local one. `install_send_pacer()` in `host/nvenc_patch.py` is now a documented no-op that keeps exactly one line of the original: it calls `winmm.timeBeginPeriod(1)` so every *other* short sleep inside aiortc, RTCP timing and retransmits included, is accurate to 1 ms instead of 15. Keyframe bursts were shrunk the boring way instead, by raising GOP from 60 to 120 frames.

That second finding is the one I would defend in an interview. The measurement that mattered was not on the network, it was on the clock the sleep call was using.

## How it works

```
Windows host                                       Browser client
------------                                       --------------

dxcam (DXGI Desktop Duplication)
   |  BGRA frames, dedicated capture thread
   v
deque(maxlen=1)        <-- stale frames dropped at the source, not the encoder
   |
   v
ScreenCaptureTrack.recv()          (asyncio, non-blocking)
   |  av.VideoFrame
   v
H264Encoder._encode_frame          [patched -> h264_nvenc]
   |  Annex-B bitstream
   v
H264Encoder._split_bitstream       [patched -> NAL-aware]
   |  NAL units
   v
RTP packetize + DTLS/SRTP
   |
   +---> RTP over UDP ------------------------>  <video srcObject>
   |                                             jitterBufferTarget = 50 ms
   +---> WebSocket: SDP, ICE, cursor_shape --->  cursor overlay
   |
   +<--- RTCDataChannel "input" --------------   pointer and key events
   |         |
   |         v
   |      SendInput via ctypes
   |
   +<--- WebRTC audio track ------------------   getUserMedia microphone
             |
             v
          VB-Cable virtual input device
```

There is a second, newer host under `host_gst/` that replaces the whole aiortc stack with GStreamer `webrtcbin`, running `d3d11screencapturesrc ! nvd3d11h264enc ! rtph264pay ! webrtcbin` for a zero-copy D3D11 capture and encode path. One detail there was expensive to learn: the encoder tail cannot be linked to `webrtcbin.sink_0` when the pipeline is built, because the caps event propagates into webrtcbin before any DTLS or SRTP transport exists and the rejection cascades back up as an "Internal data stream error" on the capture source. `_attach_video_sendpath()` in `host_gst/gst_pipeline.py` therefore terminates the encoder in a `fakesink`, then uses a blocking pad probe to swap the tail onto `webrtcbin.sink_0` after `set-local-description`.

## Stack

Python, aiortc, PyAV with FFmpeg h264_nvenc, dxcam for DXGI capture, aiohttp for signaling, ctypes for the Win32 input and cursor calls, GStreamer webrtcbin in the second host, and a single-file vanilla JavaScript client.

## Run it

Windows only, and NVENC needs an NVIDIA GPU. Without one the aiortc host still runs, falling back to libx264, and it will be slow.

```bat
git clone <repo> remote-desktop
cd remote-desktop
install.bat
start.bat
```

`install.bat` creates `venv\` and installs `host\requirements.txt`. `start.bat` launches `host\server.py`, which generates a self-signed TLS certificate into `certs\` on first run, binds `0.0.0.0:8080` over HTTPS, and prints the local and Tailscale URLs it detected. Open that URL and accept the certificate warning, since a secure context is required for `getUserMedia`.

Flags and environment:

```bat
venv\Scripts\python host\server.py --port 8080 --fps 30
venv\Scripts\python host\server.py --no-https        REM plain HTTP, breaks mic capture
set PORT=9000                                        REM default port, read by argparse
set LUCID_REMOTE_HOST=my-host.tailnet.ts.net         REM display only, used by the .bat banners
```

Microphone passthrough needs [VB-Cable](https://vb-audio.com/Cable/) installed, so that the host has a `CABLE Input` playback device to write the browser's audio into. Everything else works without it. `install-autostart.bat` registers a logon Task Scheduler entry if you want the host running headless, and `uninstall-autostart.bat` removes it.

The GStreamer host has no installer yet. It needs Python 3.9, a GStreamer 1.28 MSVC x86_64 build with `nvd3d11h264enc` and `wasapi2sink` present, and PyGObject able to import `gi`. Then `venv-gst\Scripts\python host_gst\server.py`.

## State of it

This is a working prototype that I use, not a finished product. Being specific about that:

- **It does not hit its latency target.** The last measured aiortc session captured at 30.0 fps and delivered 8.6 fps to the browser, with 42.5 ms spent inside `recv()` and a 70.4 ms pipeline gap per frame. Video is watchable and input feels close to immediate at lower resolutions, but 1080p30 is not there. The remaining bottleneck is the Python RTP send path, which is exactly why `host_gst/` exists.
- **`host_gst/` is unfinished.** The pipeline builds, the SDP exchange completes and the send path attaches. It has no requirements file, no install script and no fallback if a GStreamer element is missing.
- **There is no authentication.** None at all. Anyone who can reach port 8080 gets full keyboard and mouse control of the machine. I only ever run it inside a Tailscale tailnet, and that tailnet is the entire security model. Do not port-forward this.
- **Hardcoded values that should be configuration:** bitrate at 3 Mbps and GOP at 120 in `host/nvenc_patch.py`, 8 Mbps in `host_gst/gst_pipeline.py`, MTU 1200, monitor index 0 on every new connection, and the VB-Cable device name as the literal string `CABLE Input`.
- **`start.bat` advertises a `--bitrate` flag that `server.py` never implemented.** Left visible rather than quietly deleted, because it is the kind of drift a reviewer should expect to find.
- **Windows only, structurally.** dxcam, SendInput, GetCursorInfo, winmm, d3d11screencapturesrc and wasapi2sink all have to be replaced for any other platform.
- **Never tested off my own LAN and tailnet.** One STUN server is configured and there is no TURN, so anything behind symmetric NAT is unexplored. Trickle ICE is not implemented either; the server waits up to 5 seconds for a full gather before answering.
- **Audio is one direction.** Browser microphone reaches the host. Host audio does not come back.
- **Monitor switching tears down and rebuilds the capture device**, which stalls the aiortc path visibly for a moment.
- **No tests.** Diagnosis was done with the `/diag` endpoint, the timing counters in `ScreenCaptureTrack.timing_report()` and reading server logs.

[`WRITEUP-756MS.md`](WRITEUP-756MS.md) is the full account of the pacer, written for someone who has not read this repo.

`RESEARCH-BRIEF.md` is the write-up of the latency problem as it stood before the fixes above, including the hypotheses that turned out to be wrong. It is kept because the wrong ones are the useful part.

## License

Apache 2.0. See `LICENSE` and `NOTICE`.
