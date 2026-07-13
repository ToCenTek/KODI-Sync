"""多设备 alignment 精度测试.

在三台机器上同时触发 /alignment_ready 同一 seek 位置, 收集每台机器
上报的 /kodi/report/ready 实际位置, 验证位置偏差 ≤ 15ms (README 指标).

在 10.0.0.29 上运行: 发组播命令 + 收所有 agent 的单播响应.

用法: python3 multi_align_test.py
"""
import socket
import sys
import threading
import time
from collections import defaultdict

from pythonosc import osc_message_builder
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer


LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5006
SEND_GROUP = "239.0.0.69"
SEND_PORT = 9000
EXPECTED_MACHINES = ["10.0.0.29", "10.0.0.69", "10.0.0.89"]

# 收集每台机器的 /kodi/report/ready 上报
position_by_machine: dict[str, int] = {}
file_by_machine: dict[str, str] = {}
lock = threading.Lock()


def _handle_message(address, *args):
    """记录所有收到的消息, 重点关注 /kodi/report/ready."""
    origin_ip = "?"
    # python-osc 不传 origin, 但我们 listen 0.0.0.0 收所有, 用一个 timestamp + source
    print(f"[recv {time.strftime('%H:%M:%S')}] {address} {args}")
    if address == "/kodi/report/ready" and len(args) >= 3:
        # 我们无法从 args 拿 origin_ip, 因为 thread 是同一个 listener
        # 用 thread name 推断? 不行. 改用 thread-local + origin_ip
        pass


def _start_listener() -> ThreadingOSCUDPServer:
    """listener + 把收到的消息分发到 per-thread 记录, 但要拿到 origin IP.

    python-osc 的 Dispatcher 不传 origin. 我们用 raw socket 接 UDP, 自己解析.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    print(f"[test] raw UDP listener on {LISTEN_HOST}:{LISTEN_PORT}")

    def _recv_loop():
        from pythonosc.osc_message import OscMessage
        while True:
            try:
                datagram, origin = sock.recvfrom(65535)
            except OSError:
                break
            try:
                message = OscMessage(datagram)
            except Exception:
                continue
            address = message.address
            args = message.params
            origin_ip = origin[0]
            with lock:
                if address == "/kodi/report/ready" and len(args) >= 3:
                    clip_idx = args[0]
                    file_path = args[1]
                    actual_ms = args[2]
                    position_by_machine[origin_ip] = actual_ms
                    file_by_machine[origin_ip] = file_path
                    print(f"  -> {origin_ip}: ready idx={clip_idx} file={file_path} pos={actual_ms}ms")
                elif address == "/kodi/report/play":
                    pass  # 忽略 OnPlay/OnPause 通知
                else:
                    print(f"  -> {origin_ip}: {address} {args}")

    thread = threading.Thread(target=_recv_loop, daemon=True)
    thread.start()
    return sock


def _send_osc_multicast(address, *args):
    """发组播 OSC 到所有机器."""
    builder = osc_message_builder.OscMessageBuilder(address=address)
    for arg in args:
        builder.add_arg(arg)
    datagram = builder.build().dgram
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, 0x18, 1)  # IP_MULTICAST_TTL=1
    sock.sendto(datagram, (SEND_GROUP, SEND_PORT))
    sock.close()
    print(f"[send] {address} {args}")


def _wait_for_all_machines(expected_count, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lock:
            if len(position_by_machine) >= expected_count:
                return True
        time.sleep(0.05)
    return False


def run_test(clip_index, target_seek_ms, timeout=60):
    print(f"\n=== Multi-device alignment: /alignment_ready {clip_index} {target_seek_ms} ===")
    # 先 build_playlist 确保 3 台 state.playlist 已填
    _send_osc_multicast("/build_playlist")
    time.sleep(3)
    print(f"  [pre] build_playlist sent, wait 3s")
    position_by_machine.clear()
    file_by_machine.clear()
    _send_osc_multicast("/alignment_ready", clip_index, target_seek_ms)
    success = _wait_for_all_machines(len(EXPECTED_MACHINES), timeout)
    if not success:
        print(f"[FAIL] 仅收到 {len(position_by_machine)}/{len(EXPECTED_MACHINES)} 机器的 ready 响应 (timeout {timeout}s)")
        print(f"  received: {dict(position_by_machine)}")
        return False
    # 计算偏差
    positions = list(position_by_machine.values())
    min_pos = min(positions)
    max_pos = max(positions)
    spread_ms = max_pos - min_pos
    print(f"\n  positions: {position_by_machine}")
    print(f"  min={min_pos}ms, max={max_pos}ms, spread={spread_ms}ms")
    if spread_ms <= 15:
        print(f"  [PASS] 偏差 {spread_ms}ms ≤ 15ms (README 指标)")
        return True
    else:
        print(f"  [FAIL] 偏差 {spread_ms}ms > 15ms")
        return False


def main() -> int:
    listener_sock = _start_listener()
    time.sleep(0.5)
    print(f"等待机器: {EXPECTED_MACHINES}")

    failures = 0

    # Test 1: clip 2 (huaYao, 小视频) seek 2000ms
    if not run_test(0, 2000, timeout=60):
        failures += 1

    # 让 Chataigne 端先 stop 之前的播放
    _send_osc_multicast("/stop")
    time.sleep(2)

    # Test 2: clip 0 (4K 视频) seek 5000ms
    if not run_test(0, 5000, timeout=120):
        failures += 1

    _send_osc_multicast("/stop")
    time.sleep(2)

    print(f"\n=== {('ALL PASSED' if failures == 0 else f'{failures} TEST(S) FAILED')} ===")
    listener_sock.close()
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
