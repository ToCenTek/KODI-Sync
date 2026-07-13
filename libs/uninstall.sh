#!/bin/sh
# KODI-Sync 一键卸载脚本
# 把 install.sh 装的东西全部清掉，恢复裸机状态。
# 用法:
#   ssh root@<设备IP> "cd ~/libs && ./uninstall.sh"

set -e

echo "╔══════════════════════════════════════════╗"
echo "║        KODI-Sync Agent 一键卸载          ║"
echo "╚══════════════════════════════════════════╝"

# ---- 1. 停止 daemon ----
echo ""
echo "[1/5] 停止 daemon ..."
pkill -f daemon.py 2>/dev/null && echo "      -> 已停止" || echo "      -> 未运行"

# ---- 2. 删除 daemon ----
echo ""
echo "[2/5] 删除 daemon ..."
rm -f /storage/daemon.py
echo "      -> /storage/daemon.py 已删除"

# ---- 3. 删除 ffprobe ----
echo ""
echo "[3/5] 删除 ffprobe ..."
rm -f /storage/bin/ffprobe
echo "      -> /storage/bin/ffprobe 已删除"

# ---- 4. 卸载 Python 库 ----
echo ""
echo "[4/5] 卸载 Python 库 ..."
python3 -m pip uninstall -y python-osc websocket-client pip 2>/dev/null || true
rm -rf /storage/.local/lib/python3.11/site-packages/pythonosc*
rm -rf /storage/.local/lib/python3.11/site-packages/websocket*
rm -rf /storage/.local/lib/python3.11/site-packages/pip*
rm -rf /storage/.local/lib/python3.11/site-packages/setuptools*
rm -rf /storage/.local/lib/python3.11/site-packages/_distutils_hack
rm -rf /storage/.local/lib/python3.11/site-packages/__pycache__
echo "      -> Python 库已清理"

# ---- 5. 删除自启动 ----
echo ""
echo "[5/5] 删除自启动配置 ..."
rm -f /storage/.config/autostart.sh
echo "      -> /storage/.config/autostart.sh 已删除"

# ---- 完成 ----
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  已卸载，机器恢复裸机状态。               ║"
echo "╚══════════════════════════════════════════╝"
