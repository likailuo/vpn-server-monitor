#!/bin/bash
# ============================================================
# VPN Server Monitor v1.0 — 一键部署脚本
# 支持 sing-box / WireGuard / OpenVPN
#
# 用法:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# 或一条命令（需自行托管脚本）:
#   bash <(curl -sL https://your-server.com/deploy.sh)
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

INSTALL_DIR="/opt/vpn-monitor"
PORT="${1:-8088}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════╗"
echo "║     VPN Server Monitor v1.0 部署工具     ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── 检查 root ──
if [ "$(id -u)" != "0" ]; then
    echo -e "${RED}[错误] 请用 root 运行: sudo bash deploy.sh${NC}"
    exit 1
fi

# ── 检测系统 ──
echo -e "${YELLOW}[1/6]${NC} 检测系统环境..."
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
else
    echo -e "${RED}[错误] 不支持的包管理器${NC}"
    exit 1
fi
echo "  系统: $(cat /etc/os-release | grep PRETTY_NAME | cut -d'"' -f2)"
echo "  包管理: $PKG_MGR"

# ── 安装依赖 ──
echo -e "${YELLOW}[2/6]${NC} 安装系统依赖 (python3, pip, vnstat)..."
if [ "$PKG_MGR" = "apt" ]; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip vnstat
elif [ "$PKG_MGR" = "yum" ]; then
    yum install -y python3 python3-pip vnstat
fi
systemctl enable vnstat --now 2>/dev/null || true
echo "  ✅ 完成"

# ── 安装 Python 包 ──
echo -e "${YELLOW}[3/6]${NC} 安装 Python 依赖..."
pip3 install fastapi uvicorn psutil websockets -q
echo "  ✅ 完成"

# ── 部署文件 ──
echo -e "${YELLOW}[4/6]${NC} 部署项目文件..."
mkdir -p "$INSTALL_DIR/static"
cp "$SCRIPT_DIR/server.py" "$INSTALL_DIR/server.py"
cp "$SCRIPT_DIR/static/index.html" "$INSTALL_DIR/static/index.html"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
chmod +x "$INSTALL_DIR/server.py"
echo "  ✅ 文件已部署到 $INSTALL_DIR"

# ── 创建 systemd 服务 ──
echo -e "${YELLOW}[5/6]${NC} 配置 systemd 服务..."
cat > /etc/systemd/system/vpn-monitor.service << EOF
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

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vpn-monitor
systemctl restart vpn-monitor
echo "  ✅ 服务已配置"

# ── 防火墙 ──
echo -e "${YELLOW}[6/6]${NC} 配置防火墙..."
if command -v ufw &>/dev/null; then
    ufw allow $PORT/tcp 2>/dev/null
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --add-port=$PORT/tcp --permanent 2>/dev/null
    firewall-cmd --reload 2>/dev/null
fi
iptables -I INPUT -p tcp --dport $PORT -j ACCEPT 2>/dev/null || true
echo "  ✅ 完成"

# ── 验证 ──
sleep 2
if curl -s http://localhost:$PORT/api/health | grep -q "ok"; then
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║          🎉 部署成功！                   ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    echo -e "  访问地址: ${CYAN}http://${SERVER_IP}:${PORT}${NC}"
    echo ""
    echo -e "  管理命令:"
    echo -e "    systemctl status vpn-monitor   # 查看状态"
    echo -e "    systemctl restart vpn-monitor  # 重启服务"
    echo -e "    journalctl -u vpn-monitor -f   # 查看日志"
    echo ""
else
    echo -e "${RED}[错误] 服务未正常启动，请检查: journalctl -u vpn-monitor -n 20${NC}"
    exit 1
fi
