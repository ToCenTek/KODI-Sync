#!/usr/bin/env python3
"""
daemon.py
=========

KODI-Sync 守护进程（框架版）

职责
----
1. 加入组播组 239.0.0.69:9000，监听 OSC 消息。
2. 收到 /discover 时：
   a. 通过 WebSocket 向本机 Kodi（:9090）查询版本；
   b. 读取本机非环回 IPv4 地址和 /sys/class/net/eth0/address 的 MAC；
   c. 通过 OSC 地址 /kodi/discover 单播回传到组播源（source_ip:5006）。
3. 与 Kodi 保持 WebSocket 长连接，供后续命令复用。

依赖
----
    pip install python-osc websocket-client

后续扩展
--------
只需要在 KodiSyncDaemon._register_handlers() 中继续 map 新的 OSC 路径，
并实现对应的回调方法即可。所有回调签名：

    def on_xxx(self, ctx: OSCContext, *osc_args) -> None: ...

其中 ctx 是 OSCContext（不可变 dataclass），包含：
- address         : 收到的 OSC 路径，如 "/play"
- source_ip       : 发送方 IP
- source_port     : 发送方端口（组播包源端口，仅供参考）
- reply_target    : (source_ip, REPLY_PORT) 便捷属性

回复时用：
    self.reply(ctx, "/kodi/xxx", *args)        # 显式指定回复路径
    self.reply_mirror(ctx, *args)              # 自动按 ctx.address 派生 /kodi/<path>
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

import socketserver
import websocket  # websocket-client

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder, BuildError
from pythonosc.udp_client import SimpleUDPClient


# ============================================================
# 配置
# ============================================================
MCAST_GROUP = "239.0.0.69"
MCAST_PORT = 9000
# 加入组播的本地接口；0.0.0.0 = 所有接口
MCAST_IFACE = "0.0.0.0"

KODI_WS_URL = "ws://127.0.0.1:9090/jsonrpc"
KODI_WS_TIMEOUT = 5.0  # 初次连接超时（秒）

ETH_IFACE = "eth0"
SYS_MAC_PATH = f"/sys/class/net/{ETH_IFACE}/address"

# 单播回复端口：固定上报到组播源的 5006（与组播包的源端口无关）
REPLY_PORT = 5006

# 播放列表构建
VIDEOS_DIR = "/storage/videos"
PLAYLIST_ID = 1
MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}
# GOP 探测：调绝对路径 ffprobe，不依赖 PATH
FFPROBE_BIN = "/storage/bin/ffprobe"
GOP_FFPROBE_TIMEOUT = 10.0  # 单文件 GOP 探测超时（秒）

LOG_LEVEL = logging.INFO


# ============================================================
# 日志
# ============================================================
log = logging.getLogger("kodi-sync")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


# ============================================================
# 工具：本机 IP / MAC
# ============================================================
def get_local_ip() -> str:
    """
    获取本机非环回 IPv4 地址。

    原理：创建 UDP socket 并 connect 一个外部地址（不发包），
    让内核选路填上本机出口 IP，再 getsockname 读出。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def get_mac(iface: str = ETH_IFACE) -> str:
    """读取 /sys/class/net/<iface>/address 获取 MAC（小写、冒号分隔）"""
    with open(f"/sys/class/net/{iface}/address", "r") as f:
        return f.read().strip()
        return f.read().strip()


# ============================================================
# 工具：GOP 后端探测与单文件 GOP 抓取
# ============================================================
def _is_video_file(path: str) -> bool:
    """检查文件后缀是否在 MEDIA_EXTS 中（小写比较）。"""
    return os.path.splitext(path)[1].lower() in MEDIA_EXTS


