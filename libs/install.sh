#!/bin/sh
# KODI-Sync 一键安装脚本
# 用法:
#   scp -r libs root@<设备IP>:~/
#   ssh root@<设备IP> "cd ~/libs && ./install.sh"
#
# 零依赖：离线安装，不需要 pip / 网络 / 编译。
# 所有组件自包含在 libs/ 目录下，daemon.py 从本目录运行。

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
echo "╔══════════════════════════════════════════╗"
echo "║     Multiscreen Sync daemon 一键安装     ║"
echo "╚══════════════════════════════════════════╝"

# ---- 1. ffprobe ----
echo ""
echo "[1/6] 解压 ffprobe ..."
tar -xJf "$DIR/ffmpeg-release-arm64-static.tar.xz" -C "$DIR"
cp "$DIR"/ffmpeg-*-arm64-static/ffprobe "$DIR/ffprobe"
chmod +x "$DIR/ffprobe"
rm -rf "$DIR"/ffmpeg-*-arm64-static
echo "      -> $DIR/ffprobe"

# ---- 2. pip 自举 ----
echo ""
echo "[2/6] 自举 pip ..."
SITE=/storage/.local/lib/python3.11/site-packages
mkdir -p "$SITE"
unzip -o -q "$DIR/pip-26.1.2-py3-none-any.whl" -d "$SITE"
echo "      -> pip $(python3 -m pip --version 2>/dev/null | cut -d' ' -f2)"

# ---- 3. Python 库 ----
echo ""
echo "[3/6] 安装 Python 库 ..."
python3 -m pip install --root-user-action=ignore --no-compile --no-index --find-links="$DIR" \
  python-osc websocket-client --user 2>&1 | tail -1
echo "      -> python-osc, websocket-client"

# ---- 4. 自启动 ----
echo ""
echo "[4/6] 设置开机自启动 ..."
cat > /storage/.config/autostart.sh << CEOF
#!/bin/sh
(
  while ! grep -q 2382 /proc/net/tcp 2>/dev/null; do sleep 2; done
  cd "$DIR" && python3 -u "$DIR/daemon.py" > /tmp/daemon.log 2>&1 &
)&
CEOF
chmod +x /storage/.config/autostart.sh
echo "      -> /storage/.config/autostart.sh"

# ---- 5. 启动 ----
echo ""
echo "[5/6] 启动 daemon ..."
pkill -9 -f daemon.py 2>/dev/null || true
sleep 2
cd "$DIR" && nohup python3 -u "$DIR/daemon.py" > /tmp/daemon.log 2>&1 &
sleep 1
echo "      -> pid $(pgrep -f daemon.py 2>/dev/null || echo '?')"

# ---- 完成 ----
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  安装完成!                               ║"
echo "║                                          ║"
echo "║  日志: tail -f /tmp/daemon.log           ║"
echo "║  进程: pgrep -f daemon.py                ║"
echo "║  停止: pkill -9 -f daemon.py             ║"
echo "║  前台启动: python3 daemon.py             ║"
echo "║  后台启动: nohup python3 daemon.py > /tmp/daemon.log 2>&1 &  "
echo "╚══════════════════════════════════════════╝"
