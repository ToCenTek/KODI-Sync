# Kodi Sync Agent

多屏视频墙同步代理，运行于 CoreELEC 系统上，通过 OSC 协议接收控制指令，经 JSON-RPC 操控 Kodi 媒体中心实现多设备帧级同步播放。

## 设计意图

构建一个多台 CoreELEC 媒体中心的视频墙系统，每台媒体中心运行一个轻量 Python 代理：

- 通过组播 OSC（239.0.0.69:9000）接收控制命令
- 经 websocket JSON-RPC（localhost:9090）控制 Kodi
- 通过单播 OSC 向控制器（Chataigne）上报状态
- 所有播放操作在各设备独立执行，控制器统一协调

## 功能列表

### 核心同步功能

| OSC 命令 | 功能 | 响应 |
|---|---|---|
| `/discover` | 设备发现 | `/daemon/discover` (IP, MAC, Kodi版本) |
| `/build_playlist` | 构建播放列表 | `/kodi/playlist` (数量, 文件列表) |
| `/alignment/ready <idx> <pos_ms>` | 对齐准备：加载 → Seek → 暂停 → 上报位置 | `/kodi/alignment/ready` (idx, 文件路径, 实际位置ms, 视频信息) |
| `/alignment/play <idx> <pos_ms> <delay_ms>` | 对齐播放：Open → Seek → 暂停 → delay → 恢复 → 上报 | `/kodi/alignment/play` (idx, 文件路径, "isPlaying") |
| `/play` | 播放 | `/kodi/play` (事件名) |
| `/pause` | 暂停 | `/kodi/pause` (事件名) |
| `/stop` | 停止 | `/kodi/stop` (isStopped, 事件名) |
| `/seek <pos_ms> [delay_ms]` | 跳转到指定位置（支持暂停延迟恢复） | `/kodi/seekToTime` (状态) |
| `/playpause` | 播放/暂停切换 | `/kodi/playpause` (isPaused, 事件名) |
| `/GetProperties` | 查询当前时间 | `/kodi/GetProperties` (原始时间, 总时长ms) |

### 扩展功能

| OSC 命令 | 功能 | 响应 |
|---|---|---|
| `/setLoop <mode>` | 设置循环模式（all/one/off） | `/kodi/setLoop` (当前模式) |
| `/setLoop <空>` | 查询当前循环模式 | `/kodi/setLoop` (当前模式) |
| `/cpuAffinity <c0> <c1> <c2> <c3>` | 设置 CPU 亲和性（0=不亲和, 1=亲和） | `/daemon/CPU` (c0 c1 c2 c3) |
| `/member join` | 加入组播组 | `/daemon/member` ("is Join multicast") |
| `/member leave` | 离开组播组 | `/daemon/member` ("is Leave multicast") |
| `/member ---` | 查询组播组成员状态 | `/daemon/member` ("I am in multicast group…" / "I am not in the multicast group") |

### 事件推送（自发状态上报）

| 触发条件 | 响应 |
|---|---|
| Kodi 停止播放（Player.OnStop） | `/kodi/stop`, 1 + `/kodi/isPaused`, 0 |
| Kodi 开始播放（Player.OnAVStart） | `/kodi/stop`, 0 + `/kodi/isPaused`, 0 |
| Kodi 恢复播放（Player.OnResume / Player.OnPlay） | `/kodi/isPaused`, 0 |
| Kodi 暂停（Player.OnPause） | `/kodi/isPaused`, 1 |

### 状态指示灯参数说明

`/kodi/stop` 的参数：

| args[0] | args[1] | 含义 |
|---|---|---|
| 1 | "Player.OnStop" / "is Stopped" | 已停止 |
| 0 | — | 非停止状态（播放中/准备中） |

`/kodi/isPaused` 的参数：

| arg | 含义 |
|---|---|
| 1 | 已暂停 |
| 0 | 播放中 |

### 循环模式

| 模式 | 行为 |
|---|---|
| `off` | 播完停止（Kodi 原生） |
| `one` | 单曲循环（Kodi 原生） |
| `all` | 全部循环（Kodi 原生） |