def _probe_keyframes_ms_ffprobe(path: str) -> Tuple[int, int]:
    """
    ffprobe 找视频首尾两个 I 帧的 pts（ms）。

    思路：
    - -show_entries packet=pts_time,flags（容器层扫，不解码）
    - 过滤 flags 以 K 开头的 IDR packet
    - 只记第一个和最后一个 IDR 的 pts（中间全扔）

    返回 (startFrame_ms, endFrame_ms)；失败返回 (0, 0)。
    """
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0",
        path,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GOP_FFPROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0, 0
    if r.returncode != 0:
        return 0, 0

    second_pts: Optional[float] = None  # 第二个 IDR（首段 GOP 边界）
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
    # 若全片只有 1 个 IDR（极少见），second 退化为 last
    start_ms = int((second_pts or last_pts) * 1000)
    end_ms = int(last_pts * 1000)
    return start_ms, end_ms


def _probe_video_info_ffprobe(path: str) -> Tuple[int, str]:
    """
    ffprobe 拿视频时长和帧率。

    返回 (duration_ms, fps_str)；失败返回 (0, "0.000fps")。
    """
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=avg_frame_rate",
        "-of", "csv=p=0",
        path,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GOP_FFPROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0, "0.000fps"
    if r.returncode != 0:
        return 0, "0.000fps"

    # -of csv=p=0 输出按 entry 分行（每个 entry 一行），不是 CSV 多列
    # 例如 "2997/100\n221.955292\n"
    duration_s = 0.0
    fps = 0.0
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "/" in line:
            # 帧率 "2997/100" 或 "30000/1001"
            try:
                num, den = line.split("/", 1)
                fps = float(num) / float(den) if float(den) else 0.0
            except ValueError:
                pass
        else:
            # 时长 "221.955292"
            try:
                duration_s = float(line)
            except ValueError:
                pass
    return int(duration_s * 1000), f"{fps:.3f}fps"


def _probe_keyframes_ms(path: str) -> Tuple[int, int]:
    """
    调 ffprobe 取 (second_idr_ms, last_idr_ms)：
    - second_idr_ms: 第二个 IDR 帧 pts（首段 GOP 边界；第一 IDR 必然 pts=0 无对齐价值）
    - last_idr_ms : 最后一个 IDR 帧 pts
    失败返回 (0, 0)。
    """
    return _probe_keyframes_ms_ffprobe(path)


def _ms_to_hms(ms: int) -> str:
    """毫秒 -> 'HH:MM:SS.mmm' 格式。负数视为 0。"""
    if ms < 0:
        ms = 0
    h = ms // 3_600_000
    m = (ms % 3_600_000) // 60_000
    s = (ms % 60_000) // 1_000
    milli = ms % 1_000
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"

# 业务回调上下文
# ============================================================
@dataclass(frozen=True)
class OSCContext:
    """
    一次 OSC 业务回调的"上下文"。

    由 _MulticastUDPHandler 在拆完包后构造，作为唯一参数传进业务回调，
    避免每个 on_xxx 都要重复声明 (address, source_ip, source_port) 这串形参。

    frozen=True 保证回调里不会误改它，影响后续 reply 的目标。
    """

    address: str          # 收到的 OSC 路径，如 "/play"
    source_ip: str        # 发送方 IP
    source_port: int      # 发送方端口（组播包源端口；非回复目标）
    received_at: float = field(default_factory=time.time)

    @property
    def reply_target(self) -> Tuple[str, int]:
        """单播回复目标：(source_ip, REPLY_PORT)"""
        return (self.source_ip, REPLY_PORT)


# handler 签名: (ctx: OSCContext, *osc_args) -> None
OSCHandler = Callable[..., None]


