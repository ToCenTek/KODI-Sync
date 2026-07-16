#!/bin/bash
DEVICE=$1
if [ -z "$DEVICE" ]; then
    echo "用法: $0 <主机号: 10.0.0.69 的最后一段>   例如: $0 69"
    exit 1
fi

HOST="10.0.0.$DEVICE"
LIBS_SRC="/Users/yhc/Documents/Chataigne/modules/KODI-Sync/libs"
USER="root"

echo "[1/5] 检查 SSH 连接到 $HOST ..."
if ! ssh -q -o ConnectTimeout=5 -o BatchMode=yes "$USER@$HOST" "true" 2>/dev/null; then
    echo "错误: 无法 SSH 到 $USER@$HOST"
    exit 1
fi
echo "      连接正常"

echo "[2/5] 上传 libs 目录 ..."
ssh "$USER@$HOST" "rm -f ~/libs/*.whl; mkdir -p ~/libs" 2>/dev/null || true
tar c -C "$LIBS_SRC" --exclude __pycache__ --exclude '*.pyc' . | \
  ssh "$USER@$HOST" "tar x -C ~/libs" 2>/dev/null
echo "      上传完成"

echo "[3/5] 检查安装状态 ..."
MISSING=$(ssh -T "$USER@$HOST" <<'REMOTESCRIPT'
  items=""
  test -f ~/libs/ffprobe && test -x ~/libs/ffprobe || items="$items ffprobe"
  python3 -c "import pythonosc.udp_client" 2>/dev/null || items="$items pythonosc"
  python3 -c "import websocket" 2>/dev/null || items="$items websocket"
  test -f /storage/.config/autostart.sh || items="$items autostart"
  echo "$items"
REMOTESCRIPT
)

if [ -z "$MISSING" ]; then
    echo "      检查通过 -> 执行 reload"

    echo "[4/5] 停止旧进程 ..."
    ssh "$USER@$HOST" "pkill -f 'multiscreen-sync\.py' 2>/dev/null; sleep 2; pkill -9 -f 'multiscreen-sync\.py' 2>/dev/null; sleep 1"
    echo "      已停止"

    echo "[5/5] 启动 ..."
    ssh -T "$USER@$HOST" "cd ~/libs && nohup python3 -u ~/libs/multiscreen-sync.py > /tmp/multiscreen-sync.log 2>&1 < /dev/null &"
    sleep 1
    DAEMON_PID=$(ssh "$USER@$HOST" "pgrep -f multiscreen-sync.py")
    if [ -n "$DAEMON_PID" ]; then
        echo "      已启动 (PID $DAEMON_PID)"
    else
        echo "      警告: 可能未启动 (请检查 /tmp/multiscreen-sync.log)"
    fi
else
    MISSING=$(echo "$MISSING" | xargs | sed 's/ /, /g')
    echo "      缺失: $MISSING"
    echo "      -> 执行全新安装"

    echo "[4/5] 全新安装 ..."
    ssh -T "$USER@$HOST" "cd ~/libs && bash install.sh" 2>&1 | sed 's/^/      /'
fi

echo ""
echo "完成. 查看日志:"
echo "  ssh $USER@$HOST 'tail -f /tmp/multiscreen-sync.log'"