### `/alignment/ready` 上报信息

上报格式：`(状态, idx, 文件路径, 实际位置ms, 信息)`

示例：

```
/kodi/alignment/ready : 1 0 /storage/videos/4K_29.97-Chimei-inn-RoastDuck.mp4 1101 ready
```

| 字段 | 含义 |
|---|---|
| 状态 | 1=成功, -1=失败 |
| idx | 文件索引 |
| 文件路径 | 完整路径 |
| 位置 | Seek+暂停后 Kodi 的实际位置（ms） |
| 信息 | ready / 错误描述 |

### `/alignment/play` 上报信息

上报格式：`(状态, idx, 文件路径, 实际位置ms, 信息)`

| 字段 | 含义 |
|---|---|
| 状态 | 0=已恢复播放, -1=失败 |
| idx | 文件索引 |
| 文件路径 | 完整路径 |
| 实际位置 | 恢复前的实际位置（ms） |
| 信息 | "isPlaying" / 错误描述 |

## 技术架构

```
┌──────────────┐    OSC 组播（239.0.0.69:9000）    ┌──────────────┐
│  Chataigne  │ ─────────────────────────────→ │ CoreELEC 媒体中心 │
│  控制器      │ ←── OSC 单播（:5006）────────── │  kodi_agent.py  │
└──────────────┘                                  │       ↓        │
                                                  │ JSON-RPC WS    │
                                                  │  localhost:9090 │
                                                  │       ↓        │
                                                  │  Kodi 媒体中心  │
                                                  └──────────────┘
```

## 实测性能

三台 CoreELEC 20.5 / S905X3 / 2GB 媒体中心（10.0.0.29/69/89）测试结果：

### 对齐精度

| 测试项 | 结果 |
|---|---|
| `/ready` 对齐后三台位置差 | ≤ 15ms（< 1/2 帧 @30fps） |

## 安装

### 依赖

```bash
python3 -m ensurepip --upgrade 2>/dev/null || true
python3 -m pip install python-osc --user
```

### 部署

```bash
for ip in 10.0.0.69 10.0.0.89 10.0.0.29; do
  scp daemon.py root@$ip:/storage/kodi_agent.py
  ssh root@$ip "killall python3 2>/dev/null; sleep 2; cd /storage && nohup python3 -u /storage/kodi_agent.py > /tmp/agent.log 2>&1 &"
done
```

### 自启动

```bash
cat > /storage/.config/autostart.sh << 'EOF'
#!/bin/sh
(
  while ! grep -q 2382 /proc/net/tcp 2>/dev/null; do sleep 2; done
  cd /storage && python3 -u /storage/kodi_agent.py > /tmp/agent.log 2>&1 &
)&
EOF
chmod +x /storage/.config/autostart.sh
```

## 注意事项

1. **CPU 亲和性**：默认绑定最后一块核心（CPU[3]），避免与 Kodi 视频解码（CPU 0/1）竞争；可通过 `/cpuAffinity` 动态调整
2. **组播地址**：`239.0.0.69:9000`，硬编码（暂不支持环境变量配置）
3. **单播/组播**：控制器发命令到组播地址，所有成员可接收；成员单播到 `控制器IP:5006` 上报
4. **成员管理**：`/member leave` 后设备离开组播组，不再接收组播命令；`/member join` 重新加入
5. **上报端口**：固定 5006
6. **依赖**：需要 `python-osc` 库
7. **Kodi Seek 精度**：Kodi 的 `Player.Seek` 只能跳到最近的关键帧（I 帧），精度受视频 GOP 大小限制
8. **事件驱动**：所有 Kodi 状态变更通过 websocket 事件驱动，无轮询

## 命名空间约定

| 前缀 | 用途 | 示例 |
|---|---|---|
| `/kodi/…` | 与 Kodi 功能直接相关的上报 | `/kodi/stop`, `/kodi/play`, `/kodi/isPaused` |
| `/daemon/…` | 与 Kodi 无关的 daemon 自身功能 | `/daemon/discover`, `/daemon/member`, `/daemon/CPU` |