# ============================================================
# Kodi JSON-RPC over WebSocket 长连接客户端
# ============================================================
class KodiClient:
    """
    维护与 Kodi 的 WebSocket 长连接。

    - call() 是阻塞的：send 后等匹配 id 的 response 返回 result
    - 后台 reader 线程持续读 Kodi 消息：
      * 响应 → 匹配到对应 call 的 waiter 并 set event
      * notification (method + params) → 转发到 on_notification 回调
    - 连接断开时下一次 call() 会自动重连
    """

    def __init__(self, url: str, timeout: float = KODI_WS_TIMEOUT):
        self.url = url
        self.timeout = timeout
        self._ws: Optional[websocket.WebSocket] = None
        self._lock = threading.Lock()
        self._next_id = 1
        # id -> [Event, response_dict]，call 等 response 走这里
        self._responses: Dict[int, list] = {}
        # 通知回调：on_notification(method, params)，业务订阅用
        self._on_notification: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._connect()

    @property
    def on_notification(self) -> Optional[Callable[[str, Dict[str, Any]], None]]:
        return self._on_notification

    @on_notification.setter
    def on_notification(self, cb: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
        self._on_notification = cb

    # ---- 内部 ----
    def _connect(self) -> None:
        log.info("connecting to Kodi: %s", self.url)
        self._ws = websocket.create_connection(self.url, timeout=self.timeout)
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="KodiReader", daemon=True,
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        """持续读 Kodi 消息，分发到 response waiter 或 notification callback。"""
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

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if isinstance(msg.get("id"), int) and "method" not in msg:
            # response：匹配到对应 call 的 waiter
            with self._lock:
                waiter = self._responses.pop(msg["id"], None)
            if waiter is not None:
                waiter[1] = msg
                waiter[0].set()
        elif "method" in msg:
            # notification：转发到回调
            cb = self._on_notification
            if cb is not None:
                try:
                    cb(msg["method"], msg.get("params", {}))
                except Exception:
                    log.exception("kodi notification handler error")

    def _ensure_connected(self) -> None:
        try:
            # 探测 socket 是否还活着
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

    # ---- 公共 API ----
    def call(self, method: str,
             params: Optional[Dict[str, Any]] = None,
             timeout: float = 5.0) -> Dict[str, Any]:
        """
        发送 JSON-RPC 请求并返回 result 字段（已 unwrap）。

        错误时抛 RuntimeError 或 TimeoutError。
        """
        with self._lock:
            self._ensure_connected()
            msg_id = self._next_id
            self._next_id += 1
            waiter: list = [threading.Event(), None]  # [event, response]
            self._responses[msg_id] = waiter
            payload = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
            self._ws.send(json.dumps(payload))

        # 等响应（不持锁，让 reader 线程能 dispatch）
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

    def get_version(self) -> Dict[str, int]:
        """返回形如 {'major':20, 'minor':2, 'patch':0, 'tag':'stable'}"""
        return self.call("Application.GetProperties",
                         {"properties": ["version"]})["version"]

    def close(self) -> None:
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


# ============================================================
# 组播 OSC 接收器
# ============================================================
class _MulticastUDPHandler(socketserver.BaseRequestHandler):
    """socketserver 回调：每个收到的 UDP 包跑一遍。"""

    def handle(self) -> None:
        data: bytes = self.request[0]
        src_ip, src_port = self.client_address

        # 解析 OSC
        try:
            msg = OscMessage(data)
        except Exception as e:
            log.error("OSC parse error from %s:%d: %s", src_ip, src_port, e)
            return

        receiver: "MulticastOSCReceiver" = self.server.receiver  # type: ignore[attr-defined]
        handler = receiver.handlers.get(msg.address)
        if handler is None:
            log.warning("no handler for %s from %s:%d (params=%s)",
                        msg.address, src_ip, src_port, msg.params)
            return

        # 构造 ctx 后调用业务回调
        ctx = OSCContext(
            address=msg.address,
            source_ip=src_ip,
            source_port=src_port,
        )
        # 收到一条（统一格式，args 完整呈现）
        log.info("OSC <- %s from %s:%d args=%s",
                 msg.address, src_ip, src_port, tuple(msg.params))
        try:
            handler(ctx, *msg.params)
        except Exception:
            log.exception("handler error for %s from %s:%d",
                          ctx.address, ctx.source_ip, ctx.source_port)


class MulticastOSCServer(socketserver.ThreadingUDPServer):
    """加入组播组的 UDP 服务器，每个包分配一个线程处理。"""

    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self) -> None:
        mreq = struct.pack(
            "=4s4s",
            socket.inet_aton(MCAST_GROUP),
            socket.inet_aton(MCAST_IFACE),
        )
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        # 接收缓冲区 64KB
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 64)
        super().server_bind()
        log.info("joined mcast %s:%d on iface %s",
                 MCAST_GROUP, self.server_address[1], MCAST_IFACE)


