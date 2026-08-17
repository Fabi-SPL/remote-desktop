"""
Lucid Remote Desktop — Host Server (GStreamer / webrtcbin edition)
===================================================================
Drop-in replacement for host/server.py.  Runs under Python 3.9 +
GStreamer 1.28 (MSVC x86_64).

Architecture:
  Video  → d3d11screencapturesrc → nvd3d11h264enc → rtph264pay → webrtcbin
  Input  ← RTCDataChannel ← browser keyboard/mouse    (via gst_pipeline)
  Audio  ← webrtcbin ← opusdec → wasapi2sink("CABLE Input")

Why GStreamer (not aiortc):
  * C-speed media engine — no Python GIL on the hot path
  * Real production webrtcbin vs. aiortc's half-finished Python impl
  * Zero-copy D3D11 capture → NVENC encode path
  * NAL-aware RTP payloader (no keyframe corruption)
  * Real RTP congestion control inside webrtcbin

Wire protocol (signaling, on the WebSocket) — unchanged from v2:
  Client → Server:  {"type":"offer", "sdp":"..."}
                     {"type":"ice",   "candidate": {...}}
  Server → Client:  {"type":"init", monitors:[...], width, height, fps}
                     {"type":"answer", "sdp":"..."}
                     {"type":"ice",    "candidate": {...}}
                     {"type":"cursor_shape", w, h, hx, hy, bgra:base64}

Data channel "input" (client-initiated) — unchanged.

Usage:
  venv-gst/Scripts/python.exe host_gst/server.py [--port 8080] [--fps 30]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import datetime
import ipaddress
import json
import logging
import os
import platform
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from capture        import get_monitor_list
from input_handler  import InputHandler
from cursor_capture import CursorTracker
from gst_pipeline   import (
    GstWebRTCPipeline, ensure_gst_initialized, shutdown_gst,
)

CLIENT_DIR = Path(__file__).parent.parent / "client"
CERT_DIR   = Path(__file__).parent.parent / "certs"
CERT_FILE  = CERT_DIR / "lucid.crt"
KEY_FILE   = CERT_DIR / "lucid.key"
START_TIME = time.time()


# ═════════════════════════════════════════════════════════════════════════
#   Event log + logger (same as host/server.py)
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
    "INP": "\033[96m",  "SYS": "\033[97m", "RTC": "\033[95m", "GST": "\033[92m",
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


# ═════════════════════════════════════════════════════════════════════════
#   TLS cert (self-signed, auto-generated) — same as v2
# ═════════════════════════════════════════════════════════════════════════

def get_tailscale_ip() -> Optional[str]:
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
            if "LocalPort:" not in blk or f"{port}" not in blk:
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
    log("INFO", "SYS", f"Python {sys.version.split()[0]} (GStreamer edition)")
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

    # GStreamer element probe
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: E402
        Gst.init(None)
        elems = ["d3d11screencapturesrc", "nvd3d11h264enc",
                 "rtph264pay", "webrtcbin", "wasapi2sink", "opusdec"]
        missing = [e for e in elems
                   if Gst.ElementFactory.find(e) is None]
        if missing:
            log("ERROR", "GST", f"Missing GStreamer elements: {missing}")
        else:
            log("INFO", "GST", f"GStreamer elements ok ({', '.join(elems)})")
        results["gst_missing"] = missing
    except Exception as e:
        log("ERROR", "GST", f"GStreamer probe failed: {e}")

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
        "version":     "3.0.0-gst-webrtcbin",
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
        "gst_missing": app["preflight"].get("gst_missing", []),
        "active_sessions": len(_sessions),
        "recent_events":   eventlog.snapshot(80),
    }
    return web.json_response(snap, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
    })


async def healthz_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok", headers={"Access-Control-Allow-Origin": "*"})


# ═════════════════════════════════════════════════════════════════════════
#   Signaling: WebSocket ↔ GstWebRTCPipeline
# ═════════════════════════════════════════════════════════════════════════

_sessions: "set[GstWebRTCPipeline]" = set()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    addr = request.headers.get("X-Forwarded-For", request.remote)
    client_no = request.app.get("clients_total", 0) + 1
    request.app["clients_total"] = client_no
    log("INFO", "WS", f"[#{client_no}] CONNECT {addr}")

    monitors = get_monitor_list()
    fps      = request.app["fps"]
    loop     = asyncio.get_event_loop()

    # ── Per-session handlers ─────────────────────────────────────────
    input_handler  = InputHandler(monitors=monitors)
    cursor_tracker = CursorTracker()

    async def send_local_ice(mline_index: int, candidate: str):
        """Forward a server-side ICE candidate to the browser."""
        if ws.closed:
            return
        try:
            await ws.send_json({
                "type": "ice",
                "candidate": {
                    "candidate":     candidate,
                    "sdpMLineIndex": mline_index,
                    # sdpMid: webrtcbin emits only the mline index; browsers
                    # accept a candidate without sdpMid when sdpMLineIndex is set.
                },
            })
        except Exception as e:
            log("WARNING", "RTC", f"[#{client_no}] send local ICE failed: {e}")

    def on_input_message(data: dict):
        """Runs on GStreamer thread — must be fast/non-blocking."""
        try:
            t = data.get("type", "")
            if t == "switch_monitor":
                idx = int(data.get("index", 0))
                if 0 <= idx < len(monitors):
                    input_handler.set_monitor(idx)
                    # pipeline.switch_monitor is GStreamer-safe
                    if session_pipe is not None:
                        session_pipe.switch_monitor(idx)
                    log("INFO", "INP", f"[#{client_no}] switch_monitor → {idx}")
                return
            input_handler.handle(data)
        except Exception as e:
            log("WARNING", "INP", f"[#{client_no}] input err: {e}")

    # ── Build the pipeline BEFORE the offer arrives ──────────────────
    session_pipe: Optional[GstWebRTCPipeline] = None
    try:
        session_pipe = GstWebRTCPipeline(
            loop=loop,
            monitor_index=0,
            fps=fps,
            on_local_ice=send_local_ice,
            on_input_message=on_input_message,
        )
        session_pipe.start()
        _sessions.add(session_pipe)
    except Exception as e:
        log("ERROR", "GST", f"[#{client_no}] pipeline start failed: {e}")
        await ws.close()
        return ws

    monitor = monitors[0]
    await ws.send_json({
        "type":     "init",
        "monitors": monitors,
        "width":    monitor["width"],
        "height":   monitor["height"],
        "fps":      fps,
    })

    # ── Cursor shape streaming ───────────────────────────────────────
    async def stream_cursor():
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

    # ── Stats logging ────────────────────────────────────────────────
    async def stats_loop():
        last_sent = 0
        last_t    = time.time()
        while not ws.closed:
            await asyncio.sleep(5)
            now  = time.time()
            sent = session_pipe.frames_sent if session_pipe else 0
            dt   = now - last_t
            fps_sent = (sent - last_sent) / dt if dt else 0
            last_sent, last_t = sent, now
            log("INFO", "VID",
                f"[#{client_no}] rtp_sent={fps_sent:.1f} pkts/s total={sent}")

    stats_task = asyncio.ensure_future(stats_loop())

    # ── Signaling loop ───────────────────────────────────────────────
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
                try:
                    answer_sdp = await session_pipe.handle_remote_offer(d["sdp"])
                    await ws.send_json({"type": "answer", "sdp": answer_sdp})
                    log("INFO", "RTC", f"[#{client_no}] answer sent")
                except Exception as e:
                    log("ERROR", "RTC", f"[#{client_no}] offer handling failed: {e}")
                    break

            elif t == "ice":
                cand = d.get("candidate") or {}
                c_str = cand.get("candidate") or ""
                mline = cand.get("sdpMLineIndex")
                if c_str and mline is not None:
                    try:
                        session_pipe.add_remote_ice_candidate(int(mline), c_str)
                    except Exception as e:
                        log("WARNING", "RTC", f"[#{client_no}] add remote ICE: {e}")

    except Exception as e:
        log("ERROR", "WS", f"[#{client_no}] {e}")
    finally:
        cursor_task.cancel()
        stats_task.cancel()
        if session_pipe is not None:
            session_pipe.stop()
            _sessions.discard(session_pipe)
        log("INFO", "WS", f"[#{client_no}] DISCONNECT")

    return ws


async def on_shutdown(app):
    for pipe in list(_sessions):
        try:
            pipe.stop()
        except Exception:
            pass
    _sessions.clear()
    shutdown_gst()


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
    for noisy in ("aiohttp",):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Init GStreamer + background GLib MainLoop for webrtcbin
    ensure_gst_initialized()

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
    print(f"  │  Lucid Remote Desktop — GStreamer edition (v3)         │")
    print(f"  │  Local      →  {scheme}://localhost:{args.port}")
    print(f"  │  Tailscale  →  {scheme}://{ts_ip}:{args.port}")
    print(f"  │  Transport  →  WebRTC RTP/UDP (webrtcbin + NVENC)")
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
