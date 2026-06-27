#!/bin/bash
# ============================================================
# VPN Server Monitor v1.0 — 远程一键部署脚本
# 支持 sing-box / WireGuard / OpenVPN
#
# 用法（SSH 登录服务器后一行命令）:
#   bash <(curl -sL https://raw.githubusercontent.com/likailuo/vpn-server-monitor/main/remote-deploy.sh)
#
# 指定端口:
#   bash <(curl -sL https://raw.githubusercontent.com/likailuo/vpn-server-monitor/main/remote-deploy.sh) 9090
#
# 更新已有安装:
#   bash <(curl -sL https://raw.githubusercontent.com/likailuo/vpn-server-monitor/main/remote-deploy.sh) --update
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

INSTALL_DIR="/opt/vpn-monitor"
REPO_URL="https://github.com/likailuo/vpn-server-monitor.git"
PORT="8088"
UPDATE_MODE=false

# ── 解析参数 ──
for arg in "$@"; do
    case "$arg" in
        --update|-u) UPDATE_MODE=true ;;
        *) PORT="$arg" ;;
    esac
done

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════╗"
echo "║       VPN Server Monitor v1.0  远程部署工具       ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

# ── 检查 root ──
if [ "$(id -u)" != "0" ]; then
    echo -e "${RED}[错误] 请用 root 运行:${NC}"
    echo -e "  sudo bash <(curl -sL https://raw.githubusercontent.com/likailuo/vpn-server-monitor/main/remote-deploy.sh)"
    exit 1
fi

# ── 检测系统 ──
echo -e "${YELLOW}[1/7]${NC} 检测系统环境..."
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
else
    echo -e "${RED}[错误] 不支持的包管理器（需要 apt / yum / dnf）${NC}"
    exit 1
fi
echo "  系统: $(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'"' -f2 || uname -a)"
echo "  包管理: $PKG_MGR"
echo "  安装目录: $INSTALL_DIR"
echo "  端口: $PORT"

# ── 拉取代码 ──
echo -e "${YELLOW}[2/7]${NC} 拉取最新代码..."
if [ "$UPDATE_MODE" = true ] && [ -d "$INSTALL_DIR/.git" ]; then
    echo "  检测到已有安装，拉取更新..."
    cd "$INSTALL_DIR"
    git pull origin main
    echo "  ✅ 已更新到最新版本"
elif [ -d "$INSTALL_DIR/.git" ]; then
    echo "  检测到已有安装，拉取更新..."
    cd "$INSTALL_DIR"
    git pull origin main
    echo "  ✅ 已更新到最新版本"
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "  备份旧目录到 $INSTALL_DIR.bak.$(date +%Y%m%d%H%M%S)"
        mv "$INSTALL_DIR" "$INSTALL_DIR.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || rm -rf "$INSTALL_DIR"
    fi
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
    echo "  ✅ 代码已克隆到 $INSTALL_DIR"
fi

# ── 安装系统依赖 ──
echo -e "${YELLOW}[3/7]${NC} 安装系统依赖 (python3, pip, vnstat, git)..."
if [ "$PKG_MGR" = "apt" ]; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip vnstat git curl
elif [ "$PKG_MGR" = "yum" ]; then
    yum install -y python3 python3-pip vnstat git curl
elif [ "$PKG_MGR" = "dnf" ]; then
    dnf install -y python3 python3-pip vnstat git curl
fi
systemctl enable vnstat --now 2>/dev/null || true
echo "  ✅ 完成"

# ── 安装 Python 包 ──
echo -e "${YELLOW}[4/7]${NC} 安装 Python 依赖..."
pip3 install fastapi uvicorn psutil websockets -q 2>/dev/null || \
pip3 install fastapi uvicorn psutil websockets --break-system-packages -q 2>/dev/null || true
echo "  ✅ 完成"

# ── 部署文件 ──
echo -e "${YELLOW}[5/7]${NC} 部署项目文件..."
chmod +x "$INSTALL_DIR/server.py"
echo "  ✅ 完成"

# ── 创建/更新 systemd 服务 ──
echo -e "${YELLOW}[6/7]${NC} 配置 systemd 服务..."
cat > /etc/systemd/system/vpn-monitor.service << SYSTEMDEOF
[Unit]
Description=VPN Server Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/server.py
Restart=always
RestartSec=3
Environment=PORT=$PORT
Environment=HOST=0.0.0.0

# 可选：取消注释以下行启用 Token 鉴权
# Environment=AUTH_TOKEN=your_secret_token

# 可选：非标准 VPN 配置路径
# Environment=SINGBOX_CONFIG=/etc/s-box/sb.json
# Environment=WG_INTERFACE=wg0
# Environment=OVPN_STATUS_LOG=/var/log/openvpn/openvpn-status.log

[Install]
WantedBy=multi-user.target
SYSTEMDEOF

systemctl daemon-reload
systemctl enable vpn-monitor
systemctl restart vpn-monitor
echo "  ✅ 服务已配置"

# ── 防火墙 ──
echo -e "${YELLOW}[7/7]${NC} 配置防火墙..."
if command -v ufw &>/dev/null; then
    ufw allow $PORT/tcp 2>/dev/null || true
    echo "  ufw 规则已添加"
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --add-port=$PORT/tcp --permanent 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    echo "  firewalld 规则已添加"
fi
iptables -I INPUT -p tcp --dport $PORT -j ACCEPT 2>/dev/null || true
echo "  ✅ 完成"

# ── 验证 ──
sleep 2
echo ""
if systemctl is-active vpn-monitor --quiet; then
    echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║              🎉 部署成功！                       ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s ip.sb 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
    echo -e "  访问地址: ${CYAN}http://${SERVER_IP}:${PORT}${NC}"
    echo ""
    echo -e "  管理命令:"
    echo -e "    systemctl status vpn-monitor     # 查看状态"
    echo -e "    systemctl restart vpn-monitor    # 重启服务"
    echo -e "    systemctl stop vpn-monitor       # 停止服务"
    echo -e "    journalctl -u vpn-monitor -f     # 实时日志"
    echo ""
    echo -e "  更新命令:"
    echo -e "    ${CYAN}bash <(curl -sL https://raw.githubusercontent.com/likailuo/vpn-server-monitor/main/remote-deploy.sh) --update${NC}"
    echo ""
else
    echo -e "${RED}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║           ✗ 服务启动失败，请检查日志              ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  查看日志: ${YELLOW}journalctl -u vpn-monitor -n 30${NC}"
    echo -e "  查看状态: ${YELLOW}systemctl status vpn-monitor${NC}"
    exit 1
fi
