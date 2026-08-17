"""
Lucid Remote Desktop — Host Server (WebRTC / aiortc edition)
==============================================================
Architecture:
  Video  → ScreenCaptureTrack → aiortc → H.264 RTP / UDP → WebRTC
  Input  ← RTCDataChannel ← browser keyboard/mouse
  Audio  ← WebRTC audio track ← browser mic → VB-Cable

Why WebRTC and not WebSocket:
  * UDP transport — no TCP head-of-line blocking
  * Built-in congestion control (REMB / TWCC)
  * Built-in adaptive bitrate
  * Frame dropping happens at the RTP layer, not in our queue
  * Browser uses the standard <video> element with hardware decode

Wire protocol (signaling, on the WebSocket):
  Client → Server:  {"type":"offer", "sdp":"..."}
                     {"type":"ice",   "candidate": {...}}
  Server → Client:  {"type":"init", monitors:[...], width, height}
                     {"type":"answer", "sdp":"..."}
                     {"type":"ice",    "candidate": {...}}
                     {"type":"cursor_shape", w, h, hx, hy, bgra:base64}

Data channel "input" (client → server, JSON):
  {"type":"mousemove", x, y}      # x,y normalised [0,1]
  {"type":"mousedown/up", button}
  {"type":"wheel", deltaX, deltaY}
  {"type":"keydown/up", code, key}
  {"type":"switch_monitor", index}

Usage:
  python server.py [--port 8080] [--fps 30]
"""

import sys

# NOTE: WindowsSelectorEventLoopPolicy was tested but breaks aiohttp's SSL
# server on Python 3.14 (accepts() never fire). Sticking with ProactorEventLoop.
# The research report suggested Selector for lower UDP jitter, but HTTPS is
# non-negotiable (WebCodecs secure context). Revisit if we drop HTTPS.

import argparse
import asyncio
import collections
import datetime
import ipaddress
import json
import logging
import os
import platform
import socket
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
from aiortc.contrib.media import MediaBlackhole

from capture        import get_monitor_list
from encoder        import ScreenCaptureTrack
from input_handler  import InputHandler
from audio_bridge   import AudioBridge
from cursor_capture import CursorTracker
from nvenc_patch    import apply_nvenc_patch, install_send_pacer

CLIENT_DIR = Path(__file__).parent.parent / "client"
CERT_DIR   = Path(__file__).parent.parent / "certs"
CERT_FILE  = CERT_DIR / "lucid.crt"
KEY_FILE   = CERT_DIR / "lucid.key"
START_TIME = time.time()


# ═════════════════════════════════════════════════════════════════════════
#   Event log + logger
# ═════════════════════════════════════════════════════════════════════════

class EventLog:
    def __init__(self, maxlen=300):
        self.events = collections.deque(maxlen=maxlen)

    def add(self, level, cat, msg):
        self.events.append({
            "t":   round(time.time(), 3),
            "ts":  time.strftime("%H:%M:%S"),
            "lvl": level,
            "cat": cat,
            "msg": msg,
        })

    def snapshot(self, n=100):
        return list(self.events)[-n:]


eventlog = EventLog()

