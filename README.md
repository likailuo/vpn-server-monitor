# VPN Server Monitor v1.0

轻量级 VPN 服务器实时监控面板，支持 **sing-box / WireGuard / OpenVPN**。

---

## 功能总览

| 模块 | 内容 |
|------|------|
| CPU | 使用率、多核进度条、频率、温度 |
| 内存 | 已用/可用/Swap |
| 磁盘 | 挂载点空间 + 实时读写速度 |
| 系统负载 | Load 1m / 5m / 15m + 趋势图 |
| 网卡流量 | 实时 Mbps 折线图（中文化标签） |
| vnStat 统计 | 24h / 30d / 8w / 12m 服务器总流量 GB |
| VPN 服务 | 运行状态 + 运行时长 |
| 端口速率 | 各协议端口实时 Mbps + 连接数 |
| 活跃客户端 | IP + 国家 + 城市 + ISP + 协议 |

### 界面预览

![screenshot](https://via.placeholder.com/800x500/1e293b/e2e8f0?text=VPN+Monitor+Dashboard)

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.8+ · FastAPI · WebSocket |
| 前端 | HTML5 · Tailwind CSS · ECharts |
| 数据 | psutil · vnStat · iptables · ip-api.com |

---

## 前置要求

部署前请确保服务器已安装以下基础软件：

```bash
# Debian/Ubuntu
apt-get install -y python3 python3-pip vnstat iptables

# CentOS/RHEL
yum install -y python3 python3-pip vnstat iptables
```

---

## 一键部署

```bash
# 1. 克隆仓库到服务器
git clone https://github.com/你的用户名/vpn-server-monitor.git
cd vpn-server-monitor

# 2. 执行部署脚本
chmod +x deploy.sh
sudo ./deploy.sh

# 指定端口（默认 8088）
sudo ./deploy.sh 9090
```

部署完成后访问 `http://你的服务器IP:8088`

---

## 支持的 VPN 类型

| VPN | 检测方式 | 客户端追踪 |
|-----|----------|-----------|
| **sing-box** | systemd + 配置文件解析 | TCP (ss) + UDP (journal 日志) |
| WireGuard | `wg show dump` | 握手时间 + 流量 |
| OpenVPN | `openvpn-status.log` | 状态文件解析 |

面板启动时自动检测 VPN 类型，无需手动配置。

---

## 目录结构

```
vpn-server-monitor/
├── deploy.sh          # 一键部署脚本
├── server.py          # FastAPI 后端
├── requirements.txt   # Python 依赖
├── .env.example       # 环境变量模板
├── static/
│   └── index.html     # 监控仪表盘
├── LICENSE            # MIT 开源协议
└── README.md          # 本文档
```

---

## 环境变量配置

复制 `.env.example` 了解所有可配置项：

```bash
cp .env.example .env
# 编辑 .env 文件，按需修改配置
```

**可选**：在 `/etc/systemd/system/vpn-monitor.service` 的 `[Service]` 中添加：

```ini
Environment=PORT=8088              # 监听端口
Environment=HOST=0.0.0.0           # 监听地址
Environment=VPN_TYPE=auto          # auto / singbox / wireguard / openvpn
Environment=AUTH_TOKEN=your_token  # 启用鉴权后，WebSocket 需传 ?token=xxx
Environment=WG_INTERFACE=wg0       # WireGuard 网卡名
Environment=SINGBOX_CONFIG=/etc/s-box/sb.json  # sing-box 配置路径
```

---

## 管理命令

```bash
systemctl status vpn-monitor     # 服务状态
systemctl restart vpn-monitor    # 重启
systemctl stop vpn-monitor       # 停止
journalctl -u vpn-monitor -f     # 实时日志
```

---

## API 接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 监控仪表盘 HTML |
| `/api/health` | GET | 健康检查 |
| `/api/status` | GET | 一次性获取所有状态 |
| `/api/vnstat` | GET | vnStat GB 流量汇总 |
| `/api/vpn/clients` | GET | VPN 客户端 + 服务状态 |
| `/ws` | WebSocket | 实时数据推送（2秒间隔） |

---

## 安全建议

在线生产环境建议：

### 1. Nginx 反代 + HTTPS

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 2. IP 白名单

```bash
iptables -I INPUT -p tcp --dport 8088 -s 你的IP -j ACCEPT
iptables -A INPUT -p tcp --dport 8088 -j DROP
```

### 3. Token 鉴权

```ini
Environment=AUTH_TOKEN=your_secret_token
```

访问时带参数：`http://IP:8088?token=your_secret_token`

---

## 隐私说明

本项目涉及网络流量和客户端 IP 数据的采集，请务必了解以下信息：

### 数据流向

| 数据项 | 采集位置 | 是否离开服务器 |
|--------|----------|---------------|
| 系统资源（CPU/内存/磁盘） | 仅服务器本地 | ❌ 不离开 |
| 网卡流量统计 | 仅服务器本地（/proc/net/dev） | ❌ 不离开 |
| vnStat 流量统计 | 仅服务器本地 | ❌ 不离开 |
| VPN 端口速率 | iptables 本地计数器 | ❌ 不离开 |
| **客户端 IP 地理位置** | **ip-api.com 第三方 API** | ⚠️ 会发送客户端 IP |

### 关于 IP 地理位置查询

面板通过 [ip-api.com](https://ip-api.com/) 的免费 API 查询客户端 IP 的国家/城市/ISP 信息。这意味着：

- **每个活跃 VPN 客户端的公网 IP 会被发送到 ip-api.com**
- IP 查询结果会在服务器本地缓存 1 小时
- 内网 IP（10.x、192.168.x 等）不会发送到外部

### 禁用 IP 地理位置查询

如果你不希望客户端 IP 离开服务器，在 `server.py` 中的 `get_ip_geo()` 函数直接返回空即可：

```python
def get_ip_geo(ip: str) -> dict:
    # 禁用外部查询，仅区分本地/非本地
    if ip.startswith(("10.", "172.16.", "192.168.", "127.")):
        return {"country": "Local", "city": "", "isp": "", "org": ""}
    return {"country": "Remote", "city": "", "isp": "", "org": ""}
```

或者自行部署本地 GeoIP 数据库（如 MaxMind GeoLite2）替代 ip-api.com。

---

## 自定义配置路径

如果你的 VPN 配置文件不在默认路径，可通过环境变量指定：

```bash
# sing-box 非标准路径
Environment=SINGBOX_CONFIG=/home/user/sing-box/config.json

# WireGuard 非标准网卡名
Environment=WG_INTERFACE=custom-wg0

# OpenVPN 状态日志
Environment=OVPN_STATUS_LOG=/custom/path/openvpn-status.log
```

---

## 依赖清单

| 包 | 最低版本 | 用途 |
|----|---------|------|
| python3 | 3.8 | 运行环境 |
| fastapi | 0.104 | Web 框架 |
| uvicorn | 0.24 | ASGI 服务器 |
| psutil | 5.9 | 系统资源监控 |
| websockets | 12.0 | WebSocket 支持 |
| vnstat | 2.6 | 流量统计 |
| iptables | - | 端口速率追踪 |

---

## 常见问题

**Q: 面板显示"未检测到 VPN 服务"？**

确认 VPN 服务的 systemd 单元名正确：
- sing-box: `systemctl is-active sing-box`
- WireGuard: `systemctl is-active wg-quick@wg0`
- OpenVPN: `systemctl is-active openvpn-server`

**Q: vnStat 显示"数据暂不可用"？**

确保 vnStat 已安装并运行：
```bash
systemctl enable --now vnstat
vnstat --json  # 确认有输出
```

**Q: 端口速率和连接数不显示？**

需要 root 权限运行（iptables 需要 root）：
```bash
sudo systemctl restart vpn-monitor
```

---

## 开源协议

[MIT License](LICENSE) — 自由使用、修改、分发。
