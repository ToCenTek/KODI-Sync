import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
import socket
import struct
from types import SimpleNamespace
import websockets
from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder
CFG = SimpleNamespace(
    osc_group=os.environ.get("OSC_GROUP", "239.0.0.69"),
    osc_listen_port=int(os.environ.get("OSC_PORT", "9000")),
    osc_report_port=int(os.environ.get("OSC_REPORT_PORT", "5006")),
    kodi_uri=os.environ.get("KODI_URI", "ws://localhost:9090/jsonrpc"),
    playlist_dir=os.environ.get("PLAYLIST_DIR", "/storage/videos"),
    heartbeat_hz=float(os.environ.get("HEARTBEAT_HZ", "2.5")),
    iface=os.environ.get("NETWORK_INTERFACE", "eth0"),
    cpu_mask=(False, True, True, False),  # Kodi decoder on CPU 0/3
)
SEEK_SETTLE = 0.3  # PlayPause 后等 Kodi 进入暂停态再 Seek
MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}
KODI_REPEAT_VALID = ("off", "one", "all")
RECONNECT_BACKOFF_MAX = 10.0
OSC_MAX_DATAGRAM = 65535
OSC_REQUEST_TIMEOUT = 10.0
PLAYER_WAIT_TICKS = 250
PLAYER_WAIT_INTERVAL = 0.02

DOWN = SimpleNamespace(
    discover="/discover", build_playlist="/build_playlist",
    alignment_ready="/alignment_ready", alignment_play="/alignment_play",
    play="/play", play_pause="/play_pause",
    pause="/pause", stop="/stop", query_current_time="/query_current_time",
    set_loop="/set_loop", cpu_affinity="/cpu_affinity", member="/member", volume="/volume",
)
UPK = SimpleNamespace(
    discover="/kodi/discover", playlist="/kodi/playlist",
    # 事件通知 (OnPlay / OnPause 等)
    report_play="/kodi/report/play", report_current_time="/kodi/report/current_time",
    report_volume="/kodi/report/volume", report_loop_mode="/kodi/report/loop_mode",
    # alignment 响应
    alignment_ready="/kodi/alignment/ready",
    alignment_play="/kodi/alignment/play",
)
UPA = SimpleNamespace(
    heartbeat="/agent/heartbeat", member="/agent/member", error="/agent/error",
)

class OscIO:
    def __init__(self, group, listen_port, report_port):
        self._group = group
        self._report_port = report_port
        self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_socket.bind(("0.0.0.0", listen_port))
        self._is_joined = True
        self._apply_membership(True)
        self._listen_socket.setblocking(False)
        self._transmit_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._target_address = ("127.0.0.1", report_port)  # 由首条 OSC origin IP 锁定
        self._on_message = None

    def _apply_membership(self, join_group):
        option = socket.IP_ADD_MEMBERSHIP if join_group else socket.IP_DROP_MEMBERSHIP
        request = struct.pack("4s4s", socket.inet_aton(self._group), socket.inet_aton("0.0.0.0"))
        with contextlib.suppress(OSError):
            self._listen_socket.setsockopt(socket.IPPROTO_IP, option, request)
            self._is_joined = join_group

    def set_report_target(self, controller_ip):
        if self._target_address[0] != controller_ip:
            self._target_address = (controller_ip, self._report_port)
            logging.info(f"OSC report -> {controller_ip}:{self._report_port}")

    def send(self, address, *arguments):
        try:
            builder = OscMessageBuilder(address=address)
            for argument in arguments:
                builder.add_arg(argument)
            self._transmit_socket.sendto(builder.build().dgram, self._target_address)
        except Exception as exc:
            logging.warning(f"OSC send fail [{address}]: {exc}")

    def set_joined(self, want_join):
        if want_join != self._is_joined:
            self._apply_membership(want_join)
            logging.info(f"multicast {'joined' if want_join else 'left'} {self._group}")

    async def receive_loop(self):
        event_loop = asyncio.get_event_loop()
        while True:
            try:
                datagram, origin = await event_loop.sock_recvfrom(self._listen_socket, OSC_MAX_DATAGRAM)
            except asyncio.CancelledError:
                return
            except OSError as exc:
                logging.warning(f"OSC recv fail: {exc}")
                await asyncio.sleep(0.1)
                continue
            self._dispatch(datagram, origin[0])

    def _dispatch(self, datagram, origin_ip):
        try:
            message = OscMessage(datagram)
        except Exception as exc:
            logging.warning(f"OSC parse fail: {exc}")
            return
        if self._on_message:
            with contextlib.suppress(Exception):
                self._on_message(message.address, message.params, origin_ip)

    def close(self):
        for sock in (self._listen_socket, self._transmit_socket):
            with contextlib.suppress(OSError):
                sock.close()

