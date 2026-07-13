#!/bin/sh
# KODI-Sync 一键安装脚本
# 用法:
#   scp -r libs root@<设备IP>:~/
#   ssh root@<设备IP> "cd ~/libs && ./install.sh"
#
# 零依赖：离线安装，不需要 pip / 网络 / 编译。

set -e

echo "╔══════════════════════════════════════════╗"
echo "║        KODI-Sync Agent 一键安装          ║"
echo "╚══════════════════════════════════════════╝"

# ---- 1. ffprobe ----
echo ""
echo "[1/6] 解压 ffprobe ..."
mkdir -p /storage/bin
tar -xJf ffmpeg-release-arm64-static.tar.xz
cp ffmpeg-*-arm64-static/ffprobe /storage/bin/ffprobe
chmod +x /storage/bin/ffprobe
rm -rf ffmpeg-*-arm64-static
echo "      -> /storage/bin/ffprobe"

# ---- 2. pip 自举 ----
echo ""
echo "[2/6] 自举 pip ..."
SITE=/storage/.local/lib/python3.11/site-packages
mkdir -p "$SITE"
unzip -o -q pip-26.1.2-py3-none-any.whl -d "$SITE"
echo "      -> pip $(python3 -m pip --version 2>/dev/null | cut -d' ' -f2)"

# ---- 3. Python 库 ----
echo ""
echo "[3/6] 安装 Python 库 ..."
python3 -m pip install --no-compile --no-index --find-links=. \
  python-osc websocket-client --user 2>&1 | tail -1
echo "      -> python-osc, websocket-client"

# ---- 4. daemon ----
echo ""
echo "[4/6] 部署 daemon ..."
cp daemon.py /storage/daemon.py
echo "      -> /storage/daemon.py"

# ---- 5. 自启动 ----
echo ""
echo "[5/6] 设置开机自启动 ..."
cat > /storage/.config/autostart.sh << 'CEOF'
#!/bin/sh
(
  while ! grep -q 2382 /proc/net/tcp 2>/dev/null; do sleep 2; done
  cd /storage && python3 -u /storage/daemon.py > /tmp/agent.log 2>&1 &
)&
CEOF
chmod +x /storage/.config/autostart.sh
echo "      -> /storage/.config/autostart.sh"

# ---- 6. 启动 ----
echo ""
echo "[6/6] 启动 daemon ..."
killall python3 2>/dev/null || true
sleep 2
cd /storage && nohup python3 -u /storage/daemon.py > /tmp/agent.log 2>&1 &
sleep 1
echo "      -> pid $(pgrep -f daemon.py 2>/dev/null || echo '?')"



# ---- 完成 ----
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  安装完成！                               ║"
echo "║                                          ║"
echo "║  日志: tail -f /tmp/agent.log            ║"
echo "║  重启: killall python3                   ║"
echo "╚══════════════════════════════════════════╝"
