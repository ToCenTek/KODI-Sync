#!/bin/sh
# KODI-Sync 一键安装脚本
# 用法:
#   scp -r libs root@<设备IP>:~/
#   ssh root@<设备IP> "cd ~/libs && ./install.sh"

set -e

echo "╔══════════════════════════════════════════╗"
echo "║        KODI-Sync Agent 一键安装          ║"
echo "╚══════════════════════════════════════════╝"

# ---- 1. ffprobe ----
echo ""
echo "[1/5] 解压 ffprobe ..."
mkdir -p /storage/bin
tar -xJf ffmpeg-release-arm64-static.tar.xz
cp ffmpeg-*-arm64-static/ffprobe /storage/bin/ffprobe
chmod +x /storage/bin/ffprobe
rm -rf ffmpeg-*-arm64-static
echo "      -> /storage/bin/ffprobe"

# ---- 2. pip 依赖 ----
echo ""
echo "[2/5] 安装 Python 依赖 ..."
python3 -m ensurepip --upgrade 2>/dev/null || true
python3 -m pip install \
  --no-index \
  --find-links=. \
  python-osc websocket-client \
  --user
echo "      -> python-osc, websocket-client"

# ---- 3. daemon ----
echo ""
echo "[3/5] 部署 daemon ..."
cp daemon.py /storage/kodi_agent.py
echo "      -> /storage/kodi_agent.py"

# ---- 4. 自启动 ----
echo ""
echo "[4/5] 设置开机自启动 ..."
cat > /storage/.config/autostart.sh << 'CEOF'
#!/bin/sh
(
  while ! grep -q 2382 /proc/net/tcp 2>/dev/null; do sleep 2; done
  cd /storage && python3 -u /storage/kodi_agent.py > /tmp/agent.log 2>&1 &
)&
CEOF
chmod +x /storage/.config/autostart.sh
echo "      -> /storage/.config/autostart.sh"

# ---- 5. 启动 ----
echo ""
echo "[5/5] 启动 daemon ..."
killall python3 2>/dev/null || true
sleep 2
cd /storage && nohup python3 -u /storage/kodi_agent.py > /tmp/agent.log 2>&1 &
sleep 1
echo "      -> pid $(pgrep -f kodi_agent.py 2>/dev/null || echo '?')"

# ---- 完成 ----
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  安装完成！                               ║"
echo "║                                          ║"
echo "║  日志: tail -f /tmp/agent.log            ║"
echo "║  重启: killall python3                   ║"
echo "╚══════════════════════════════════════════╝"
