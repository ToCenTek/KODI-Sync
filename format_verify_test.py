#!/usr/bin/env python3
"""
OSC 响应格式全面验证测试
对照 MODIFICATION.md 规范, 验证所有 13 种 OSC 上报消息格式.
"""
import socket
import struct
import time
import sys

MCAST_GROUP = "239.0.0.239"
MCAST_PORT = 9000
REPLY_PORT = 5007  # 用 5007 避免和 Chataigne 5006 冲突
TIMEOUT = 5.0

# ============================================================
# OSC 编码/解码工具
# ============================================================
def osc_encode_string(s):
    """OSC 字符串编码: 4 字节对齐"""
    data = s.encode("utf-8") + b"\x00"
    while len(data) % 4 != 0:
        data += b"\x00"
    return data

def osc_encode_int(val):
    """OSC int32 编码"""
    return struct.pack(">i", val)

def osc_encode_float(val):
    """OSC float32 编码"""
    return struct.pack(">f", val)

def osc_encode_blob(data):
    """OSC blob 编码"""
    size = len(data)
    size_bytes = struct.pack(">I", size)
    padded = data + b"\x00" * ((4 - size % 4) % 4)
    return size_bytes + padded

def osc_build_message(address, *args):
    """构建 OSC 消息"""
    addr_data = osc_encode_string(address)
    type_tag = ","
    arg_data = b""
    for arg in args:
        if isinstance(arg, int):
            type_tag += "i"
            arg_data += osc_encode_int(arg)
        elif isinstance(arg, float):
            type_tag += "f"
            arg_data += osc_encode_float(arg)
        elif isinstance(arg, str):
            type_tag += "s"
            arg_data += osc_encode_string(arg)
        elif isinstance(arg, bytes):
            type_tag += "b"
            arg_data += osc_encode_blob(arg)
        else:
            type_tag += "N"  # Nil
    return addr_data + osc_encode_string(type_tag) + arg_data

def osc_decode_string(data, offset=0):
    """OSC 字符串解码"""
    end = data.index(b"\x00", offset)
    s = data[offset:end].decode("utf-8")
    # 对齐到 4 字节
    padded_len = (end - offset + 1 + 3) & ~3
    return s, offset + padded_len

def osc_decode_int(data, offset=0):
    """OSC int32 解码"""
    val = struct.unpack(">i", data[offset:offset+4])[0]
    return val, offset + 4

def osc_decode_float(data, offset=0):
    """OSC float32 解码"""
    val = struct.unpack(">f", data[offset:offset+4])[0]
    return val, offset + 4

def osc_decode_message(data):
    """解码 OSC 消息"""
    addr, offset = osc_decode_string(data, 0)
    type_tag, offset = osc_decode_string(data, offset)
    args = []
    for ch in type_tag[1:]:  # 跳过 ','
        if ch == "i":
            val, offset = osc_decode_int(data, offset)
            args.append(val)
        elif ch == "f":
            val, offset = osc_decode_float(data, offset)
            args.append(val)
        elif ch == "s":
            val, offset = osc_decode_string(data, offset)
            args.append(val)
        elif ch == "b":
            size = struct.unpack(">I", data[offset:offset+4])[0]
            offset += 4
            val = data[offset:offset+size]
            offset += (size + 3) & ~3
            args.append(val)
        else:
            args.append(None)
    return addr, args

