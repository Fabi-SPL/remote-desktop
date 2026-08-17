# Deep Research Brief: Custom WebRTC Remote Desktop — Latency Instability

## TL;DR

Building a custom Python-based remote desktop similar to Chrome Remote Desktop / RustDesk / Sunshine. Uses **aiortc** (WebRTC) + **h264_nvenc** (NVIDIA hardware encoder via PyAV/FFmpeg) + **dxcam** (DXGI screen capture). End-to-end latency is wildly unstable: **jumps from 7ms to 7000ms** over a Tailscale-direct 2ms LAN link with zero packet loss. FPS reported by the `<video>` element drops and spikes randomly.

**The question:** Why is the latency so unstable despite a fast network, hardware encoding, and WebRTC transport, and what is the standard way to fix it?

---

## The stack

| Layer | Implementation |
|---|---|
| Screen capture | `dxcam` 0.3.0 (DXGI Desktop Duplication API, cv2 color backend) |
| Encoder | `h264_nvenc` via PyAV `av.CodecContext.create("h264_nvenc", "w")` |
| Encoder wrapper | Monkey-patched `aiortc.codecs.h264.H264Encoder._encode_frame` to use NVENC instead of libx264 |
| WebRTC server | `aiortc` 1.14.0 (Python) |
| Signaling | Custom WebSocket (SDP offer/answer + ICE full-gather, no trickle) |
| Transport | WebRTC RTP over UDP / DTLS |
| Client | Chromium browser (Chrome 146 on ChromeOS) |
| Client video | Standard HTML5 `<video>` element with `srcObject = e.streams[0]` |
| Data channel | Client-side created `RTCDataChannel("input")` — keyboard/mouse JSON |
| Audio | WebRTC audio track (browser mic → server → VB-Cable virtual device) |
| Network | Both peers on Tailscale, path is **direct** (verified), **2ms raw RTT**, same physical LAN |
| Host | Windows 11 (26200), Python 3.14, RTX 2080 Super, Ryzen CPU |

## NVENC config (currently)

```python
codec.options = {
    "preset":      "p1",      # fastest latency preset
    "tune":        "ull",     # ultra-low-latency
    "rc":          "cbr",
    "bf":          "0",       # no B-frames
    "g":           "60",      # keyframe every 2s at 30fps
    "delay":       "0",
    "zerolatency": "1",
    "profile":     "high",
    "level":       "40",
}
codec.bit_rate  = target_bitrate  # dynamic, from aiortc
codec.pix_fmt   = "yuv420p"
codec.framerate = fractions.Fraction(30, 1)
codec.time_base = fractions.Fraction(1, 30)
```

NVENC encode time measured in isolation: **10.8ms per frame at 1080p30** — well within the 33ms budget.

---

## Symptoms (what we actually observe)

1. Latency reported by `RTCStatsReport.candidate-pair.currentRoundTripTime` jumps wildly: **7ms → 200ms → 7000ms → 50ms → …**
2. `inbound-rtp.video.framesPerSecond` on the client fluctuates — sometimes 30, sometimes 5, sometimes 0.
3. Server-side capture is consistent: `capture=30fps delivered=30fps` in server logs (the ScreenCaptureTrack produces exactly 30 fps).
4. Video visibly tears and stalls — looks like frames arrive in bursts, not smoothly.
5. Tailscale path is confirmed **direct** (not DERP-relayed): `tailscale ping` returns `pong via <peer-lan-ip>:41287 in 2ms`.
6. Happens even with the Chromebook on the SAME physical LAN as the PC.

## What we've ruled out

- **Network path**: direct Tailscale, 2ms RTT, same physical LAN.
- **TCP head-of-line blocking**: we switched from WebSocket+WebCodecs to WebRTC specifically to eliminate this. UDP transport is now in use.
- **Software encoder being slow**: we replaced libx264 with h264_nvenc. Isolated benchmark shows 10.8ms/frame at 1080p.
- **Firewall**: explicit inbound TCP/UDP rule for port 8080 (HTTPS signaling), WebRTC uses ephemeral UDP ports.
- **Tailscale relay (DERP)**: verified direct path.
- **Bitrate**: tried 8M, 5M, 3M, 2M. Latency spikes regardless.
- **Frame capture rate**: server-side confirmed at 30 fps; dxcam delivers frames on schedule.

---

## Hypotheses we want researched