class KodiClient:
    def __init__(self, uri, on_notification):
        self._uri = uri
        self._on_notification = on_notification
        self._websocket = None
        self._next_id = 1
        self._pending = {}
        self._connected = False
        self._should_run = True
        self._backoff = 1.0

    async def run_forever(self):
        while self._should_run:
            try:
                await self._connect()
                self._backoff = 1.0
                async for message in self._websocket:
                    if isinstance(message, bytes): continue  # 忽略 binary frame (Kodi 缩略图)
                    self._handle(message)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logging.warning(f"Kodi lost: {exc}")
            self._teardown()
            if not self._should_run:
                return
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, RECONNECT_BACKOFF_MAX)

    async def _connect(self):
        self._websocket = await websockets.connect(self._uri, ping_interval=None, ping_timeout=None, max_size=2**24)

        self._connected = True
        logging.info(f"Kodi connected: {self._uri}")

    def _teardown(self):
        self._connected = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Kodi disconnected"))
        self._pending.clear()
        if self._websocket:
            with contextlib.suppress(Exception):
                asyncio.get_event_loop().create_task(self._websocket.close())
        self._websocket = None

    def _handle(self, raw):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "id" in payload and "method" not in payload:
            request_id = payload.get("id")
            if not isinstance(request_id, int):
                return
            future = self._pending.pop(request_id, None)
            if future and not future.done():
                if "error" in payload:
                    future.set_exception(RuntimeError(payload["error"]))
                else:
                    future.set_result(payload.get("result", {}))
        elif "method" in payload:
            with contextlib.suppress(Exception):
                asyncio.create_task(self._on_notification(payload["method"], payload.get("params", {})))

    async def call(self, method, parameters=None):
        if not self._connected or not self._websocket:
            raise ConnectionError("Kodi not connected")
        request_id = self._next_id
        self._next_id = (self._next_id + 1) % 1_000_000_000
        future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        await self._websocket.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": parameters or {}}))
        return await asyncio.wait_for(future, OSC_REQUEST_TIMEOUT)

    def stop(self):
        self._should_run = False

def read_local_ip(interface_name):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", interface_name.encode()[:15])
        return socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, request)[20:24])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()

def read_mac(interface_name):
    try:
        with open(f"/sys/class/net/{interface_name}/address", encoding="ascii") as mac_file:
            return mac_file.read().strip()
    except OSError:
        return "00:00:00:00:00:00"

def apply_cpu_affinity(cpu_mask):
    if not hasattr(os, "sched_setaffinity"):
        return False
    cores = [i for i, enabled in enumerate(cpu_mask) if enabled]
    if not cores:
        return False
    try:
        os.sched_setaffinity(0, set(cores))
        return True
    except OSError:
        return False

