#!/usr/bin/env python3
"""
KODI-Sync Daemon — event-driven refactor.

Protocol matches MODIFICATION.md spec exactly.
All Kodi interaction via WebSocket JSON-RCP (no xbmc dependency).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import socket
import sys
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, Optional, Tuple, Union

import socketserver
import websocket

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder, BuildError
from pythonosc.udp_client import SimpleUDPClient


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

mcast_group: str = "239.0.0.239"
MCAST_PORT = 9000
mcast_iface: str = "0.0.0.0"

KODI_WS_URL = "ws://127.0.0.1:9090/jsonrpc"
KODI_WS_TIMEOUT = 5.0

ETH_IFACE = "eth0"

reply_port: int = 5006

VIDEOS_DIR = "/storage/videos"
MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}
FFPROBE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffprobe")
GOP_FFPROBE_TIMEOUT = 10.0

LOG_LEVEL = logging.INFO

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

log = logging.getLogger("multiscreen-sync")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ═══════════════════════════════════════════════════════════════
#  UTILITY
# ═══════════════════════════════════════════════════════════════

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def get_mac(iface: str = ETH_IFACE) -> str:
    with open(f"/sys/class/net/{iface}/address") as f:
        return f.read().strip()

def _is_video_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXTS

def _probe_keyframes_ms_ffprobe(path: str) -> Tuple[int, int]:
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=GOP_FFPROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0, 0
    if r.returncode != 0:
        return 0, 0

    second_pts: Optional[float] = None
    last_pts: Optional[float] = None
    idr_count = 0
    for line in r.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].startswith("K"):
            try:
                pts = float(parts[0])
            except ValueError:
                continue
            idr_count += 1
            if idr_count == 2:
                second_pts = pts
            last_pts = pts

    if last_pts is None:
        return 0, 0
    start_ms = int((second_pts or last_pts) * 1000)
    end_ms = int(last_pts * 1000)
    return start_ms, end_ms

def _probe_video_info_ffprobe(path: str) -> Tuple[int, str]:
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=avg_frame_rate",
        "-of", "csv=p=0",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=GOP_FFPROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0, "0.000fps"
    if r.returncode != 0:
        return 0, "0.000fps"

    duration_s = 0.0
    fps = 0.0
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "/" in line:
            try:
                num, den = line.split("/", 1)
                fps = float(num) / float(den) if float(den) else 0.0
            except ValueError:
                pass
        else:
            try:
                duration_s = float(line)
            except ValueError:
                pass
    return int(duration_s * 1000), f"{fps:.3f}fps"

def _probe_keyframes_ms(path: str) -> Tuple[Union[int, float], Union[int, float]]:
    return _probe_keyframes_ms_ffprobe(path)

def _ms_to_hms(ms: int) -> str:
    if ms < 0:
        ms = 0
    h = ms // 3_600_000
    m = (ms % 3_600_000) // 60_000
    s = (ms % 60_000) // 1_000
    milli = ms % 1_000
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"

def _hms_to_ms(hms: str) -> int:
    try:
        parts = hms.split(":")
        h, m = int(parts[0]), int(parts[1])
        sec_parts = parts[2].split(".")
        s = int(sec_parts[0])
        ms = int(sec_parts[1]) if len(sec_parts) > 1 else 0
        return h * 3_600_000 + m * 60_000 + s * 1_000 + ms
    except (IndexError, ValueError):
        return 0

# ═══════════════════════════════════════════════════════════════
#  OSC CONTEXT
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class OSCContext:
    address: str
    source_ip: str
    source_port: int
    received_at: float = field(default_factory=time.time)

    @property
    def reply_target(self) -> Tuple[str, int]:
        return (self.source_ip, reply_port)

OSCHandler = Callable[..., None]

# ═══════════════════════════════════════════════════════════════
#  KODI JSON-RPC OVER WEBSOCKET
# ═══════════════════════════════════════════════════════════════

class KodiClient:
    def __init__(self, url: str, timeout: float = KODI_WS_TIMEOUT):
        self.url = url
        self.timeout = timeout
        self._ws: Optional[websocket.WebSocket] = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._responses: Dict[int, list] = {}
        self._on_notification: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._connect()

    @property
    def on_notification(self):
        return self._on_notification

    @on_notification.setter
    def on_notification(self, cb):
        self._on_notification = cb

    def _connect(self):
        log.info("connecting to Kodi: %s", self.url)
        self._ws = websocket.create_connection(self.url, timeout=self.timeout)
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="KodiReader", daemon=True,
        )
        self._reader_thread.start()

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except Exception:
                if self._stop.is_set():
                    break
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]):
        if isinstance(msg.get("id"), int) and "method" not in msg:
            with self._lock:
                waiter = self._responses.pop(msg["id"], None)
            if waiter is not None:
                waiter[1] = msg
                waiter[0].set()
        elif "method" in msg:
            cb = self._on_notification
            if cb is not None:
                try:
                    cb(msg["method"], msg.get("params", {}))
                except Exception:
                    log.exception("kodi notification handler error")

    def _ensure_connected(self):
        try:
            self._ws.send(b"")
        except Exception:
            log.warning("Kodi WS lost, reconnecting...")
            try:
                self._ws.close()
            except Exception:
                pass
            self._stop.set()
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2)
            self._connect()

    def call(self, method: str,
             params: Optional[Dict[str, Any]] = None,
             timeout: float = 5.0) -> Dict[str, Any]:
        with self._lock:
            self._ensure_connected()
            msg_id = self._next_id
            self._next_id += 1
            waiter: list = [threading.Event(), None]
            self._responses[msg_id] = waiter
            payload = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
            self._ws.send(json.dumps(payload))

        if not waiter[0].wait(timeout):
            with self._lock:
                self._responses.pop(msg_id, None)
            raise TimeoutError(f"Kodi {method} timeout after {timeout}s")

        resp = waiter[1]
        if resp is None:
            raise RuntimeError(f"Kodi {method}: no response")
        if "error" in resp:
            raise RuntimeError(f"Kodi {method}: {resp['error']}")
        return resp.get("result", {})

    def call_no_result(self, method: str,
                       params: Optional[Dict[str, Any]] = None,
                       timeout: float = 5.0) -> None:
        """Fire-and-forget: call API, ignore result."""
        self.call(method, params, timeout)

    def get_version(self) -> Dict[str, int]:
        return self.call("Application.GetProperties",
                         {"properties": ["version"]})["version"]

    def close(self):
        with self._lock:
            self._stop.set()
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2)
                self._reader_thread = None
            if self._ws is not None:
                try:
                    self._ws.close()
                finally:
                    self._ws = None

# ═══════════════════════════════════════════════════════════════
#  STATE MACHINE — phases & command context
# ═══════════════════════════════════════════════════════════════

class Phase(Enum):
    IDLE = auto()
    OPENING = auto()      # Player.Play sent, waiting for OnAVStart
    PAUSING = auto()      # PlayPause(False) sent, waiting for OnPause
    SEEKING = auto()      # Player.Seek sent, waiting for OnSeek
    SEEK_SETTLING = auto()# Seek executed, polling until position stabilises
    DELAYING = auto()     # Timer running before resume
    RESUMING = auto()     # PlayPause(True) sent, waiting for OnResume
    SEEK_WAIT = auto()    # /seek command: just seek (no pause needed)

@dataclass
class Command:
    ctx: OSCContext
    phase: Phase
    cmd_type: str = ""            # "alignment_ready" | "alignment_play" | "seek"
    target_pos_ms: int = 0
    target_idx: int = 0
    delay_ms: int = 0
    was_playing: bool = False
    file_path: str = ""
    actual_ms: int = 0
    total_ms: int = 0
    timer: Optional[threading.Timer] = None
    settle_start: float = 0.0
    settle_readings: list = field(default_factory=list)

# ═══════════════════════════════════════════════════════════════
#  MULTICAST OSC RECEIVER
# ═══════════════════════════════════════════════════════════════

class _MulticastUDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data: bytes = self.request[0]
        src_ip, src_port = self.client_address

        try:
            msg = OscMessage(data)
        except Exception as e:
            log.error("OSC parse error from %s:%d: %s", src_ip, src_port, e)
            return

        receiver: "MulticastOSCReceiver" = self.server.receiver
        handler = receiver.handlers.get(msg.address.lower())
        if handler is None:
            log.warning("no handler for %s from %s:%d (params=%s)",
                        msg.address, src_ip, src_port, msg.params)
            return

        ctx = OSCContext(address=msg.address, source_ip=src_ip, source_port=src_port)
        log.info("OSC <- %s from %s:%d args=%s",
                 msg.address, src_ip, src_port, tuple(msg.params))
        try:
            handler(ctx, *msg.params)
        except Exception:
            log.exception("handler error for %s from %s:%d",
                          ctx.address, ctx.source_ip, ctx.source_port)

class MulticastOSCServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        mreq = struct.pack(
            "=4s4s",
            socket.inet_aton(mcast_group),
            socket.inet_aton(mcast_iface),
        )
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 64)
        super().server_bind()
        log.info("joined mcast %s:%d on iface %s",
                 mcast_group, self.server_address[1], mcast_iface)

class MulticastOSCReceiver:
    def __init__(self, group: str = mcast_group,
                 port: int = MCAST_PORT,
                 iface: str = mcast_iface):
        self.group = group
        self.port = port
        self.iface = iface
        self.handlers: Dict[str, OSCHandler] = {}
        self._server: Optional[MulticastOSCServer] = None
        self._thread: Optional[threading.Thread] = None
        self._joined: bool = False

    @property
    def is_joined(self):
        return self._joined

    def map(self, address: str, handler: OSCHandler):
        self.handlers[address.lower()] = handler

    def start(self):
        self._server = MulticastOSCServer(("0.0.0.0", self.port), _MulticastUDPHandler)
        self._server.receiver = self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="MulticastOSCServer", daemon=True,
        )
        self._thread.start()
        self._joined = True

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def join_group(self):
        if self._server is None:
            return
        try:
            mreq = struct.pack("=4s4s", socket.inet_aton(self.group), socket.inet_aton(self.iface))
            self._server.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self._joined = True
        except OSError as e:
            log.warning("join_group: %s", e)

    def leave_group(self):
        if self._server is None:
            return
        try:
            mreq = struct.pack("=4s4s", socket.inet_aton(self.group), socket.inet_aton(self.iface))
            self._server.socket.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            self._joined = False
        except OSError as e:
            log.warning("leave_group: %s", e)

# ═══════════════════════════════════════════════════════════════
#  OSC UNICAST SENDER
# ═══════════════════════════════════════════════════════════════

class OSCUnicastSender:
    def __init__(self):
        self._clients: Dict[Tuple[str, int], SimpleUDPClient] = {}
        self._lock = threading.Lock()

    def _get(self, ip: str, port: int) -> SimpleUDPClient:
        key = (ip, port)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = SimpleUDPClient(ip, port)
                self._clients[key] = client
            return client

    def send(self, target_ip: str, target_port: int, address: str, *args: Any):
        client = self._get(target_ip, target_port)
        builder = OscMessageBuilder(address=address)
        for a in args:
            if isinstance(a, bool):
                builder.add_arg(1 if a else 0)
            elif isinstance(a, (int, float, str, bytes, bytearray)):
                builder.add_arg(a)
            else:
                builder.add_arg(str(a))
        try:
            client.send(builder.build())
        except BuildError as e:
            log.error("OSC build error for %s: %s", address, e)

# ═══════════════════════════════════════════════════════════════
#  DAEMON MAIN CLASS
# ═══════════════════════════════════════════════════════════════

class KodiSyncDaemon:
    def __init__(self):
        self.kodi = KodiClient(KODI_WS_URL)
        self.sender = OSCUnicastSender()
        self.receiver = MulticastOSCReceiver()
        self._stop = threading.Event()
        self._last_ctx: Optional[OSCContext] = None

        # ── State machine ──
        self._lock = threading.Lock()
        self._cmd: Optional[Command] = None
        self._current_volume: float = 80.0
        self._report_addr: str = mcast_group
        self._active_player_speed: Optional[int] = None

        # Event queue: Kodi notifications arrive via callback → queued → processed
        # sequentially by _event_loop.
        self._event_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._event_thread = threading.Thread(
            target=self._event_loop, name="EventProcessor", daemon=True,
        )
        self._event_thread.start()

        self.kodi.on_notification = self._on_kodi_event_raw
        self._register_handlers()

        # Default CPU affinity
        try:
            ncpu = os.cpu_count() or 0
            if ncpu > 0:
                self._set_cpu_affinity(tuple(0 if i < ncpu - 1 else 1 for i in range(ncpu)))
        except Exception:
            pass

    # ── Reply helpers ──

    def reply(self, ctx: OSCContext, address: str, *args: Any):
        target_ip, target_port = ctx.reply_target
        self.sender.send(target_ip, target_port, address, *args)
        log.info("OSC -> %s to %s:%d args=%s", address, target_ip, target_port, args)

    def _reply_last(self, address: str, *args: Any):
        if self._last_ctx is None:
            return
        self.reply(self._last_ctx, address, *args)

    def _send_osc(self, address: str, *args: Any):
        """Send OSC to the last known report target (for spontaneous events)."""
        self.sender.send(self._report_addr, reply_port, address, *args)
        log.info("OSC -> %s to %s:%d args=%s", address, self._report_addr, reply_port, args)

    @staticmethod
    def _set_cpu_affinity(masks: Tuple[int, ...]):
        cpus = {i for i, m in enumerate(masks) if m}
        if not cpus:
            return
        try:
            os.sched_setaffinity(0, cpus)
            log.info("cpu_affinity -> CPU%s", sorted(cpus))
        except OSError as e:
            log.warning("cpu_affinity failed: %s", e)

    # ── Kodi helpers ──

    def _time_dict_to_ms(self, t: Dict[str, Any]) -> int:
        if not t:
            return 0
        return (t.get("hours", 0) * 3_600_000
                + t.get("minutes", 0) * 60_000
                + t.get("seconds", 0) * 1_000
                + t.get("milliseconds", 0))

    def _get_player_info(self) -> Tuple[str, int, int]:
        """Returns (file_path, current_ms, total_ms) or ("", 0, 0) if no player."""
        try:
            active = self.kodi.call("Player.GetActivePlayers", timeout=3.0)
        except Exception:
            return "", 0, 0
        if not active:
            return "", 0, 0
        pid = active[0].get("playerid", 1)
        try:
            props = self.kodi.call("Player.GetProperties", {
                "playerid": pid,
                "properties": ["time", "totaltime"],
            }, timeout=3.0)
        except Exception:
            return "", 0, 0
        current_ms = self._time_dict_to_ms(props.get("time", {}))
        total_ms = self._time_dict_to_ms(props.get("totaltime", {}))
        try:
            item = self.kodi.call("Player.GetItem",
                                   {"playerid": pid, "properties": ["file"]}, timeout=3.0)
            file_path = (item.get("item") or {}).get("file", "")
        except Exception:
            file_path = ""
        return file_path, current_ms, total_ms

    def _is_player_paused(self) -> Optional[bool]:
        try:
            result = self.kodi.call("Player.GetProperties", {
                "playerid": 1, "properties": ["speed"],
            }, timeout=3.0)
            speed = result.get("speed")
            if speed is None:
                return None
            return speed == 0
        except Exception:
            return None

    def _get_speed(self) -> Optional[int]:
        try:
            r = self.kodi.call("Player.GetProperties", {
                "playerid": 1, "properties": ["speed"],
            }, timeout=3.0)
            return r.get("speed")
        except Exception:
            return None

    def _kodi_play_pause(self, play: Optional[bool] = None) -> None:
        params: dict = {"playerid": 1}
        if play is not None:
            params["play"] = play
        self.kodi.call_no_result("Player.PlayPause", params, timeout=5.0)

    # ── Event queue ──

    def _on_kodi_event_raw(self, method: str, params: Dict[str, Any]):
        """Called from reader thread. Just queue the event for sequential processing."""
        self._event_queue.put((method, params))

    def _event_loop(self):
        """Dedicated thread: processes Kodi events and polls seek settle."""
        SETTLE_POLL_S = 0.05
        while not self._stop.is_set():
            try:
                method, params = self._event_queue.get(timeout=SETTLE_POLL_S)
            except queue.Empty:
                self._poll_settle()
                continue
            try:
                self._process_event(method, params)
            except Exception:
                log.exception("event processing error: %s", method)

    def _process_event(self, method: str, params: Dict[str, Any]):
        log.info("Kodi event: %s", method)
        with self._lock:
            cmd = self._cmd
            if cmd is None:
                # ── Spontaneous event (no command in progress) ──
                self._report_spontaneous_state(method, params)
                return

            # ── Command in progress — advance state machine ──
            phase = cmd.phase

            # ── OPENING → PAUSING ──
            if phase == Phase.OPENING and method == "Player.OnAVStart":
                cmd.phase = Phase.PAUSING
                log.debug("State: OPENING → PAUSING")
                self._kodi_play_pause(play=False)
                return

            # ── PAUSING → SEEKING ──
            if phase == Phase.PAUSING and method == "Player.OnPause":
                cmd.phase = Phase.SEEKING
                log.debug("State: PAUSING → SEEKING")
                seek_time = {
                    "hours": cmd.target_pos_ms // 3_600_000,
                    "minutes": (cmd.target_pos_ms % 3_600_000) // 60_000,
                    "seconds": (cmd.target_pos_ms % 60_000) // 1_000,
                    "milliseconds": cmd.target_pos_ms % 1_000,
                }
                self.kodi.call_no_result("Player.Seek",
                                          {"playerid": 1, "value": {"time": seek_time}},
                                          timeout=5.0)
                return

            # ── SEEKING → handle OnSeek → SEEK_SETTLING ──
            if phase == Phase.SEEKING and method == "Player.OnSeek":
                file_path, current_ms, total_ms = self._get_player_info()
                cmd.file_path = file_path
                cmd.actual_ms = current_ms
                cmd.total_ms = total_ms
                cmd.phase = Phase.SEEK_SETTLING
                cmd.settle_start = time.monotonic()
                cmd.settle_readings = [current_ms]
                log.debug("State: SEEKING → SEEK_SETTLING (initial: %dms)", current_ms)
                return

            # ── SEEK_WAIT → handle OnSeek → SEEK_SETTLING ──
            if phase == Phase.SEEK_WAIT and method == "Player.OnSeek":
                file_path, current_ms, total_ms = self._get_player_info()
                cmd.file_path = file_path
                cmd.actual_ms = current_ms
                cmd.total_ms = total_ms
                cmd.phase = Phase.SEEK_SETTLING
                cmd.settle_start = time.monotonic()
                cmd.settle_readings = [current_ms]
                log.debug("State: SEEK_WAIT → SEEK_SETTLING (initial: %dms)", current_ms)
                return

            # ── RESUMING → IDLE ──
            if phase == Phase.RESUMING and method == "Player.OnResume":
                file_path, current_ms, total_ms = self._get_player_info()
                cmd.file_path = file_path
                cmd.actual_ms = current_ms
                addr = "/kodi/alignment/play" if cmd.cmd_type == "alignment_play" else "/kodi/alignment/seek"
                self.reply(cmd.ctx, addr,
                           cmd.target_idx, file_path, current_ms, _ms_to_hms(total_ms))
                self._cmd = None
                log.debug("State: RESUMING → IDLE (%s)", cmd.cmd_type)
                return

    def _timer_resume(self):
        """Called from Timer thread (DELAYING phase)."""
        with self._lock:
            cmd = self._cmd
            if cmd is None or cmd.phase != Phase.DELAYING:
                return
            cmd.phase = Phase.RESUMING
        self._kodi_play_pause(play=True)

    # ── Seek settle polling ──

    SETTLE_TIMEOUT_S = 2.0
    SETTLE_MAX_READINGS = 3
    SETTLE_THRESHOLD_MS = 4   # 1/4 frame at 60 fps ≈ 4.17ms

    def _poll_settle(self):
        with self._lock:
            cmd = self._cmd
            if cmd is None or cmd.phase not in (Phase.SEEK_SETTLING,):
                return

        _, current_ms, _ = self._get_player_info()

        with self._lock:
            if self._cmd is not cmd or cmd.phase != Phase.SEEK_SETTLING:
                return
            cmd.settle_readings.append(current_ms)
            if len(cmd.settle_readings) > self.SETTLE_MAX_READINGS:
                cmd.settle_readings.pop(0)

            elapsed = time.monotonic() - cmd.settle_start
            readings = cmd.settle_readings

            if len(readings) >= self.SETTLE_MAX_READINGS:
                stable = all(
                    abs(r - readings[-1]) < self.SETTLE_THRESHOLD_MS
                    for r in readings
                )
                if stable:
                    log.debug("Seek settled after %.0fms @ %dms (readings=%s)",
                              elapsed * 1000, readings[-1], readings)
                    self._finalize_seek(cmd, readings[-1])
                    return

            if elapsed >= self.SETTLE_TIMEOUT_S:
                log.warning("Seek settle timeout after %.1fs, using %dms (target=%dms, diff=%+dms)",
                            elapsed, current_ms,
                            cmd.target_pos_ms, current_ms - cmd.target_pos_ms)
                self._finalize_seek(cmd, current_ms)

    def _finalize_seek(self, cmd: Command, current_ms: int):
        file_path = cmd.file_path
        total_ms = cmd.total_ms
        log.debug("State: SEEK_SETTLING → ...")

        if cmd.cmd_type == "alignment_ready":
            log.debug("State: SEEK_SETTLING → IDLE (alignment_ready)")
            self.reply(cmd.ctx, "/kodi/alignment/ready",
                       cmd.target_idx, file_path, current_ms, _ms_to_hms(total_ms))
            self._cmd = None

        elif cmd.cmd_type == "alignment_play":
            log.debug("State: SEEK_SETTLING → DELAY (alignment_play)")
            self.reply(cmd.ctx, "/kodi/alignment/play",
                       cmd.target_idx, file_path, current_ms, _ms_to_hms(total_ms))
            if cmd.delay_ms > 0:
                cmd.phase = Phase.DELAYING
                cmd.timer = threading.Timer(cmd.delay_ms / 1000.0,
                                             self._timer_resume)
                cmd.timer.start()
            else:
                cmd.phase = Phase.RESUMING
                self._kodi_play_pause(play=True)

        elif cmd.cmd_type == "seek":
            log.debug("State: SEEK_SETTLING → ... (seek)")
            self.reply(cmd.ctx, "/kodi/alignment/seek",
                       cmd.target_idx, file_path, current_ms, _ms_to_hms(total_ms))
            if cmd.delay_ms > 0 and cmd.was_playing:
                cmd.phase = Phase.DELAYING
                cmd.timer = threading.Timer(cmd.delay_ms / 1000.0,
                                             self._timer_resume)
                cmd.timer.start()
            elif cmd.was_playing:
                cmd.phase = Phase.RESUMING
                self._kodi_play_pause(play=True)
            else:
                self._cmd = None

    # ── Spontaneous event reporting ──

    def _report_spontaneous_state(self, method: str, params: Dict[str, Any]):
        """Report a Kodi player event via /kodi/state when no command is active."""
        event_name = self._method_to_event(method)
        if event_name is None:
            return

        # /kodi/error is reported separately (not via /kodi/state)
        if event_name == "onPlayBackError":
            err_code = (params.get("data") or {}).get("error", -1)
            self._send_osc("/kodi/error", err_code, "playback failed")
            return

        file_path, current_ms, total_ms = self._get_player_info()
        is_paused = 0
        if event_name in ("onPlayBackPaused",):
            is_paused = 1
        elif event_name in ("onPlayBackResumed", "onAVStarted", "onPlayBackStarted"):
            is_paused = 0

        if event_name == "onPlayBackSeek":
            paused_info = self._is_player_paused()
            if paused_info is not None:
                is_paused = 1 if paused_info else 0

        is_stopped = 1 if event_name in ("onPlayBackStopped",) else 0
        fn = file_path if file_path else ""
        ct = max(0, current_ms) if not is_stopped else 0

        self._send_osc("/kodi/state", is_paused, is_stopped,
                       event_name, fn, ct, _ms_to_hms(total_ms))

    @staticmethod
    def _method_to_event(method: str) -> Optional[str]:
        mapping = {
            "Player.OnPlay": "onPlayBackStarted",
            "Player.OnAVStart": "onAVStarted",
            "Player.OnResume": "onPlayBackResumed",
            "Player.OnPause": "onPlayBackPaused",
            "Player.OnStop": "onPlayBackStopped",
            "Player.OnSeek": "onPlayBackSeek",
            "Player.OnError": "onPlayBackError",
        }
        return mapping.get(method)

    # ── OSC command registration ──

    def _register_handlers(self):
        for addr, handler in [
            ("/discover", self.on_discover),
            ("/playlist", self.on_playlist),
            ("/alignment/ready", self.on_alignment_ready),
            ("/alignment/play", self.on_alignment_play),
            ("/GetProperties", self.on_get_properties),
            ("/playpause", self.on_playpause),
            ("/play", self.on_play),
            ("/pause", self.on_pause),
            ("/stop", self.on_stop),
            ("/seek", self.on_seek),
            ("/setLoop", self.on_set_loop),
            ("/volume", self.on_volume),
            ("/mute", self.on_mute),
            ("/member", self.on_member),
            ("/cpuAffinity", self.on_cpu_affinity),
            ("/multicast/reply", self.on_multicast_reply),
            ("/multicast/host", self.on_multicast_host),
            ("/restart", self.on_restart),
            ("/reboot", self.on_reboot),
            ("/shutdown", self.on_shutdown),
        ]:
            self.receiver.map(addr, handler)

    # ═══════════════════════════════════════════════════════════
    #  COMMAND HANDLERS
    # ═══════════════════════════════════════════════════════════

    def on_alignment_ready(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if len(osc_args) < 2:
            self.reply(ctx, "/kodi/alignment/ready", -1, "", 0, "bad_args")
            return
        try:
            idx = int(osc_args[0])
            pos_ms = int(osc_args[1])
        except (TypeError, ValueError):
            self.reply(ctx, "/kodi/alignment/ready", -1, "", 0, "bad_int")
            return

        log.info("/alignment/ready idx=%d pos=%dms", idx, pos_ms)

        cmd = Command(ctx=ctx, phase=Phase.OPENING, cmd_type="alignment_ready",
                      target_idx=idx, target_pos_ms=pos_ms)
        with self._lock:
            self._cmd = cmd

        # Fire: Play from playlist
        try:
            self.kodi.call("Player.Open", {
                "item": {"playlistid": 1, "position": idx},
            }, timeout=10.0)
        except Exception as e:
            with self._lock:
                self._cmd = None
            self.reply(ctx, "/kodi/alignment/ready", idx, "", 0, f"open_exc: {e}")

    def on_alignment_play(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if len(osc_args) < 3:
            self.reply(ctx, "/kodi/alignment/play", -1, "", 0, "bad_args")
            return
        try:
            idx = int(osc_args[0])
            pos_ms = int(osc_args[1])
            delay_ms = int(osc_args[2])
        except (TypeError, ValueError):
            self.reply(ctx, "/kodi/alignment/play", -1, "", 0, "bad_int")
            return

        log.info("/alignment/play idx=%d pos=%dms delay=%dms", idx, pos_ms, delay_ms)

        cmd = Command(ctx=ctx, phase=Phase.OPENING, cmd_type="alignment_play",
                      target_idx=idx, target_pos_ms=pos_ms, delay_ms=delay_ms)
        with self._lock:
            self._cmd = cmd

        try:
            self.kodi.call("Player.Open", {
                "item": {"playlistid": 1, "position": idx},
            }, timeout=10.0)
        except Exception as e:
            with self._lock:
                self._cmd = None
            self.reply(ctx, "/kodi/alignment/play", idx, "", 0, f"open_exc: {e}")

    def on_seek(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if not osc_args or osc_args[0] is None:
            self.reply(ctx, "/kodi/error", -1, "bad_args")
            return
        try:
            pos_ms = int(osc_args[0])
            delay_ms = int(osc_args[1]) if len(osc_args) > 1 else 0
        except (TypeError, ValueError):
            self.reply(ctx, "/kodi/error", -1, "bad_int")
            return

        speed = self._get_speed()
        if speed is None:
            self.reply(ctx, "/kodi/error", -1, "no_active_player")
            return

        was_playing = (speed != 0)
        idx = 0
        try:
            pl = self.kodi.call("Playlist.GetProperties",
                                 {"playlistid": 1, "properties": ["position"]}, timeout=3.0)
            idx = pl.get("position", 0)
        except Exception:
            pass

        log.info("/seek pos=%dms delay=%dms was_playing=%s idx=%d", pos_ms, delay_ms, was_playing, idx)

        if was_playing:
            cmd = Command(ctx=ctx, phase=Phase.SEEKING, cmd_type="seek",
                          target_pos_ms=pos_ms, target_idx=idx,
                          delay_ms=delay_ms, was_playing=True)
        else:
            cmd = Command(ctx=ctx, phase=Phase.SEEK_WAIT, cmd_type="seek",
                          target_pos_ms=pos_ms, target_idx=idx,
                          delay_ms=delay_ms, was_playing=False)

        with self._lock:
            self._cmd = cmd

        seek_time = {
            "hours": pos_ms // 3_600_000,
            "minutes": (pos_ms % 3_600_000) // 60_000,
            "seconds": (pos_ms % 60_000) // 1_000,
            "milliseconds": pos_ms % 1_000,
        }
        try:
            self.kodi.call_no_result("Player.Seek",
                                      {"playerid": 1, "value": {"time": seek_time}},
                                      timeout=5.0)
        except Exception as e:
            with self._lock:
                self._cmd = None
            self.reply(ctx, "/kodi/error", -1, f"seek_exc: {e}")

    def on_play(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        log.info("/play")
        self._kodi_play_pause(play=True)

    def on_pause(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        log.info("/pause")
        self._kodi_play_pause(play=False)

    def on_playpause(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        log.info("/playpause")
        self._kodi_play_pause()

    def on_stop(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        log.info("/stop")
        try:
            self.kodi.call_no_result("Player.Stop", {"playerid": 1}, timeout=5.0)
        except Exception as e:
            self.reply(ctx, "/kodi/error", -1, f"stop_exc: {e}")

    def on_get_properties(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        file_path, current_ms, total_ms = self._get_player_info()
        if not file_path:
            self.reply(ctx, "/kodi/error", -1, "no video playing")
            return
        self.reply(ctx, "/kodi/GetProperties", current_ms, _ms_to_hms(total_ms))

    def on_volume(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if not osc_args:
            return
        try:
            vol = int(osc_args[0])
            vol = max(0, min(100, vol))
        except (TypeError, ValueError):
            return
        self._current_volume = float(vol)
        try:
            self.kodi.call_no_result("Application.SetVolume", {"volume": vol}, timeout=3.0)
        except Exception as e:
            log.warning("SetVolume failed: %s", e)
        self.reply(ctx, "/kodi/volume", vol)

    def on_mute(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if not osc_args:
            return
        try:
            value = int(osc_args[0])
        except (TypeError, ValueError):
            return
        target_muted = (value == 1)
        try:
            props = self.kodi.call("Application.GetProperties",
                                    {"properties": ["muted"]}, timeout=3.0)
            is_muted = props.get("muted", False)
        except Exception:
            is_muted = False

        if target_muted:
            if not is_muted:
                try:
                    props = self.kodi.call("Application.GetProperties",
                                           {"properties": ["volume"]}, timeout=3.0)
                    db_val = props.get("volume", 0.0)
                    if db_val > -60:
                        self._current_volume = min(100.0, max(0.0, db_val))
                except Exception:
                    pass
                self.kodi.call_no_result("Application.SetMute", {"mute": True}, timeout=3.0)
            self.reply(ctx, "/kodi/mute", 0, self._current_volume)
        else:
            if is_muted:
                self.kodi.call_no_result("Application.SetMute", {"mute": False}, timeout=3.0)
            self.reply(ctx, "/kodi/mute", 1, self._current_volume)

    def on_set_loop(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if not osc_args or not osc_args[0]:
            self.reply(ctx, "/kodi/setLoop", "unknown")
            return
        mode = str(osc_args[0])
        if mode == "all":
            self.kodi.call_no_result("Player.SetRepeat", {"playerid": 1, "repeat": "all"}, timeout=3.0)
        elif mode == "one":
            self.kodi.call_no_result("Player.SetRepeat", {"playerid": 1, "repeat": "one"}, timeout=3.0)
        elif mode == "off":
            self.kodi.call_no_result("Player.SetRepeat", {"playerid": 1, "repeat": "off"}, timeout=3.0)
        self.reply(ctx, "/kodi/setLoop", mode)

    def on_discover(self, ctx: OSCContext, *osc_args: Any):
        try:
            ver = self.kodi.get_version()
        except Exception as e:
            log.error("get_version failed: %s", e)
            return
        version_str = f"{ver.get('major', 0)}.{ver.get('minor', 0)}.{ver.get('patch', 0)}"
        try:
            local_ip = get_local_ip()
            mac = get_mac(ETH_IFACE)
        except Exception as e:
            log.error("get local info failed: %s", e)
            return
        self.reply(ctx, "/daemon/discover", local_ip, mac, version_str)

    def on_playlist(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        self._send_osc("/kodi/playlist", 0, "Please wait, ffprobe is processing...")

        directory = str(osc_args[0]) if (osc_args and osc_args[0]) else VIDEOS_DIR
        threading.Thread(target=self._build_playlist, args=(ctx, directory), daemon=True).start()

    def _build_playlist(self, ctx: OSCContext, directory: str):
        attempts = [directory]
        if directory != VIDEOS_DIR:
            attempts.append(VIDEOS_DIR)
        for d in attempts:
            if self._try_build_playlist(ctx, d):
                return

    def _try_build_playlist(self, ctx: OSCContext, directory: str) -> bool:
        try:
            dir_resp = self.kodi.call("Files.GetDirectory", {
                "directory": directory,
                "media": "video",
                "properties": ["file"],
                "sort": {"method": "file", "order": "ascending", "ignorearticle": True},
            })
        except Exception:
            self.reply(ctx, "/kodi/playlist/state", "ERROR", directory, "INVALID DIRECTORY")
            return False

        files = [it["file"] for it in (dir_resp or {}).get("files", []) if it.get("file")]
        ext = MEDIA_EXTS
        video_files = [f for f in files if os.path.splitext(f)[1].lower() in ext]

        if not video_files:
            self.reply(ctx, "/kodi/playlist/state", "ERROR", directory, "NO VIDEO FILE")
            return False

        try:
            self.kodi.call_no_result("Playlist.Clear", {"playlistid": 1})
        except Exception as e:
            log.error("playlist clear failed: %s", e)
            self.reply(ctx, "/kodi/playlist/state", "ERROR", directory, "UNKNOWN ERROR")
            return False

        item_args: list = []
        for idx, fp in enumerate(video_files):
            try:
                self.kodi.call_no_result("Playlist.Insert", {
                    "playlistid": 1, "position": idx, "item": {"file": fp},
                })
            except Exception as e:
                log.error("Insert %s failed: %s", fp, e)
                continue

            local_path = fp[len("file://"):] if fp.startswith("file://") else fp
            basename = os.path.basename(fp)
            duration_ms, fps_str = _probe_video_info_ffprobe(local_path)
            second_idr_ms, last_idr_ms = _probe_keyframes_ms(local_path)

            item_args.extend([idx, basename, _ms_to_hms(duration_ms), fps_str,
                              second_idr_ms, last_idr_ms])

        count = len(item_args) // 6
        if count == 0:
            self.reply(ctx, "/kodi/playlist/state", "ERROR", directory, "UNKNOWN ERROR")
            return False

        log.info("playlist built: %d items from %s", count, directory)
        self.reply(ctx, "/kodi/playlist/state", "OK", directory, "OK")
        self.reply(ctx, "/kodi/playlist", count, *item_args)
        return True

    def on_member(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        try:
            socket.inet_aton(ctx.source_ip)
        except OSError:
            self.reply(ctx, "/daemon/member", "Invalid IP address")
            return
        if not osc_args:
            self.reply(ctx, "/daemon/member", "Invalid command")
            return
        action = str(osc_args[0]).lower()
        if action == "---":
            if self.receiver.is_joined:
                self.reply(ctx, "/daemon/member",
                           f"I am in multicast group {mcast_group}:{MCAST_PORT}")
            else:
                self.reply(ctx, "/daemon/member", "I am not in the multicast group")
        elif action == "join":
            self.receiver.join_group()
            self.reply(ctx, "/daemon/member", "is Join multicast")
        elif action == "leave":
            self.receiver.leave_group()
            self.reply(ctx, "/daemon/member", "is Leave multicast")
        else:
            self.reply(ctx, "/daemon/member", "Invalid command")

    def on_multicast_reply(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if not osc_args:
            return
        try:
            new_port = int(osc_args[0])
            if new_port < 1 or new_port > 65535:
                return
        except (ValueError, TypeError):
            return
        global reply_port
        reply_port = new_port
        log.info("reply_port -> %d", new_port)
        for _ in range(3):
            self.reply(ctx, "/daemon/config", f"Reporting Port: {new_port}")
            time.sleep(0.3)

    def on_multicast_host(self, ctx: OSCContext, *osc_args: Any):
        self._last_ctx = ctx
        if not osc_args:
            return
        new_group = str(osc_args[0])
        parts = new_group.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            self.reply(ctx, "/daemon/config", "bad host")
            return
        global mcast_group, mcast_iface
        old = mcast_group
        self.receiver.leave_group()
        mcast_group = new_group
        self.receiver.group = new_group
        self.receiver.join_group()
        log.info("mcast_group %s -> %s", old, new_group)
        self.reply(ctx, "/daemon/config", f"Host: {new_group}")

    def on_cpu_affinity(self, ctx: OSCContext, *osc_args: Any):
        if len(osc_args) < 1:
            self.reply(ctx, "/daemon/CPU", "bad_args")
            return
        masks = tuple(int(a) if a else 0 for a in osc_args)
        self._set_cpu_affinity(masks)
        self.reply(ctx, "/daemon/CPU", *masks)

    def on_restart(self, ctx: OSCContext, *osc_args: Any):
        self.reply(ctx, "/system/power", 0, "KODI RESTARTING......")
        self.kodi.call_no_result("Application.Quit")

    def on_reboot(self, ctx: OSCContext, *osc_args: Any):
        self.reply(ctx, "/system/power", 1, "SYSTEM REBOOTING......")
        self.kodi.call_no_result("System.Reboot")

    def on_shutdown(self, ctx: OSCContext, *osc_args: Any):
        self.reply(ctx, "/system/power", 2, "KODI SHUTTING DOWN......")
        self.kodi.call_no_result("System.Shutdown")

    # ── Lifecycle ──

    def run(self):
        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGTERM, self._signal)

        log.info("starting receiver...")
        self.receiver.start()
        log.info("multiscreen-sync running. Ctrl-C to stop.")

        while not self._stop.is_set():
            self._stop.wait(0.5)

        self.stop()

    def _signal(self, signum, frame):
        log.info("got signal %d, stopping...", signum)
        self._stop.set()

    def stop(self):
        log.info("shutting down...")
        # Cancel any pending timer
        with self._lock:
            if self._cmd and self._cmd.timer:
                self._cmd.timer.cancel()
        try:
            self.receiver.stop()
        except Exception:
            pass
        try:
            self.kodi.close()
        except Exception:
            pass
        log.info("bye.")


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    daemon = KodiSyncDaemon()
    daemon.run()
    return 0

if __name__ == "__main__":
    sys.exit(main())