### H1. aiortc's single-threaded event loop contention
aiortc runs the encoder, packetizer, RTP sender, ICE, DTLS, and SCTP in the **same asyncio event loop**. Each frame goes through: `VideoStreamTrack.recv()` → `H264Encoder._encode_frame()` (currently our patched NVENC path) → `_split_bitstream()` → RTP packetization → DTLS encryption → `sock.sendto()`. If any step blocks the loop (e.g., `codec.encode()` is synchronous and runs in the loop), it stalls everything else.

**Question:** Is the standard fix to run the encoder in a `run_in_executor`, or is there a known pattern in the aiortc community for hardware encoder offload?

### H2. aiortc adaptive bitrate behavior
aiortc has some REMB/TWCC handling but it's known to be incomplete vs. libwebrtc. Under normal WebRTC, if the encoder stalls, the sender adapts. In aiortc, the sender may not react properly, causing build-ups.

**Question:** Does aiortc actually implement proper pacing/congestion control, or does it just dump every frame to the wire as fast as `encode()` returns? What do production aiortc deployments do?

### H3. Keyframe size spikes
At 3-5 Mbps with 2-second keyframe interval, keyframes are ~100-200 KB. A single keyframe has to be fully RTP-packetized, DTLS-encrypted, and sent in one tick of the event loop. On a real libwebrtc stack, the pacer spreads this over multiple ms. aiortc may not pace, causing a fat burst every 2 seconds that floods the socket buffer and stalls the loop for ~100ms.

**Question:** Does aiortc have a send pacer? If not, is there a way to reduce keyframe size (lower quality keyframes? longer GOP? periodic intra-refresh instead of full keyframes?) OR is the standard fix to paced delivery manually?

### H4. asyncio event loop starvation from blocking CPU work
NVENC encoding is fast (10ms per frame) but it's still **synchronous CPU work** on the main thread. At 30fps, that's 300ms of CPU time per second, on a loop that also needs to handle signaling, data channels, audio, ICE heartbeats, and RTP sending. Add frame copying, YUV420p conversion, Annex B stripping, and the loop has very little idle time.

**Question:** What's the proper architecture for offloading the encoder step? Should every `recv()` call hand the frame to `loop.run_in_executor(None, encode, frame)`? Is there prior art?

### H5. aiortc's H264 packetizer performance
`aiortc.codecs.h264` has a pure-Python NAL splitter (`_split_bitstream`) and RTP packetizer (`_packetize_fu_a`, `_packetize_stap_a`). These are Python loops that run per-frame. At 30fps × keyframe=200KB, this might be expensive.

**Question:** Has anyone profiled aiortc's H264 packetization at high frame rates? Are there known bottlenecks or C extensions that replace this?

### H6. Chromium's jitter buffer mode
Chrome's `<video>` element defaults to a longer jitter buffer for smooth playback, which adds latency. `RTCRtpReceiver.playoutDelayHint = 0` or `jitterBufferTarget = 0` might reduce this but still won't help if the sender is bursty.

**Question:** What's the correct browser-side configuration for lowest-latency playback (sub-100ms) of a WebRTC H.264 video track?

### H7. `recv()` timing vs. actual send
Our `ScreenCaptureTrack.recv()` uses `await self.next_timestamp()` which paces at the configured fps. But the returned frame still goes through aiortc's internal sender, which may not send immediately. If `recv()` is called faster than the encoder can handle, aiortc might queue frames internally.

**Question:** How does aiortc handle the case when a VideoStreamTrack produces frames faster than the encoder/sender can process them?

---

## Things to research

1. **GitHub issues / discussions in aiortc repo** about latency, jitter, frame drops, NVENC, high-resolution streaming. Look for maintainer comments about pacing, congestion control, and asyncio loop contention.

