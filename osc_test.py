"""OSC test harness for kodi_agent.py.

启动一个 OSC listener 监听 5006, 发组播 OSC 命令到 239.0.0.69:9000,
验证 agent 响应. 全在 10.0.0.29 上跑, 用 agent 的开发机即可.

用法: python3 osc_test.py
"""
import socket
import sys
import threading
import time

from pythonosc import osc_message_builder
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer


LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5006
SEND_GROUP = "239.0.0.69"
SEND_PORT = 9000

received_log: list[tuple[str, tuple]] = []


def _handle_message(address, *args):
    received_log.append((address, args))
    print(f"[recv] {address} {args}")


def _start_listener() -> ThreadingOSCUDPServer:
    dispatcher = Dispatcher()
    dispatcher.map("/*", _handle_message)
    server = ThreadingOSCUDPServer((LISTEN_HOST, LISTEN_PORT), dispatcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[test] listener on {LISTEN_HOST}:{LISTEN_PORT}")
    return server


def _send_osc(address: str, *args) -> None:
    builder = osc_message_builder.OscMessageBuilder(address=address)
    for arg in args:
        builder.add_arg(arg)
    datagram = builder.build().dgram
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, 0x18, 1)  # IP_MULTICAST_TTL=1
    sock.sendto(datagram, (SEND_GROUP, SEND_PORT))
    sock.close()
    print(f"[send] {address} {args}")


def _wait_for_messages(count: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(received_log) >= count:
            return True
        time.sleep(0.05)
    return False


def _all_received_str() -> str:
    return " | ".join(f"{a} {args}" for a, args in received_log)


def run_suite() -> int:
    server = _start_listener()
    time.sleep(0.5)
    failures = 0

    # Test 1: /discover -> /kodi/discover
    print("\n=== Test 1: /discover ===")
    received_log.clear()
    _send_osc("/discover")
    if _wait_for_messages(1, 3.0):
        last_addr, last_args = received_log[-1]
        if last_addr == "/kodi/discover" and len(last_args) == 2:
            print(f"[pass] /kodi/discover ip={last_args[0]} mac={last_args[1]}")
        else:
            print(f"[FAIL] unexpected: {last_addr} {last_args}")
            failures += 1
    else:
        print("[FAIL] no response within 3s")
        failures += 1

    # Test 2: /build_playlist -> /kodi/playlist
    print("\n=== Test 2: /build_playlist ===")
    received_log.clear()
    _send_osc("/build_playlist")
    if _wait_for_messages(1, 5.0):
        last_addr, last_args = received_log[-1]
        if last_addr == "/kodi/playlist" and len(last_args) >= 1:
            clip_count = int(last_args[0])
            print(f"[pass] /kodi/playlist count={clip_count}")
            for i in range(clip_count):
                print(f"        [{last_args[1 + i * 2]}] {last_args[2 + i * 2]}")
        else:
            print(f"[FAIL] unexpected: {last_addr} {last_args}")
            failures += 1
    else:
        print("[FAIL] no response within 5s")
        failures += 1

    # Test 3: /alignment_ready 0 2000 -> /kodi/report/ready (20s 等待, 看完整收包)
    # 用小视频 huaYao.mp4 (63MB) 测, 避免 4K 首次解码器初始化的 10-20s 延迟
    print("\n=== Test 3: /alignment_ready 2 2000 (huaYao, wait 20s) ===")
    received_log.clear()
    _send_osc("/alignment_ready", 2, 2000)
    # 先等 OnPlay 触发, 再清空 log, 只关注 /kodi/report/ready
    _wait_for_messages(1, 3.0)
    received_log.clear()
    _wait_for_messages(1, 120.0)  # alignment 需 30-60s (Kodi 首次解码 4K)
    print(f"  [info] all received: {_all_received_str()}")
    if not received_log:
        print("[FAIL] no response within 20s")
        failures += 1
    else:
        last_addr, last_args = received_log[-1]
        if last_addr == "/kodi/report/ready" and len(last_args) >= 3:
            print(f"[pass] /kodi/report/ready idx={last_args[0]} file={last_args[1]} pos={last_args[2]}ms total={last_args[3] if len(last_args) > 3 else '?'}ms")
        else:
            print(f"[FAIL] last message: {last_addr} {last_args}")
            failures += 1

    # Test 4: /play 0 -> /kodi/report/play
    print("\n=== Test 4: /play 0 ===")
    time.sleep(1.0)
    received_log.clear()
    _send_osc("/play", 0)
    if _wait_for_messages(1, 8.0):
        last_addr, last_args = received_log[-1]
        if last_addr == "/kodi/report/play":
            print(f"[pass] /kodi/report/play file={last_args[0]} state={last_args[1]} ms={last_args[2] if len(last_args) > 2 else '?'}")
        else:
            print(f"[FAIL] unexpected: {last_addr} {last_args}")
            failures += 1
    else:
        print("[FAIL] no /kodi/report/play within 8s")
        failures += 1

    # Test 5: /pause -> /kodi/report/play (paused)
    print("\n=== Test 5: /pause ===")
    time.sleep(1.0)
    received_log.clear()
    _send_osc("/pause")
    if _wait_for_messages(1, 3.0):
        last_addr, last_args = received_log[-1]
        if last_addr == "/kodi/report/play" and last_args[1] == "isPaused":
            print(f"[pass] paused ms={last_args[2] if len(last_args) > 2 else '?'}")
        else:
            print(f"[FAIL] unexpected: {last_addr} {last_args}")
            failures += 1
    else:
        print("[FAIL] no pause response within 3s")
        failures += 1

    # Test 6: /query_current_time -> /kodi/report/current_time
    print("\n=== Test 6: /query_current_time ===")
    time.sleep(0.5)
    received_log.clear()
    _send_osc("/query_current_time")
    if _wait_for_messages(1, 3.0):
        last_addr, last_args = received_log[-1]
        if last_addr == "/kodi/report/current_time":
            print(f"[pass] current_time: {last_args[0]} ms")
        else:
            print(f"[FAIL] unexpected: {last_addr} {last_args}")
            failures += 1
    else:
        print("[FAIL] no response within 3s")
        failures += 1

    # Test 7: /stop -> /kodi/report/play (stopped)
    print("\n=== Test 7: /stop ===")
    time.sleep(0.5)
    received_log.clear()
    _send_osc("/stop")
    if _wait_for_messages(1, 5.0):
        last_addr, last_args = received_log[-1]
        if last_addr == "/kodi/report/play" and last_args[1] == "isStopped":
            print(f"[pass] stopped")
        else:
            print(f"[FAIL] unexpected: {last_addr} {last_args}")
            failures += 1
    else:
        print("[FAIL] no stop response within 5s")
        failures += 1

    print(f"\n=== {('ALL TESTS PASSED' if failures == 0 else f'{failures} TEST(S) FAILED')} ===")
    server.shutdown()
    return 0 if failures == 0 else 1


def main() -> int:
    return run_suite()


if __name__ == "__main__":
    sys.exit(main())
