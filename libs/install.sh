#!/bin/sh
# KODI-Sync 一键安装脚本
# 用法:
#   scp -r libs root@<设备IP>:~/
#   ssh root@<设备IP> "cd ~/libs && ./install.sh"
#
# 零依赖: 离线安装, 不需要 pip / 网络 / 编译.
# 所有组件自包含在 libs/ 目录下, multiscreen-sync.py 从本目录运行.

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LINE="════════════════════════════════════════════════════════════════════════════════"
echo "╔${LINE}╗"
echo "║    Multiscreen Sync 一键安装                                                   ║"
echo "╚${LINE}╝"

# ---- 1. ffprobe ----
echo ""
echo "[1/4] 解压 ffprobe ..."
tar -xJf "$DIR/ffmpeg-release-arm64-static.tar.xz" -C "$DIR"
cp "$DIR"/ffmpeg-*-arm64-static/ffprobe "$DIR/ffprobe"
chmod +x "$DIR/ffprobe"
rm -rf "$DIR"/ffmpeg-*-arm64-static
echo "      -> $DIR/ffprobe"

# ---- 2. Python 库 ----
echo ""
echo "[2/4] 安装 Python 库 ..."
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SITE="/storage/.local/lib/python$PYVER/site-packages"
mkdir -p "$SITE"
rm -f "$SITE/distutils-precedence.pth" 2>/dev/null || true

if [ "$PYVER" = "3.7" ]; then
  WHLS="python_osc-1.8.3 websocket_client-1.6.1"
else
  WHLS="python_osc-1.10.2 websocket_client-1.9.0 pip-26.1.2"
fi
for name in $WHLS; do
  whl="$DIR/${name}-py3-none-any.whl"
  unzip -o -q "$whl" -d "$SITE"
  echo "      -> $(basename "$whl")"
done

# ---- 3. 自启动 ----
echo ""
echo "[3/4] 设置开机自启动 ..."
cat > /storage/.config/autostart.sh << CEOF
#!/bin/sh
(
  while ! grep -q 2382 /proc/net/tcp 2>/dev/null; do sleep 2; done
  cd "$DIR" || exit; python3 -u "multiscreen-sync.py" > /tmp/multiscreen-sync.log 2>&1 < /dev/null &
)&
CEOF
chmod +x /storage/.config/autostart.sh
echo "      -> /storage/.config/autostart.sh"

# ---- 4. 启动 ----
echo ""
echo "[4/4] 启动 multiscreen-sync ..."
pkill -9 -f multiscreen-sync.py 2>/dev/null || true
sleep 2
nohup python3 -u "$DIR/multiscreen-sync.py" > /tmp/multiscreen-sync.log 2>&1 < /dev/null &
echo "      -> pid $!"

# ---- 完成 ----
echo ""
LINE="════════════════════════════════════════════════════════════════════════════════"
echo "╔${LINE}╗"
echo "║  安装完成!                                                                     ║"
echo "║                                                                                ║"
echo "║  日志: tail -f /tmp/multiscreen-sync.log                                       ║"
echo "║  进程: pgrep -f multiscreen-sync.py                                            ║"
echo "║  停止: pkill -9 -f multiscreen-sync.py                                         ║"
echo "║  前台启动: python3 multiscreen-sync.py                                         ║"
echo "║  后台启动: nohup python3 multiscreen-sync.py > /tmp/multiscreen-sync.log 2>&1 &║"
echo "╚${LINE}╝"