2. **Production users of aiortc for low-latency video**: which projects actually use it for real-time remote desktop / game streaming / live video? What architectural tricks do they use? (Projects to look at: [janus](https://janus.conf.meetecho.com/) is C not Python, but look at Python projects that use aiortc similarly. `owt-server`? `streamlit-webrtc`? `picamera2` streaming examples?)

3. **Compare to libwebrtc / Pion / Go mediasoup**: for the same Python aiortc workload in a different language, what latency do people get? Is aiortc's Python runtime the fundamental ceiling, or is there headroom we're leaving on the table?

4. **PyAV / FFmpeg NVENC low-latency flags**: are there additional flags beyond `preset=p1 tune=ull zerolatency=1` that reduce encoder latency further? Things like `-rc cbr_ld_hq`, `-rc-lookahead 0`, `-forced-idr 1`, `-spatial_aq 0`, `-temporal_aq 0`, `-multipass 0`, `-2pass 0`, `-b_adapt 0`, `-surfaces 1`, `-delay 0`, `-no-scenecut 1`.

5. **NVENC session limit**: is there a limit on simultaneous NVENC sessions per GPU on consumer cards (GeForce), and can that cause contention / stalling if something else on the system is using NVENC?

6. **asyncio selector policy on Windows**: Python 3.14 on Windows uses `ProactorEventLoop` by default. Is there a known issue where UDP socket writes from within the proactor loop are slower or more variable than with the selector loop? Should we explicitly switch to `asyncio.SelectorEventLoop`?

7. **dxcam cv2 backend**: the frame capture goes through `cv2.cvtColor` on the CPU. At 1080p30 that's a real amount of work. Is there a way to get dxcam to deliver BGR directly from the GPU without a color conversion hop?

8. **Windows TCP/UDP buffer tuning**: are there registry keys or `netsh int tcp set global` settings that affect how Windows schedules UDP sends for real-time traffic?

---

## What we DON'T want the research session to tell us

- "Switch to Sunshine / Moonlight / CRD / TeamViewer" — we know these exist. We want **custom**. The user explicitly does not want to rely on someone else's implementation.
- "Rewrite in Rust/C++" — unless there's evidence this is the only path. Prefer answers that let us stay in Python.
- Generic WebRTC explanations. We already understand the stack.
- Suggestions that require a TURN server. Both peers have direct UDP connectivity.

---

## What we DO want back

1. **Root cause(s)** of the instability, ranked by likelihood with specific evidence/links.
2. **Concrete code changes** (file paths, function names, exact flags) to fix each one.
3. **Expected result after each fix** — "changing X reduces latency from Y to Z in case K".
4. **If aiortc truly has a ceiling**, what's that ceiling numerically (e.g., "aiortc can't sustain more than 720p30 without burst latency spikes regardless of encoder speed")? And what's the cheapest way around it (e.g., run encoder in subprocess, use a Python C extension for RTP packetization, switch to GStreamer's `webrtcbin` with Python bindings, etc.)?
5. **The specific aiortc issues / commits / PRs / forks** that matter for this use case.

---

## Current code location

All paths below are relative to the repository root.

Key files:
- `host/server.py` — aiohttp + aiortc signaling + RTCPeerConnection setup
- `host/encoder.py` — `ScreenCaptureTrack(VideoStreamTrack)` — uses dxcam, yields `av.VideoFrame`
- `host/nvenc_patch.py` — monkey-patches `aiortc.codecs.h264.H264Encoder._encode_frame`
- `host/cursor_capture.py` — GetCursorInfo / GetIconInfo Win32 cursor capture
- `host/input_handler.py` — SendInput via ctypes
- `host/audio_bridge.py` — pulls WebRTC audio track → VB-Cable via sounddevice
- `client/index.html` — single-file browser client, RTCPeerConnection + `<video>`

---

## One more thing: the actual measured numbers

Server log (30 seconds of a session, 1080p30 target):
```
22:43:46  [ENC ] track added: 1920×1080 @ 30fps
22:48:51  [VID ] capture=28.2fps delivered=28.2fps   ← server side, stable
22:48:56  [VID ] capture=30.0fps delivered=30.0fps
22:49:01  [VID ] capture=30.0fps delivered=30.0fps
22:49:06  [VID ] capture=30.0fps delivered=30.0fps
22:49:11  [VID ] capture=30.0fps delivered=30.0fps
```
Client-side `RTCStatsReport`:
```
currentRoundTripTime: 0.007 → 0.210 → 7.123 → 0.050 → 0.003 → 2.1 → ...
inbound-rtp.framesPerSecond: 28 → 5 → 0 → 31 → 12 → ...
```

The server thinks everything is fine. The wire is the problem.

---

## Please return

A structured research report with:
1. Ranked root-cause hypotheses with citations (GitHub issues, blog posts, papers, aiortc source line refs).
2. Concrete fix recommendations with code snippets that fit my stack.
3. An honest assessment of whether Python+aiortc can actually reach <100ms stable latency for 1080p30 remote desktop, or whether the whole transport layer needs to be in a different language.