class MulticastOSCReceiver:
    """对外的组播 OSC 接收 API，负责启停 server。"""

    def __init__(self, group: str = MCAST_GROUP,
                 port: int = MCAST_PORT,
                 iface: str = MCAST_IFACE):
        self.group = group
        self.port = port
        self.iface = iface
        self.handlers: Dict[str, OSCHandler] = {}
        self._server: Optional[MulticastOSCServer] = None
        self._thread: Optional[threading.Thread] = None

    def map(self, address: str, handler: OSCHandler) -> None:
        """注册某条 OSC 路径的回调。"""
        self.handlers[address] = handler
        log.info("mapped OSC %s -> %s", address, handler.__name__)

    def start(self) -> None:
        self._server = MulticastOSCServer(("0.0.0.0", self.port),
                                          _MulticastUDPHandler)
        # 把 receiver 引用挂到 server 上，handler 里要用
        self._server.receiver = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="MulticastOSCServer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ============================================================
# 单播 OSC 发送器
# ============================================================
class OSCUnicastSender:
    """
    按 (target_ip, target_port) 缓存 SimpleUDPClient，线程安全。
    """

    def __init__(self) -> None:
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

    def send(self, target_ip: str, target_port: int,
             address: str, *args: Any) -> None:
        client = self._get(target_ip, target_port)
        builder = OscMessageBuilder(address=address)
        for a in args:
            if isinstance(a, bool):
                # bool 是 int 的子类，单独判断
                builder.add_arg(1 if a else 0)
            elif isinstance(a, (int, float, str, bytes, bytearray, memoryview)):
                builder.add_arg(a)
            else:
                # 兜底：转字符串
                builder.add_arg(str(a))
        try:
            client.send(builder.build())
        except BuildError as e:
            log.error("OSC build error for %s: %s", address, e)