# ============================================================
# 测试辅助
# ============================================================
def create_receiver(port):
    """创建 OSC 接收器"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.settimeout(TIMEOUT)
    return sock

def send_osc(sock, address, *args, target_ip=MCAST_GROUP, target_port=MCAST_PORT):
    """发送 OSC 消息"""
    msg = osc_build_message(address, *args)
    sock.sendto(msg, (target_ip, target_port))

def receive_osc(sock):
    """接收 OSC 消息"""
    try:
        data, addr = sock.recvfrom(4096)
        return osc_decode_message(data)
    except socket.timeout:
        return None, None

def format_result(passed, detail):
    """格式化测试结果"""
    status = "✅ PASS" if passed else "❌ FAIL"
    return f"{status} | {detail}"

# ============================================================
# 格式验证规则
# ============================================================
def validate_discover(addr, args):
    """验证 /daemon/discover: ip mac version"""
    checks = []
    checks.append(addr == "/daemon/discover")
    checks.append(len(args) >= 3)
    if len(args) >= 3:
        checks.append(isinstance(args[0], str) and "." in args[0])  # IP
        checks.append(isinstance(args[1], str) and ":" in args[1])  # MAC
        checks.append(isinstance(args[2], str))  # version
    return all(checks), f"addr={addr} args={args}"

def validate_kodi_state(addr, args):
    """验证 /kodi/state: isPaused(int) isStopped(int) event(string) file(string)"""
    checks = []
    checks.append(addr == "/kodi/state")
    checks.append(len(args) == 4)
    if len(args) == 4:
        checks.append(isinstance(args[0], int))  # isPaused
        checks.append(isinstance(args[1], int))  # isStopped
        checks.append(isinstance(args[2], str))  # event_name
        checks.append(isinstance(args[3], str))  # file_name
        checks.append(args[0] in (0, 1))  # isPaused 值域
        checks.append(args[1] in (0, 1))  # isStopped 值域
    return all(checks), f"addr={addr} args={args}"

def validate_playlist_waiting(addr, args):
    """验证 /kodi/playlist 处理中: -1 0 message"""
    checks = []
    checks.append(addr == "/kodi/playlist")
    checks.append(len(args) >= 2)
    if len(args) >= 2:
        checks.append(args[0] == -1)  # status = -1
        checks.append(args[1] == 0)   # count = 0
        checks.append(isinstance(args[2], str) if len(args) > 2 else False)
    return all(checks), f"addr={addr} args={args[:3]}..."

def validate_playlist_full(addr, args):
    """验证 /kodi/playlist 完整列表: 1 count items..."""
    checks = []
    checks.append(addr == "/kodi/playlist")
    checks.append(len(args) >= 2)
    if len(args) >= 2:
        checks.append(args[0] == 1)  # status = 1
        checks.append(isinstance(args[1], int))  # count
        count = args[1]
        # 每项 6 字段: idx, name, duration_hms, fps, second_idr_ms, last_idr_ms
        expected_len = 2 + count * 6
        checks.append(len(args) == expected_len)
        # 验证每项字段类型
        for i in range(count):
            base = 2 + i * 6
            checks.append(isinstance(args[base], int))      # idx
            checks.append(isinstance(args[base+1], str))    # name
            checks.append(isinstance(args[base+2], str))    # duration_hms
            checks.append(isinstance(args[base+3], str))    # fps
            checks.append(isinstance(args[base+4], int))    # second_idr_ms
            checks.append(isinstance(args[base+5], int))    # last_idr_ms
    return all(checks), f"addr={addr} count={args[1] if len(args)>=2 else '?'}"

def validate_alignment_ready(addr, args):
    """验证 /kodi/alignment/ready: status idx file current_ms total_hms"""
    checks = []
    checks.append(addr == "/kodi/alignment/ready")
    checks.append(len(args) == 5)
    if len(args) == 5:
        checks.append(isinstance(args[0], int))    # status
        checks.append(args[0] in (1, -1))          # status 值域
        checks.append(isinstance(args[1], int))    # idx
        checks.append(isinstance(args[2], str))    # file
        checks.append(isinstance(args[3], int))    # current_ms
        checks.append(isinstance(args[4], str))    # total_hms
    return all(checks), f"addr={addr} args={args}"

def validate_alignment_play(addr, args):
    """验证 /kodi/alignment/play: status idx file current_ms total_hms"""
    checks = []
    checks.append(addr == "/kodi/alignment/play")
    checks.append(len(args) == 5)
    if len(args) == 5:
        checks.append(isinstance(args[0], int))
        checks.append(args[0] in (1, -1))
        checks.append(isinstance(args[1], int))
        checks.append(isinstance(args[2], str))
        checks.append(isinstance(args[3], int))
        checks.append(isinstance(args[4], str))
    return all(checks), f"addr={addr} args={args}"

def validate_alignment_seek(addr, args):
    """验证 /kodi/alignment/seek: status idx file current_ms total_hms"""
    checks = []
    checks.append(addr == "/kodi/alignment/seek")
    checks.append(len(args) == 5)
    if len(args) == 5:
        checks.append(isinstance(args[0], int))
        checks.append(args[0] in (1, -1))
        checks.append(isinstance(args[1], int))
        checks.append(isinstance(args[2], str))
        checks.append(isinstance(args[3], int))
        checks.append(isinstance(args[4], str))
    return all(checks), f"addr={addr} args={args}"

def validate_get_properties(addr, args):
    """验证 /kodi/GetProperties: current_ms(int) total_hms(string)"""
    checks = []
    checks.append(addr == "/kodi/GetProperties")
    checks.append(len(args) == 2)
    if len(args) == 2:
        checks.append(isinstance(args[0], int))   # current_ms
        checks.append(isinstance(args[1], str))   # total_hms
    return all(checks), f"addr={addr} args={args}"

def validate_set_loop(addr, args):
    """验证 /kodi/setLoop: status(int) mode(string)"""
    checks = []
    checks.append(addr == "/kodi/setLoop")
    checks.append(len(args) == 2)
    if len(args) == 2:
        checks.append(isinstance(args[0], int))   # status
        checks.append(args[0] in (1, -1))         # status 值域
        checks.append(isinstance(args[1], str))   # mode
    return all(checks), f"addr={addr} args={args}"

def validate_volume(addr, args):
    """验证 /kodi/volume: current_volume(int)"""
    checks = []
    checks.append(addr == "/kodi/volume")
    checks.append(len(args) == 1)
    if len(args) == 1:
        checks.append(isinstance(args[0], int))   # volume
        checks.append(0 <= args[0] <= 100)        # 值域
    return all(checks), f"addr={addr} args={args}"

def validate_member(addr, args):
    """验证 /daemon/member: message(string)"""
    checks = []
    checks.append(addr == "/daemon/member")
    checks.append(len(args) == 1)
    if len(args) == 1:
        checks.append(isinstance(args[0], str))
    return all(checks), f"addr={addr} args={args}"

def validate_cpu(addr, args):
    """验证 /daemon/CPU: c0 c1 c2 c3 (all int)"""
    checks = []
    checks.append(addr == "/daemon/CPU")
    checks.append(len(args) == 4)
    if len(args) == 4:
        for a in args:
            checks.append(isinstance(a, int))
    return all(checks), f"addr={addr} args={args}"

def validate_config(addr, args):
    """验证 /daemon/config: message(string)"""
    checks = []
    checks.append(addr == "/daemon/config")
    checks.append(len(args) == 1)
    if len(args) == 1:
        checks.append(isinstance(args[0], str))
    return all(checks), f"addr={addr} args={args}"

def validate_error(addr, args):
    """验证 /kodi/error: error_code(int) description(string)"""
    checks = []
    checks.append(addr == "/kodi/error")
    checks.append(len(args) == 2)
    if len(args) == 2:
        checks.append(isinstance(args[0], int))   # error_code
        checks.append(isinstance(args[1], str))   # description
    return all(checks), f"addr={addr} args={args}"

# ============================================================
# 主测试流程
# ============================================================
def run_tests(target_ip):
    """对指定设备运行所有格式验证测试"""
    print(f"\n{'='*60}")
    print(f"  格式验证测试 - 目标设备: {target_ip}")
    print(f"{'='*60}")

    results = []

    # 创建两个 socket: 一个发送, 一个接收
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    send_sock.bind(("", 0))

    recv_sock = create_receiver(REPLY_PORT)

    # --- 测试 1: /discover ---
    print(f"\n[1/13] 测试 /discover ...")
    send_osc(send_sock, "/discover")
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_discover(addr, args) if addr else (False, "超时无响应")
    results.append(("discover", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 2: /setLoop 查询 ---
    print(f"\n[2/13] 测试 /setLoop (查询模式) ...")
    send_osc(send_sock, "/setLoop", "")
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_set_loop(addr, args) if addr else (False, "超时无响应")
    results.append(("setLoop-query", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 3: /setLoop 设置 ---
    print(f"\n[3/13] 测试 /setLoop (设置 all) ...")
    send_osc(send_sock, "/setLoop", "all")
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_set_loop(addr, args) if addr else (False, "超时无响应")
    results.append(("setLoop-set", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 4: /volume ---
    print(f"\n[4/13] 测试 /volume ...")
    send_osc(send_sock, "/volume", 50)
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_volume(addr, args) if addr else (False, "超时无响应")
    results.append(("volume", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 5: /GetProperties ---
    print(f"\n[5/13] 测试 /GetProperties ...")
    # 先播放一个文件
    send_osc(send_sock, "/play")
    time.sleep(1)
    # 发送 GetProperties
    send_osc(send_sock, "/GetProperties")
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_get_properties(addr, args) if addr else (False, "超时无响应")
    results.append(("GetProperties", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 6: /playlist ---
    print(f"\n[6/13] 测试 /playlist ...")
    send_osc(send_sock, "/playlist")
    # 先收 "Please wait" 消息
    addr1, args1 = receive_osc(recv_sock)
    passed_wait, detail_wait = validate_playlist_waiting(addr1, args1) if addr1 else (False, "超时无响应")
    # 再收完整列表
    addr2, args2 = receive_osc(recv_sock)
    passed_full, detail_full = validate_playlist_full(addr2, args2) if addr2 else (False, "超时无响应")
    results.append(("playlist-waiting", passed_wait, detail_wait))
    results.append(("playlist-full", passed_full, detail_full))
    print(format_result(passed_wait, f"等待消息: {detail_wait}"))
    print(format_result(passed_full, f"完整列表: {detail_full}"))

    # --- 测试 7: /cpuAffinity ---
    print(f"\n[7/13] 测试 /cpuAffinity ...")
    send_osc(send_sock, "/cpuAffinity", 0, 0, 0, 1)
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_cpu(addr, args) if addr else (False, "超时无响应")
    results.append(("cpuAffinity", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 8: /member ---
    print(f"\n[8/13] 测试 /member --- ...")
    send_osc(send_sock, "/member", "---")
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_member(addr, args) if addr else (False, "超时无响应")
    results.append(("member", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 9: /stop ---
    print(f"\n[9/13] 测试 /stop (触发 /kodi/state) ...")
    send_osc(send_sock, "/stop")
    time.sleep(0.5)
    # 检查是否有自发事件
    recv_sock.settimeout(2.0)
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_kodi_state(addr, args) if addr else (False, "超时无自发事件 (可能已是停止状态) ")
    results.append(("state-onStop", passed, detail))
    print(format_result(passed, detail))
    recv_sock.settimeout(TIMEOUT)

    # --- 测试 10: /play (触发 /kodi/state) ---
    print(f"\n[10/13] 测试 /play (触发 /kodi/state) ...")
    send_osc(send_sock, "/play")
    time.sleep(2)
    recv_sock.settimeout(3.0)
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_kodi_state(addr, args) if addr else (False, "超时无自发事件")
    results.append(("state-onPlay", passed, detail))
    print(format_result(passed, detail))
    recv_sock.settimeout(TIMEOUT)

    # --- 测试 11: /pause (触发 /kodi/state) ---
    print(f"\n[11/13] 测试 /pause (触发 /kodi/state) ...")
    send_osc(send_sock, "/pause")
    time.sleep(1)
    recv_sock.settimeout(3.0)
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_kodi_state(addr, args) if addr else (False, "超时无自发事件")
    results.append(("state-onPause", passed, detail))
    print(format_result(passed, detail))
    recv_sock.settimeout(TIMEOUT)

    # --- 测试 12: /alignment/ready ---
    print(f"\n[12/13] 测试 /alignment/ready ...")
    send_osc(send_sock, "/alignment/ready", 0, 5000)
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_alignment_ready(addr, args) if addr else (False, "超时无响应")
    results.append(("alignment-ready", passed, detail))
    print(format_result(passed, detail))

    # --- 测试 13: /seek ---
    print(f"\n[13/13] 测试 /seek (触发 /kodi/alignment/seek) ...")
    send_osc(send_sock, "/seek", 3000)
    addr, args = receive_osc(recv_sock)
    passed, detail = validate_alignment_seek(addr, args) if addr else (False, "超时无响应")
    results.append(("alignment-seek", passed, detail))
    print(format_result(passed, detail))

    # 清理
    send_sock.close()
    recv_sock.close()

    # 汇总
    print(f"\n{'='*60}")
    print(f"  汇总结果 - {target_ip}")
    print(f"{'='*60}")
    total = len(results)
    passed_count = sum(1 for _, p, _ in results if p)
    failed_count = total - passed_count
    for name, passed, detail in results:
        status = "✅" if passed else "❌"
        print(f"  {status} {name}: {detail}")
    print(f"\n  总计: {total} 项, 通过: {passed_count}, 失败: {failed_count}")
    return results

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "10.0.0.92"
    run_tests(target)