_COLORS = {
    "DEBUG": "\033[90m", "INFO": "\033[37m", "WARNING": "\033[93m",
    "ERROR": "\033[91m", "RESET": "\033[0m",
    "NET": "\033[96m", "HTTP": "\033[94m", "WS": "\033[95m",
    "ENC": "\033[92m",  "VID": "\033[92m", "AUD": "\033[93m",
    "INP": "\033[96m",  "SYS": "\033[97m", "RTC": "\033[95m",
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        lvl   = record.levelname
        cat   = getattr(record, "cat", "SYS")
        color = _COLORS.get(lvl, "")
        catcol= _COLORS.get(cat, "")
        ts    = time.strftime("%H:%M:%S") + f".{int((time.time()%1)*1000):03d}"
        reset = _COLORS["RESET"]
        return f"{ts}  {catcol}[{cat:<4}]{reset} {color}{lvl:<5}{reset} {record.getMessage()}"


def log(level: str, cat: str, msg: str):
    eventlog.add(level, cat, msg)
    record = logging.LogRecord("lucid", getattr(logging, level, logging.INFO),
                                "", 0, msg, None, None)
    record.cat = cat
    _handler.emit(record)


_handler = logging.StreamHandler()
_handler.setFormatter(ColorFormatter())
_handler.setLevel(logging.DEBUG)

for noisy in ("aiohttp.access", "aiohttp.server", "aiohttp.web", "aioice", "aiortc"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ═════════════════════════════════════════════════════════════════════════
#   TLS cert (self-signed, auto-generated)
# ═════════════════════════════════════════════════════════════════════════

def get_tailscale_ip() -> str | None:
    for cmd in (["tailscale", "ip", "-4"],
                ["C:\\Program Files\\Tailscale\\tailscale.exe", "ip", "-4"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[0]
        except Exception:
            pass
    return None


def ensure_self_signed_cert():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return None

    CERT_DIR.mkdir(exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    log("INFO", "NET", "Generating self-signed TLS cert...")
    san_ips = {"127.0.0.1", "::1"}
    try:
        for _, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None):
            if isinstance(sa, tuple) and sa:
                san_ips.add(sa[0].split("%")[0])
    except Exception:
        pass
    ts = get_tailscale_ip()
    if ts:
        san_ips.add(ts)

    san_list = [x509.DNSName("localhost")]
    for ip in san_ips:
        try:
            san_list.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Lucid Remote Desktop"),
    ])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
            .not_valid_after (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))

    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    log("INFO", "NET", f"Cert written for IPs: {', '.join(sorted(san_ips))}")
    return CERT_FILE, KEY_FILE


def check_firewall_rule(port: int) -> dict:
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"],
            capture_output=True, text=True, timeout=8
        )
        out = r.stdout
        blocks = out.split("\n\n")
        matches = []
        for blk in blocks:
            if f"LocalPort:" not in blk or f"{port}" not in blk:
                continue
            lines = {}
            for line in blk.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    lines[k.strip()] = v.strip()
            if (lines.get("LocalPort", "").split(",")[0].strip() == str(port)
                    and lines.get("Direction") == "In"
                    and lines.get("Action") == "Allow"
                    and lines.get("Enabled") == "Yes"):
                matches.append(lines.get("Rule Name", "(unnamed)"))
        return {"rule_exists": bool(matches), "rules": matches}
    except Exception as e:
        return {"rule_exists": None, "error": str(e)}


def preflight(port: int) -> dict:
    log("INFO", "SYS", "=" * 60)
    log("INFO", "SYS", f"Python {sys.version.split()[0]}")
    log("INFO", "SYS", f"PID {os.getpid()}")

    results = {}
    ts = get_tailscale_ip()
    results["tailscale_ip"] = ts
    if ts:
        log("INFO", "NET", f"Tailscale IP: {ts}")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.close()
        log("INFO", "NET", f"Bind 0.0.0.0:{port}: ok")
    except OSError as e:
        log("ERROR", "NET", f"Bind 0.0.0.0:{port} failed: {e}")

    fw = check_firewall_rule(port)
    results["firewall"] = fw
    if fw.get("rule_exists"):
        log("INFO", "NET", f"Firewall: {fw['rules']}")
    elif fw.get("rule_exists") is False:
        log("WARNING", "NET", f"Firewall: no rule for {port} (run as admin to add)")

    try:
        mons = get_monitor_list()
        log("INFO", "SYS", f"Monitors: {len(mons)}")
        results["monitors"] = mons
    except Exception as e:
        log("ERROR", "SYS", f"Monitor enum failed: {e}")

    try:
        import sounddevice as sd
        has_cable = any("cable input" in d["name"].lower()
                        for d in sd.query_devices() if d["max_output_channels"] > 0)
        log("INFO" if has_cable else "WARNING", "AUD",
            f"VB-Cable: {'found' if has_cable else 'missing'}")
        results["vb_cable"] = has_cable
    except Exception as e:
        log("WARNING", "AUD", f"audio query: {e}")

    log("INFO", "SYS", "=" * 60)
    return results


# ═════════════════════════════════════════════════════════════════════════
#   HTTP handlers
# ═════════════════════════════════════════════════════════════════════════

async def index_handler(request: web.Request) -> web.Response:
    addr = request.headers.get("X-Forwarded-For", request.remote)
    log("INFO", "HTTP", f"GET /  ← {addr}")
    p = CLIENT_DIR / "index.html"
    if not p.exists():
        return web.Response(text="client/index.html not found", status=404)
    return web.FileResponse(p)