# ============================================================
# 守护进程主类
# ============================================================
class KodiSyncDaemon:
    """组合 Kodi 客户端 + 组播接收 + 单播发送，对外暴露 run() / stop()。"""

    def __init__(self) -> None:
        # 长连接 Kodi
        self.kodi = KodiClient(KODI_WS_URL)
        # 单播发送器
        self.sender = OSCUnicastSender()
        # 组播接收器
        self.receiver = MulticastOSCReceiver()
        # 注册 OSC 回调
        self._register_handlers()
        # 优雅退出
        self._stop = threading.Event()
        # Kodi 事件机制：notification 由 reader 线程转到这里
        self._event_lock = threading.Lock()
        self._wait_method: Optional[str] = None        # 当前在等的事件名
        self._wait_event: Optional[threading.Event] = None
        self._pending_methods: set = set()              # 已触发但未消费的事件
        self.kodi.on_notification = self._on_kodi_event

    # ---- 回复 helper ----
    def reply(self, ctx: OSCContext, address: str, *args: Any) -> None:
        """单播回复到 ctx 对应的源（固定 5006 端口）。"""
        target_ip, target_port = ctx.reply_target
        self.sender.send(target_ip, target_port, address, *args)
        log.info("OSC -> %s to %s:%d args=%s", address, target_ip, target_port, args)

    def reply_mirror(self, ctx: OSCContext, *args: Any) -> None:
        """
        自动按 ctx.address 派生 /kodi/<path> 并单播回复。

        例：ctx.address == "/play"  ->  回 "/kodi/play"
            ctx.address == "/volume" ->  回 "/kodi/volume"
        """
        suffix = ctx.address.lstrip("/")
        self.reply(ctx, f"/kodi/{suffix}", *args)

    # ---- Kodi 事件机制 ----
    def _on_kodi_event(self, method: str, params: Dict[str, Any]) -> None:
        """KodiClient reader 线程转过来的 notification 入口。
        若当前正在等该事件则 set event，否则加入 pending（防止事件先到后等）。"""
        with self._event_lock:
            if self._wait_method == method and self._wait_event is not None:
                self._wait_event.set()
            else:
                self._pending_methods.add(method)

    def _wait_for_kodi_event(self, method: str, timeout: float = 10.0) -> None:
        """等指定 Kodi 事件。命中 pending 立即返；否则 set _wait_method 后 wait event。
        超时抛 TimeoutError。"""
        with self._event_lock:
            if method in self._pending_methods:
                self._pending_methods.discard(method)
                return
            self._wait_method = method
            self._wait_event = threading.Event()
            evt = self._wait_event
        try:
            if not evt.wait(timeout):
                raise TimeoutError(f"Kodi event {method} timeout after {timeout}s")
        finally:
            with self._event_lock:
                self._wait_method = None
                self._wait_event = None

    # ---- 业务回调 ----
    def _register_handlers(self) -> None:
        self.receiver.map("/discover", self.on_discover)
        self.receiver.map("/build_playlist", self.on_build_playlist)
        self.receiver.map("/alignment/ready", self.on_alignment_ready)
        self.receiver.map("/alignment/play", self.on_alignment_play)
        # 后续在这里追加（签名统一为 (self, ctx, *osc_args)）：
        # self.receiver.map("/play",     self.on_play)
        # self.receiver.map("/pause",    self.on_pause)
        # self.receiver.map("/stop",     self.on_stop)
        # self.receiver.map("/volume",   self.on_volume)
        # self.receiver.map("/playlist", self.on_playlist)
        # ...

    # ---- 对齐 helper ----
    @staticmethod
    def _time_dict_to_ms(t: Dict[str, Any]) -> int:
        """Kodi time/totaltime dict -> 毫秒。"""
        if not t:
            return 0
        return (t.get("hours", 0) * 3_600_000
                + t.get("minutes", 0) * 60_000
                + t.get("seconds", 0) * 1_000
                + t.get("milliseconds", 0))

    def _seek_time_dict(self, pos_ms: int) -> Dict[str, int]:
        """毫秒 -> Kodi time dict。"""
        return {
            "hours": pos_ms // 3_600_000,
            "minutes": (pos_ms % 3_600_000) // 60_000,
            "seconds": (pos_ms % 60_000) // 1_000,
            "milliseconds": pos_ms % 1_000,
        }

    def _do_open_and_verify(self, ctx: OSCContext, address: str,
                             idx: int) -> bool:
        """Player.Open + 验证 result==OK，失败自动 reply。返回 True=成功。"""
        try:
            result = self.kodi.call("Player.Open", {
                "item": {"playlistid": PLAYLIST_ID, "position": idx},
            }, timeout=10.0)
        except Exception as e:
            self.reply(ctx, address, idx, "", 0, f"open_exc: {e}")
            return False
        if result != "OK":
            self.reply(ctx, address, idx, "", 0, f"open_fail: {result}")
            return False
        return True

    def _do_get_position(self, idx: int) -> tuple:
        """GetProperties(time) + GetItem(file)。Kodi seek 后 time 可能短暂负值，最多重试 5 次。"""
        actual_ms = 0
        for _ in range(5):
            try:
                props = self.kodi.call("Player.GetProperties", {
                    "playerid": 1, "properties": ["time"],
                })
                actual_ms = self._time_dict_to_ms(props.get("time"))
                if actual_ms > 0:
                    break
                # time 未更新，等 50ms 重试
                time.sleep(0.05)
            except Exception:
                # 偶发异常也重试
                time.sleep(0.05)
                continue
        try:
            item = self.kodi.call("Player.GetItem",
                                   {"playerid": 1, "properties": ["file"]})
            file_path = (item.get("item") or {}).get("file", "")
        except Exception as e:
            log.error("get_position GetItem failed idx=%d: %s", idx, e)
            file_path = ""
        return (file_path, actual_ms)

    def _clear_pending(self) -> None:
        with self._event_lock:
            self._pending_methods.clear()

    # ---- /alignment/ready ----
    def on_alignment_ready(self, ctx: OSCContext, *osc_args: Any) -> None:
        """
        处理 /alignment/ready <idx> <pos_ms>：

        1) Player.Open → 等 "OK"；
        2) 等 Player.OnAVStart（第一帧渲染完成）；
        3) Player.Seek(time=pos_ms) 播放中寻址（time 正常更新，不会负值）；
        4) 等 Player.OnSeek；
        5) Player.PlayPause(play=false) 暂停；
        6) 等 Player.OnPause；
        7) GetProperties(time) + GetItem(file)；
        8) 上报 /kodi/alignment/ready (idx, file, actual_ms, "ready")。
        """
        if len(osc_args) < 2:
            self.reply(ctx, "/kodi/alignment/ready", -1, "", 0, "bad_args")
            return
        try:
            idx = int(osc_args[0])
            pos_ms = int(osc_args[1])
        except (TypeError, ValueError):
            self.reply(ctx, "/kodi/alignment/ready", -1, "", 0, "bad_int")
            return
        if pos_ms <= 0:
            self.reply(ctx, "/kodi/alignment/ready", idx, "", 0, "zero_pos")
            return

        self._clear_pending()
        log.info("/alignment/ready idx=%d pos=%dms", idx, pos_ms)

        # 1) Open
        if not self._do_open_and_verify(ctx, "/kodi/alignment/ready", idx):
            return

        # 2) 等 OnAVStart
        try:
            self._wait_for_kodi_event("Player.OnAVStart", timeout=15.0)
        except TimeoutError:
            self.reply(ctx, "/kodi/alignment/ready", idx, "", 0, "avstart_timeout")
            return

        # 3) Seek（播放中寻址，time 会正常更新）
        seek_time = self._seek_time_dict(pos_ms)
        try:
            self.kodi.call("Player.Seek",
                            {"playerid": 1, "value": {"time": seek_time}},
                            timeout=5.0)
        except Exception as e:
            self.reply(ctx, "/kodi/alignment/ready", idx, "", 0, f"seek: {e}")
            return

        # 4) 等 OnSeek
        try:
            self._wait_for_kodi_event("Player.OnSeek", timeout=10.0)
        except TimeoutError:
            self.reply(ctx, "/kodi/alignment/ready", idx, "", 0, "seek_timeout")
            return

        # 5) PlayPause(play=false) 暂停
        try:
            self.kodi.call("Player.PlayPause",
                            {"playerid": 1, "play": False}, timeout=5.0)
        except Exception as e:
            self.reply(ctx, "/kodi/alignment/ready", idx, "", 0, f"pause: {e}")
            return

        # 6) 等 OnPause
        try:
            self._wait_for_kodi_event("Player.OnPause", timeout=10.0)
        except TimeoutError:
            self.reply(ctx, "/kodi/alignment/ready", idx, "", 0, "pause_timeout")
            return

        # 7) 取位置
        file_path, actual_ms = self._do_get_position(idx)

        # 8) 上报
        log.info("/alignment/ready idx=%d pos=%dms -> paused at %dms",
                 idx, pos_ms, actual_ms)
        self.reply(ctx, "/kodi/alignment/ready",
                   idx, file_path, actual_ms, "ready")

    # ---- /alignment/play ----
    def on_alignment_play(self, ctx: OSCContext, *osc_args: Any) -> None:
        """
        处理 /alignment/play <idx> <pos_ms> <delay_ms>：

        1) Player.Open → 等 "OK"；
        2) 等 Player.OnAVStart；
        3) Player.Seek(time=pos_ms)；
        4) 等 Player.OnSeek；
        5) Player.PlayPause(play=false) 暂停；
        6) 等 Player.OnPause；
        7) GetProperties(time) + GetItem(file)；
        8) 延迟 delay_ms 毫秒；
        9) Player.PlayPause(play=true) 恢复；
       10) 等 Player.OnResume；
       11) 上报 /kodi/alignment/play (idx, file, "isPlaying")。
        """
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
        if pos_ms <= 0:
            self.reply(ctx, "/kodi/alignment/play", idx, "", 0, "zero_pos")
            return

        self._clear_pending()
        log.info("/alignment/play idx=%d pos=%dms delay=%dms", idx, pos_ms, delay_ms)

        # 1) Open
        if not self._do_open_and_verify(ctx, "/kodi/alignment/play", idx):
            return

        # 2) 等 OnAVStart
        try:
            self._wait_for_kodi_event("Player.OnAVStart", timeout=15.0)
        except TimeoutError:
            self.reply(ctx, "/kodi/alignment/play", idx, "", 0, "avstart_timeout")
            return

        # 3) Seek
        seek_time = self._seek_time_dict(pos_ms)
        try:
            self.kodi.call("Player.Seek",
                            {"playerid": 1, "value": {"time": seek_time}},
                            timeout=5.0)
        except Exception as e:
            self.reply(ctx, "/kodi/alignment/play", idx, "", 0, f"seek: {e}")
            return

        # 4) 等 OnSeek
        try:
            self._wait_for_kodi_event("Player.OnSeek", timeout=10.0)
        except TimeoutError:
            self.reply(ctx, "/kodi/alignment/play", idx, "", 0, "seek_timeout")
            return

        # 5) PlayPause(play=false) 暂停
        try:
            self.kodi.call("Player.PlayPause",
                            {"playerid": 1, "play": False}, timeout=5.0)
        except Exception as e:
            self.reply(ctx, "/kodi/alignment/play", idx, "", 0, f"pause: {e}")
            return

        # 6) 等 OnPause
        try:
            self._wait_for_kodi_event("Player.OnPause", timeout=10.0)
        except TimeoutError:
            self.reply(ctx, "/kodi/alignment/play", idx, "", 0, "pause_timeout")
            return

        # 7) 取位置
        file_path, actual_ms = self._do_get_position(idx)
        log.info("/alignment/play idx=%d pos=%dms -> paused at %dms",
                 idx, pos_ms, actual_ms)

        # 8) 延迟后恢复播放
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        # 9) PlayPause(play=true) 恢复
        try:
            self.kodi.call("Player.PlayPause",
                            {"playerid": 1, "play": True}, timeout=5.0)
        except Exception as e:
            self.reply(ctx, "/kodi/alignment/play", idx, file_path, actual_ms,
                       f"resume: {e}")
            return

        # 10) 等 OnResume
        try:
            self._wait_for_kodi_event("Player.OnResume", timeout=10.0)
        except TimeoutError:
            self.reply(ctx, "/kodi/alignment/play", idx, file_path, actual_ms,
                       "resume_timeout")
            return

        # 11) 上报 isPlaying
        log.info("/alignment/play idx=%d isPlaying", idx)
        self.reply(ctx, "/kodi/alignment/play", idx, file_path, "isPlaying")
        """处理 /discover：查 Kodi 版本、拿本机信息、单播回传 /kodi/discover。"""


        # 1) Kodi 版本
        try:
            ver = self.kodi.get_version()
        except Exception as e:
            log.error("kodi get_version failed: %s", e)
            return
        version_str = f"{ver.get('major', 0)}." \
                      f"{ver.get('minor', 0)}." \
                      f"{ver.get('patch', 0)}"

        # 2) 本机 IP + MAC（出错不 log.info，只在异常时打）
        try:
            local_ip = get_local_ip()
            mac = get_mac(ETH_IFACE)
        except Exception as e:
            log.error("get local info failed: %s", e)
            return

        # 3) 单播回传到组播源（固定 5006 端口）
        try:
            self.reply(ctx, "/kodi/discover", local_ip, mac, version_str)
        except Exception:
            log.exception("send /kodi/discover failed")

    def on_build_playlist(self, ctx: OSCContext, *osc_args: Any) -> None:
        """
        处理 /build_playlist：

        1. Files.GetDirectory 列目录（带 sort，Kodi 排好序，路径不带尾斜杠）；
        2. Playlist.Clear 清空旧 playlist；
        3. 逐个 Playlist.Insert(position=N)，每 Insert 完立即 ffprobe 拿该文件元数据；
        4. 单条 OSC 消息上报完整列表，不分行、不报 ok：
           /kodi/playlist <count>
               <idx0> <name0> <dur0_hms> <fps0> <second_idr0_ms> <last_idr0_ms>
               <idx1> <name1> <dur1_hms> <fps1> <second_idr1_ms> <last_idr1_ms>
               ...

        注：startFrame 用"第二个 I 帧"（首段 GOP 边界），不用第一个 I 帧
        （第一 I 帧 pts=0 视频开头，无对齐参考价值）。
        """
        # 0) 立即发"请稍候"提示（args[0] 是 string，Chataigne 据此区分）
        self.reply(ctx, "/kodi/playlist",
                   "Please wait, ffprobe is processing...")

        # 1) 列目录
        try:
            dir_resp = self.kodi.call("Files.GetDirectory", {
                "directory": VIDEOS_DIR,
                "media": "video",
                "properties": ["file"],
                "sort": {"method": "file", "order": "ascending", "ignorearticle": True},
            })
        except Exception as e:
            log.error("get_directory failed: %s", e)
            self.reply(ctx, "/kodi/playlist", 0, "error", f"get_directory: {e}")
            return

        files = [
            it["file"] for it in (dir_resp or {}).get("files", [])
            if it.get("file")
        ]

        # 2) Playlist.Clear
        try:
            self.kodi.call("Playlist.Clear", {"playlistid": PLAYLIST_ID})
        except Exception as e:
            log.error("playlist clear failed: %s", e)
            self.reply(ctx, "/kodi/playlist", 0, "error", f"clear: {e}")
            return

        # 3) 边 Insert 边 ffprobe，攒到 item_args
        item_args: list = []
        for idx, fp in enumerate(files):
            try:
                self.kodi.call("Playlist.Insert", {
                    "playlistid": PLAYLIST_ID,
                    "position": idx,
                    "item": {"file": fp},
                })
            except Exception as e:
                log.error("Insert %s failed: %s", fp, e)
                continue

            local_path = fp[len("file://"):] if fp.startswith("file://") else fp
            basename = os.path.basename(fp)
            duration_ms, fps_str = _probe_video_info_ffprobe(local_path)
            second_idr_ms, last_idr_ms = _probe_keyframes_ms(local_path)

            item_args.extend([
                idx,
                basename,
                _ms_to_hms(duration_ms),
                fps_str,
                second_idr_ms,
                last_idr_ms,
            ])

        count = len(item_args) // 6
        log.info("playlist built: %d items", count)

        # 4) 单条消息上报完整列表（不分行、不报 ok）
        self.reply(ctx, "/kodi/playlist", count, *item_args)
    def run(self) -> None:
        # 信号
        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGTERM, self._signal)

        log.info("starting receiver...")
        self.receiver.start()
        log.info("daemon running. Ctrl-C to stop.")

        # 主线程空闲等信号
        while not self._stop.is_set():
            time.sleep(0.5)

        self.stop()

    def _signal(self, signum, frame) -> None:
        log.info("got signal %d, stopping...", signum)
        self._stop.set()

    def stop(self) -> None:
        log.info("shutting down...")
        try:
            self.receiver.stop()
        except Exception:
            pass
        try:
            self.kodi.close()
        except Exception:
            pass
        log.info("bye.")


# ============================================================
# 入口
# ============================================================
def main() -> int:
    daemon = KodiSyncDaemon()
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
