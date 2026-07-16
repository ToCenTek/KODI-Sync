#!/bin/sh
# KODI-Sync 一键卸载脚本
# 把 install.sh 装的东西全部清掉, 恢复裸机状态.
# 用法:
#   ssh root@<设备IP> "cd ~/libs && ./uninstall.sh"
# 卸载完成后可手动删除 ~/libs 目录

set -e

LINE="════════════════════════════════════════════════════════════════════════════════"
echo "╔${LINE}╗"
echo "║    Multiscreen Sync 一键卸载                                                   ║"
echo "╚${LINE}╝"

# ---- 1. 停止 multiscreen-sync ----
echo ""
echo "[1/4] 停止 multiscreen-sync ..."
pkill -9 -f multiscreen-sync.py 2>/dev/null && echo "      -> 已停止" || echo "      -> 未运行"

# ---- 2. 卸载 Python 库 ----
echo ""
echo "[2/4] 卸载 Python 库 ..."
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SITE="/storage/.local/lib/python$PYVER/site-packages"
rm -rf "$SITE"/pythonosc* "$SITE"/websocket*
rm -f "$SITE/distutils-precedence.pth" 2>/dev/null || true
rm -rf "$SITE"/__pycache__
# 清理遗留在错误 python3.11 目录的历史垃圾
rm -rf /storage/.local/lib/python3.11 2>/dev/null || true
echo "      -> Python 库已清理"

# ---- 3. 删除自启动 ----
echo ""
echo "[3/4] 删除自启动配置 ..."
rm -f /storage/.config/autostart.sh
echo "      -> /storage/.config/autostart.sh 已删除"

# ---- 4. 清空日志 ----
echo ""
echo "[4/4] 清空日志 ..."
> /tmp/multiscreen-sync.log
size=$(ls -l /tmp/multiscreen-sync.log 2>/dev/null | awk '{print $5}')
echo "      -> 日志: /tmp/multiscreen-sync.log 当前大小: ${size:-0} 字节"

# ---- 完成 ----
echo ""
LINE="════════════════════════════════════════════════════════════════════════════════"
echo "╔${LINE}╗"
echo "║  已卸载                                                                        ║"
echo "║  如需完全清除, 请手动删除 ~/libs 目录.                                         ║"
echo "╚${LINE}╝"