async def diag_handler(request: web.Request) -> web.Response:
    app = request.app
    snap = {
        "status":      "ok",
        "version":     "2.0.0-webrtc",
        "python":      sys.version.split()[0],
        "platform":    platform.platform(),
        "pid":         os.getpid(),
        "uptime_sec":  round(time.time() - START_TIME, 1),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "port":        app["port"],
        "fps":         app["fps"],
        "tailscale_ip":app["preflight"].get("tailscale_ip"),
        "firewall":    app["preflight"].get("firewall", {}),
        "monitors":    app["preflight"].get("monitors", []),
        "vb_cable":    app["preflight"].get("vb_cable", False),
        "active_pcs":  len(_pcs),
        "recent_events": eventlog.snapshot(80),
    }
    return web.json_response(snap, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
    })


async def healthz_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok", headers={"Access-Control-Allow-Origin": "*"})


# ═════════════════════════════════════════════════════════════════════════
#   WebRTC signaling over WebSocket
# ═════════════════════════════════════════════════════════════════════════

_pcs: set[RTCPeerConnection] = set()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    addr = request.headers.get("X-Forwarded-For", request.remote)
    client_no = request.app.get("clients_total", 0) + 1
    request.app["clients_total"] = client_no
    log("INFO", "WS", f"[#{client_no}] CONNECT {addr}")

    monitors = get_monitor_list()
    fps      = request.app["fps"]

    pc = RTCPeerConnection()
    _pcs.add(pc)

    capture_track  = ScreenCaptureTrack(monitor_index=0, fps=fps)
    input_handler  = InputHandler(monitors=monitors)
    audio_bridge   = AudioBridge()
    cursor_tracker = CursorTracker()

    pc.addTrack(capture_track)
    log("INFO", "ENC", f"[#{client_no}] track added: "
                       f"{capture_track.width}×{capture_track.height} @ {fps}fps")

    # ── transceiver for receiving browser mic ────────────────────────
    pc.addTransceiver("audio", direction="recvonly")

    # ── data channel — created CLIENT-SIDE (the offerer), received here ──
    @pc.on("datachannel")
    def _on_datachannel(channel):
        log("INFO", "RTC", f"[#{client_no}] data channel received: {channel.label}")
        if channel.label != "input":
            return

        @channel.on("message")
        def _on_input(message):
            try:
                d = json.loads(message)
                t = d.get("type", "")
                if t == "switch_monitor":
                    idx = d.get("index", 0)
                    if 0 <= idx < len(monitors):
                        input_handler.set_monitor(idx)
                        capture_track.switch_monitor(idx)
                        log("INFO", "INP", f"[#{client_no}] switch_monitor → {idx}")
                else:
                    if t == "mousemove":
                        capture_track.mouse_x = float(d.get("x", 0.5))
                        capture_track.mouse_y = float(d.get("y", 0.5))
                    input_handler.handle(d)
            except Exception as e:
                log("WARNING", "INP", f"[#{client_no}] input err: {e}")

    @pc.on("track")
    def _on_track(track):
        log("INFO", "RTC", f"[#{client_no}] received {track.kind} from browser")
        if track.kind == "audio":
            audio_bridge.start(track)
        else:
            blackhole = MediaBlackhole()
            blackhole.addTrack(track)

    @pc.on("connectionstatechange")
    async def _on_state():
        log("INFO", "RTC", f"[#{client_no}] state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            capture_track.stop()
            audio_bridge.stop()
            _pcs.discard(pc)

    # ── send init message right away ─────────────────────────────────
    await ws.send_json({
        "type":     "init",
        "monitors": monitors,
        "width":    capture_track.width,
        "height":   capture_track.height,
        "fps":      fps,
    })

    # ── cursor shape streaming task ──────────────────────────────────
    async def stream_cursor():
        import base64
        while not ws.closed:
            await asyncio.sleep(0.1)
            try:
                state = cursor_tracker.poll()
            except Exception:
                continue
            if state is None or not state.get("shape_update"):
                continue
            try:
                await ws.send_json({
                    "type": "cursor_shape",
                    "w":  state["width"],
                    "h":  state["height"],
                    "hx": state["hotspot_x"],
                    "hy": state["hotspot_y"],
                    "bgra": base64.b64encode(state["bgra"]).decode("ascii"),
                })
            except Exception:
                break

    cursor_task = asyncio.ensure_future(stream_cursor())

    # ── stats logging task ───────────────────────────────────────────
    async def stats_loop():
        last_cap, last_del, last_t = 0, 0, time.time()
        while not ws.closed:
            await asyncio.sleep(5)
            now = time.time()
            cap = capture_track.frames_captured
            dlv = capture_track.frames_delivered
            dt  = now - last_t
            cap_fps = (cap - last_cap) / dt if dt else 0
            dlv_fps = (dlv - last_del) / dt if dt else 0
            last_cap, last_del, last_t = cap, dlv, now
            timing = capture_track.timing_report()
            log("INFO", "VID",
                f"[#{client_no}] cap={cap_fps:.1f}fps dlv={dlv_fps:.1f}fps "
                f"recv_body={timing['recv_ms_avg']:.1f}ms "
                f"pipeline_gap={timing['gap_ms_avg']:.1f}ms")

    stats_task = asyncio.ensure_future(stats_loop())

    # ── signaling loop ───────────────────────────────────────────────
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except Exception:
                continue

            t = d.get("type")

            if t == "offer":
                offer = RTCSessionDescription(sdp=d["sdp"], type="offer")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                # Install RTP send pacer AFTER setLocalDescription —
                # transceivers/senders/transport exist only after this point.
                install_send_pacer(pc)

                # Wait for ICE gathering — simpler than trickle
                if pc.iceGatheringState != "complete":
                    gather_done = asyncio.Event()
                    @pc.on("icegatheringstatechange")
                    def _g():
                        if pc.iceGatheringState == "complete":
                            gather_done.set()
                    try:
                        await asyncio.wait_for(gather_done.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        log("WARNING", "RTC", f"[#{client_no}] ICE gather timeout")

                await ws.send_json({
                    "type": "answer",
                    "sdp":  pc.localDescription.sdp,
                })
                log("INFO", "RTC", f"[#{client_no}] answer sent")

    except Exception as e:
        log("ERROR", "WS", f"[#{client_no}] {e}")
    finally:
        cursor_task.cancel()
        stats_task.cancel()
        capture_track.stop()
        audio_bridge.stop()
        await pc.close()
        _pcs.discard(pc)
        log("INFO", "WS", f"[#{client_no}] DISCONNECT")

    return ws


async def on_shutdown(app):
    coros = [pc.close() for pc in _pcs]
    await asyncio.gather(*coros)
    _pcs.clear()


# ═════════════════════════════════════════════════════════════════════════
#   Entry point
# ═════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--fps",  type=int, default=30)
    ap.add_argument("--no-https", action="store_true")
    args = ap.parse_args()

    root_logger = logging.getLogger()
    root_logger.handlers = [_handler]
    root_logger.setLevel(logging.INFO)
    for noisy in ("aiohttp", "aioice", "aiortc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Apply NVENC patch BEFORE any RTCPeerConnection is created
    if apply_nvenc_patch():
        log("INFO", "ENC", "Hardware NVENC encoder enabled")
    else:
        log("WARNING", "ENC", "Falling back to software libx264 (slow)")

    pre = preflight(args.port)

    app = web.Application()
    app["port"]      = args.port
    app["fps"]       = args.fps
    app["preflight"] = pre

    app.router.add_get("/",        index_handler)
    app.router.add_get("/diag",    diag_handler)
    app.router.add_get("/healthz", healthz_handler)
    app.router.add_get("/ws",      ws_handler)
    if CLIENT_DIR.exists():
        app.router.add_static("/", CLIENT_DIR, show_index=False)
    app.on_shutdown.append(on_shutdown)

    ssl_ctx = None
    scheme  = "http"
    if not args.no_https:
        cp = ensure_self_signed_cert()
        if cp:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=str(cp[0]), keyfile=str(cp[1]))
            scheme = "https"

    ts_ip = pre.get("tailscale_ip") or "?"
    print()
    print("  ┌" + "─" * 56 + "┐")
    print(f"  │  Lucid Remote Desktop — WebRTC edition                 │")
    print(f"  │  Local      →  {scheme}://localhost:{args.port}")
    print(f"  │  Tailscale  →  {scheme}://{ts_ip}:{args.port}")
    print(f"  │  Transport  →  WebRTC RTP/UDP")
    print("  └" + "─" * 56 + "┘")
    print()

    try:
        web.run_app(app, host="0.0.0.0", port=args.port,
                    ssl_context=ssl_ctx, print=None, access_log=None)
    except OSError as e:
        if "10048" in str(e):
            log("ERROR", "NET", f"Port {args.port} in use — kill-port.bat")
        else:
            log("ERROR", "NET", f"run_app: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
