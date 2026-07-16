#!/bin/sh
# KODI-Sync 一键卸载脚本
# 把 install.sh 装的东西全部清掉，恢复裸机状态。
# 用法:
#   ssh root@<设备IP> "cd ~/libs && ./uninstall.sh"
# 卸载完成后可手动删除 ~/libs 目录

set -e

echo "╔══════════════════════════════════════════╗"
echo "║        KODI-Sync Agent 一键卸载           ║"
echo "╚══════════════════════════════════════════╝"

# ---- 1. 停止 daemon ----
echo ""
echo "[1/4] 停止 daemon ..."
pkill -f daemon.py 2>/dev/null && echo "      -> 已停止" || echo "      -> 未运行"

# ---- 2. 卸载 Python 库 ----
echo ""
echo "[2/4] 卸载 Python 库 ..."
python3 -m pip uninstall -y python-osc websocket-client pip 2>/dev/null || true
rm -rf /storage/.local/lib/python3.11/site-packages/pythonosc*
rm -rf /storage/.local/lib/python3.11/site-packages/websocket*
rm -rf /storage/.local/lib/python3.11/site-packages/pip*
rm -rf /storage/.local/lib/python3.11/site-packages/setuptools*
rm -rf /storage/.local/lib/python3.11/site-packages/_distutils_hack
rm -rf /storage/.local/lib/python3.11/site-packages/__pycache__
echo "      -> Python 库已清理"

# ---- 3. 删除自启动 ----
echo ""
echo "[3/4] 删除自启动配置 ..."
rm -f /storage/.config/autostart.sh
echo "      -> /storage/.config/autostart.sh 已删除"

# ---- 4. 清空日志 ----
echo ""
echo "[4/4] 清空日志 ..."
> /tmp/daemon.log
size=$(ls -l /tmp/daemon.log 2>/dev/null | awk '{print $5}')
echo "      -> 日志: /tmp/daemon.log 当前大小: ${size:-0} 字节"

# ---- 完成 ----
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  已卸载                                   ║"
echo "║  如需完全清除, 请手动删除 ~/libs 目录.       ║"
echo "╚══════════════════════════════════════════╝"
 