def ms_to_time_dict(milliseconds_total):  # Kodi Player.Seek 需要 {time: {hours, minutes, seconds, milliseconds}}
    return {"hours": milliseconds_total // 3_600_000, "minutes": (milliseconds_total % 3_600_000) // 60_000,
            "seconds": (milliseconds_total % 60_000) // 1_000, "milliseconds": milliseconds_total % 1_000}

def time_to_ms(time_payload):
    if not isinstance(time_payload, dict):
        return 0
    return max(0, time_payload.get("hours", 0) * 3_600_000 + time_payload.get("minutes", 0) * 60_000
            + time_payload.get("milliseconds", 0))

class AgentState:
    def __init__(self):
        self.player_id = None
        self.playlist = []  # (clip_index, file_path, duration_ms)
        self.media_path = ""
        self.position_ms = 0
        self.seek_complete = False  # Player.OnSeek 触发后置 True, 表示 seek 真正完成
        self.loop_mode = "off"

class KodiEventBridge:
    def __init__(self, kodi, osc, state, sync=None):
        self._kodi, self._osc, self._state = kodi, osc, state
        self._sync = sync
        self._handlers = {
            "Player.OnPlay": self._on_play,
            "Player.OnPause": self._on_pause,
            "Player.OnStop": self._on_stop,
            "Player.OnAVStart": self._on_avstart,
            "Player.OnSeek": self._on_seek,
        }

    async def handle(self, method, params):
        data = params.get("data", {}) if isinstance(params, dict) else {}
        handler = self._handlers.get(method)
        if handler:
            await handler(data)

    async def _on_play(self, data):
        player = data.get("player", {})
        if isinstance(player.get("playerid"), int):
            self._state.player_id = player["playerid"]
        self._state.is_playing = True
        self._state.media_path = data.get("item", {}).get("file", self._state.media_path)

        self._osc.send(UPK.report_play, self._state.media_path, "isPlaying", 0)
        self._state.video_ready = False  # 重置, 等待新的 AVStart

    async def _on_avstart(self, data): self._state.video_ready = True
    async def _on_seek(self, data): self._state.seek_complete = True
    async def _on_pause(self, data):
        self._state.is_playing = False
        if self._sync and hasattr(self._sync, '_on_pause_event'):
            self._sync._on_pause_event.set()
        self._osc.send(UPK.report_play, self._state.media_path, "isPaused", 0)

    async def _on_stop(self, data):
        self._state.is_playing = False
        self._state.player_id = None
        self._osc.send(UPK.report_play, self._state.media_path, "isStopped", 0)
        if self._state.loop_mode == "startFrame" and self._state.media_path:
            await self._reopen_current()

    async def _on_volume(self, data):
        self._osc.send(UPK.report_volume, int(data.get("volume", 0)))

    async def _read_position(self):
        if self._state.player_id is None:
            return 0
        try:
            response = await self._kodi.call("Player.GetProperties", {"playerid": self._state.player_id, "properties": ["time"]})
            return time_to_ms(response.get("time", {}))
        except Exception:
            return 0

    async def _reopen_current(self):
        try:
            await self._kodi.call("Playlist.Clear", {"playlistid": 1})
            await self._kodi.call("Playlist.Add", {"playlistid": 1, "item": {"file": self._state.media_path}})
            await self._kodi.call("Player.Open", {"item": {"playlistid": 1, "position": 0}})
        except Exception as exc:
            logging.warning(f"reopen for startFrame failed: {exc}")

class CommandDispatcher:
    def __init__(self, kodi, osc, state, agent):
        self._kodi, self._osc, self._state, self._agent = kodi, osc, state, agent

    def route(self, address, arguments, origin_ip):
        # /alignment/ready → _on_alignment_ready; /alignment_ready 也兼容
        sanitized = address.replace('/', '_').lstrip('_')
        handler = getattr(self, f"_on_{sanitized}", None)
        if handler:
            asyncio.create_task(handler(arguments, origin_ip))

    async def _on_discover(self, args, origin):
        self._osc.send(UPK.discover, read_local_ip(CFG.iface), read_mac(CFG.iface))

    async def _on_build_playlist(self, args, origin):
        await self._agent.build_playlist_from_directory()

    async def _on_alignment_ready(self, args, origin):
        """
        /alignment_ready <idx> <pos_ms>

        1) Open + OnAVStart (playing, 第一帧渲染完)
        2) Seek(time=pos_ms) 播放中寻址 (time 正常更新)
        3) OnSeek
        4) PlayPause(play=false) 暂停
        5) OnPause
        6) GetProperties(time) 取实际位置
        7) report (idx, file, actual_ms, total_ms)
        """
        try:
            clip_index, target_seek_ms = int(args[0]), int(args[1])
            media_item = self._state.playlist[clip_index]
        except (TypeError, ValueError):
            self._osc.send(UPA.error, "ready", "bad_arg")
            return
        except (IndexError, KeyError):
            self._osc.send(UPA.error, "ready", f"no_clip:{args[0]}")
            return
        file_path = media_item[1]
        if target_seek_ms <= 0:
            self._osc.send(UPA.error, "ready", "zero_pos")
            return

        self._state.video_ready = False
        self._state.seek_complete = False
        try:
            k = self._kodi
            # 1) Open + 等 OnPlay + OnAVStart
            await k.call("Playlist.Clear", {"playlistid": 1})
            await k.call("Playlist.Add", {"playlistid": 1, "item": {"file": file_path}})
            await k.call("Player.Open", {"item": {"playlistid": 1, "position": 0}})
            for i in range(30):
                if self._state.player_id is not None and self._state.is_playing and self._state.video_ready:
                    break
                await asyncio.sleep(0.5)

            # 2) Seek(time=pos_ms) — 播放中寻址, time 不会归零
            self._state.seek_complete = False
            seek_time = ms_to_time_dict(target_seek_ms)
            await k.call("Player.Seek",
                        {"playerid": self._state.player_id, "value": {"time": seek_time}})
            for i in range(20):
                if self._state.seek_complete:
                    break
                await asyncio.sleep(0.5)

            self._agent._on_pause_event.clear()
            await k.call("Player.PlayPause",
                        {"playerid": self._state.player_id, "play": False})
            try:
                await asyncio.wait_for(self._agent._on_pause_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            # 4) GetProperties(time) + GetItem 取实际位置
            actual_ms = 0
            total_ms = 0
            with contextlib.suppress(Exception):
                resp = await asyncio.wait_for(
                    k.call("Player.GetProperties", {
                        "playerid": self._state.player_id,
                        "properties": ["time", "totaltime"]
                    }), timeout=3.0)
                # resp 已是 result 本体 (KodiClient 已解包)
                actual_ms = time_to_ms(resp.get("time", {}))
                tot = resp.get("totaltime", {})
                if tot:
                    total_ms = tot.get("hours", 0) * 3600000 \
                             + tot.get("minutes", 0) * 60000 \
                             + tot.get("seconds", 0) * 1000 \
                             + tot.get("milliseconds", 0)
            with contextlib.suppress(Exception):
                item_resp = await asyncio.wait_for(
                    k.call("Player.GetItem", {"playerid": self._state.player_id}), timeout=3.0)
                file_path = item_resp.get("item", {}).get("file", file_path)

            self._state.media_path = file_path
            self._state.position_ms = actual_ms
            self._state.is_playing = False
            log_msg = f"ready idx={clip_index} pos={target_seek_ms}ms -> paused at {actual_ms}ms"
            logging.info(log_msg)
            self._osc.send(UPK.alignment_ready, clip_index, file_path, actual_ms, total_ms)
        except Exception as exc:
            self._osc.send(UPA.error, "ready", str(exc))

    async def _on_alignment_play(self, args, origin):
        """
        /alignment_play <idx> <pos_ms> <delay_ms>

        1-6 同 ready: Open → OnAVStart → Seek → OnSeek → PlayPause(false) → OnPause
        7) GetPosition + GetItem
        8) 延迟 delay_ms
        9) PlayPause(play=true) 恢复
       10) OnResume
       11) report (idx, file, "isPlaying")
        """
        try:
            clip_index, target_seek_ms, delay_ms = int(args[0]), int(args[1]), int(args[2])
            media_item = self._state.playlist[clip_index]
        except (TypeError, ValueError):
            self._osc.send(UPA.error, "play", "bad_arg")
            return
        except (IndexError, KeyError):
            self._osc.send(UPA.error, "play", f"no_clip:{args[0]}")
            return
        file_path = media_item[1]
        if target_seek_ms <= 0:
            self._osc.send(UPA.error, "play", "zero_pos")
            return

        self._state.video_ready = False
        self._state.seek_complete = False
        try:
            k = self._kodi
            # 1) Open + 等 OnAVStart
            await k.call("Playlist.Clear", {"playlistid": 1})
            await k.call("Playlist.Add", {"playlistid": 1, "item": {"file": file_path}})
            await k.call("Player.Open", {"item": {"playlistid": 1, "position": 0}})
            for i in range(30):
                if self._state.player_id is not None and self._state.is_playing and self._state.video_ready:
                    break
                await asyncio.sleep(0.5)

            # 2) Seek(time=pos_ms)
            self._state.seek_complete = False
            seek_time = ms_to_time_dict(target_seek_ms)
            await k.call("Player.Seek",
                        {"playerid": self._state.player_id, "value": {"time": seek_time}})
            for i in range(20):
                if self._state.seek_complete:
                    break
                await asyncio.sleep(0.5)

            self._agent._on_pause_event.clear()
            await k.call("Player.PlayPause",
                        {"playerid": self._state.player_id, "play": False})
            try:
                await asyncio.wait_for(self._agent._on_pause_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            # 4) 取位置
            actual_ms = 0
            with contextlib.suppress(Exception):
                resp = await asyncio.wait_for(
                    k.call("Player.GetProperties", {
                        "playerid": self._state.player_id,
                        "properties": ["time"]
                    }), timeout=3.0)
                actual_ms = time_to_ms(resp.get("time", {}))
            with contextlib.suppress(Exception):
                item_resp = await asyncio.wait_for(
                    k.call("Player.GetItem", {"playerid": self._state.player_id}), timeout=3.0)
                file_path = item_resp.get("item", {}).get("file", file_path)

            self._state.media_path = file_path
            self._state.position_ms = actual_ms
            self._state.is_playing = False
            logging.info(f"play idx={clip_index} paused at {actual_ms}ms")

            # 5) 延迟
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)

            # 6) PlayPause(play=true) 恢复
            await k.call("Player.PlayPause",
                        {"playerid": self._state.player_id, "play": True})
            # 稍等 OnResume 触发
            await asyncio.sleep(0.5)

            self._state.is_playing = True
            logging.info(f"play idx={clip_index} isPlaying")
            self._osc.send(UPK.alignment_play, file_path, "isPlaying", actual_ms)
        except Exception as exc:
            self._osc.send(UPA.error, "play", str(exc))

    async def _on_play(self, args, origin):
        if not args:
            if self._state.player_id is not None:
                with contextlib.suppress(Exception): await self._kodi.call("Player.PlayPause", {"playerid": self._state.player_id, "play": True})
            return
        try:
            media_item = self._state.playlist[int(args[0])]
            file_path = media_item[1]
        except (TypeError, ValueError, IndexError, KeyError):
            return
        try:
            k = self._kodi
            await k.call("Playlist.Clear", {"playlistid": 1})
            await k.call("Playlist.Add", {"playlistid": 1, "item": {"file": file_path}})
            await k.call("Player.Open", {"item": {"playlistid": 1, "position": 0}})
            self._state.media_path = file_path
        except Exception as exc:
            logging.warning(f"play failed: {exc}")

    async def _on_play_pause(self, args, origin):
        if self._state.player_id is not None:
            with contextlib.suppress(Exception): await self._kodi.call("Player.PlayPause", {"playerid": self._state.player_id})

    async def _on_pause(self, args, origin):
        if self._state.player_id is not None:
            with contextlib.suppress(Exception): await self._kodi.call("Player.PlayPause", {"playerid": self._state.player_id, "play": False})

    async def _on_stop(self, args, origin):
        if self._state.player_id is not None:
            with contextlib.suppress(Exception): await self._kodi.call("Player.Stop", {"playerid": self._state.player_id})

    async def _on_query_current_time(self, args, origin):
        if self._state.player_id is None: self._osc.send(UPK.report_current_time, 0); return
        try:
            response = await self._kodi.call("Player.GetProperties", {"playerid": self._state.player_id, "properties": ["time"]})
            self._osc.send(UPK.report_current_time, time_to_ms(response.get("time", {})))
        except Exception:
            self._osc.send(UPK.report_current_time, 0)

    async def _on_set_loop(self, args, origin):
        if not args: return
        loop_mode = str(args[0])
        self._state.loop_mode = loop_mode
        kodi_repeat = loop_mode if loop_mode in KODI_REPEAT_VALID else "off"
        with contextlib.suppress(Exception):
            await self._kodi.call("Playlist.SetRepeat", {"playlistid": 1, "repeat": kodi_repeat})
        self._osc.send(UPK.report_loop_mode, loop_mode)

    async def _on_cpu_affinity(self, args, origin):
        if len(args) >= 4:
            with contextlib.suppress(TypeError, ValueError): apply_cpu_affinity(tuple(bool(int(raw)) for raw in args[:4]))

    async def _on_member(self, args, origin):
        if not args: return
        action = str(args[0]).lower()
        if action in ("join", "add"): self._osc.set_joined(True)
        elif action == "leave": self._osc.set_joined(False)
        self._osc.send(UPA.member, "join" if self._osc._is_joined else "leave")

    async def _on_volume(self, args, origin):
        if not args: return
        with contextlib.suppress(Exception, TypeError, ValueError):
            await self._kodi.call("Application.SetVolume", {"volume": max(0, min(100, int(float(args[0]) * 100)))})

async def heartbeat_loop(kodi, osc, state, frequency_hz):
    if frequency_hz <= 0: return
    while True:
        await asyncio.sleep(1.0 / frequency_hz)

class KodiSyncAgent:
    def __init__(self):
        self._state = AgentState()
        self._osc = OscIO(CFG.osc_group, CFG.osc_listen_port, CFG.osc_report_port)
        self._kodi = KodiClient(CFG.kodi_uri, on_notification=None)
        self._event_bridge = KodiEventBridge(self._kodi, self._osc, self._state)
        self._dispatcher = CommandDispatcher(self._kodi, self._osc, self._state, self)
        self._kodi._on_notification = self._event_bridge.handle
        self._osc._on_message = self._on_osc_message
        self._on_pause_event = asyncio.Event()
        apply_cpu_affinity(CFG.cpu_mask)

    def _on_osc_message(self, address, arguments, origin_ip):
        self._osc.set_report_target(origin_ip)
        self._dispatcher.route(address, arguments, origin_ip)

    async def build_playlist_from_directory(self):
        k = self._kodi
        try:
            await k.call("Playlist.Clear", {"playlistid": 1})
            dir_response = await k.call("Files.GetDirectory", {"directory": CFG.playlist_dir, "media": "video"})
            file_paths = sorted([item["file"] for item in (dir_response or {}).get("files", []) if "file" in item])
            for file_path in file_paths:
                await k.call("Playlist.Add", {"playlistid": 1, "item": {"file": file_path}})
            items_response = await k.call("Playlist.GetItems", {"playlistid": 1})
        except Exception as exc:
            self._osc.send(UPA.error, "playlist", str(exc))
            return
        self._state.playlist = [(idx, file_path, 0) for idx, file_path in enumerate(file_paths)]
        arguments = [len(file_paths)] + [v for idx, file_path in enumerate(file_paths) for v in (idx, os.path.basename(file_path))]
        self._osc.send(UPK.playlist, *arguments)
        logging.info(f"playlist built: {len(file_paths)} clips")

    def request_shutdown(self):
        self._kodi.stop()
        for task in asyncio.all_tasks(): task.cancel()

    async def run_forever(self):
        event_loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError): event_loop.add_signal_handler(sig, self.request_shutdown)
        tasks = [
            asyncio.create_task(self._osc.receive_loop(), name="osc"),
            asyncio.create_task(self._kodi.run_forever(), name="kodi"),
            asyncio.create_task(heartbeat_loop(self._kodi, self._osc, self._state, CFG.heartbeat_hz), name="heartbeat"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._osc.close()

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname).1s %(name)s | %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(KodiSyncAgent().run_forever())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
