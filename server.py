#!/usr/bin/env python3
"""
轻量级 VPN 服务器监控面板 - FastAPI 后端
支持 WireGuard / OpenVPN / sing-box，WebSocket 实时推送
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# ──────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────
VPN_TYPE = os.environ.get("VPN_TYPE", "auto")
WG_INTERFACE = os.environ.get("WG_INTERFACE", "wg0")
OVPN_STATUS_LOG = os.environ.get("OVPN_STATUS_LOG", "/var/log/openvpn/openvpn-status.log")
SINGBOX_CONFIG = os.environ.get("SINGBOX_CONFIG", "/etc/s-box/sb.json")
NIC_SPEED_INTERVAL = float(os.environ.get("NIC_SPEED_INTERVAL", "2.0"))
TOKEN = os.environ.get("AUTH_TOKEN", "")

app = FastAPI(title="VPN Monitor", version="2.0.0")

# ──────────────────────────────────────────────────────────
# IP 地理位置缓存
# ──────────────────────────────────────────────────────────
_geo_cache: dict[str, dict] = {}
GEO_CACHE_TTL = 3600  # 1小时


def get_ip_geo(ip: str) -> dict:
    """获取 IP 地理位置信息（带缓存），使用 ip-api.com 免费 API"""
    if ip in ("127.0.0.1", "::1", "localhost", ""):
        return {"country": "Local", "city": "", "isp": "", "org": ""}

    # 跳过内网 IP
    if ip.startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                       "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                       "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                       "172.30.", "172.31.", "192.168.", "198.18.", "198.19.")):
        return {"country": "Private", "city": "", "isp": "", "org": ""}

    now = time.time()
    if ip in _geo_cache:
        entry = _geo_cache[ip]
        if now - entry["_ts"] < GEO_CACHE_TTL:
            return entry

    try:
        import urllib.request
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=country,city,isp,org,countryCode"
        req = urllib.request.Request(url, headers={"User-Agent": "VPN-Monitor/2.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            data["_ts"] = now
            _geo_cache[ip] = data
            return data
    except Exception:
        return {"country": "Unknown", "city": "", "isp": "", "org": "", "countryCode": ""}


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────

def detect_vpn_type() -> str:
    """自动检测 VPN 类型"""
    if VPN_TYPE != "auto":
        return VPN_TYPE
    try:
        result = subprocess.run(["wg", "show"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return "wireguard"
    except FileNotFoundError:
        pass
    if os.path.exists(OVPN_STATUS_LOG):
        return "openvpn"
    try:
        r = subprocess.run(["systemctl", "is-active", "sing-box"], capture_output=True, text=True, timeout=3)
        if r.stdout.strip() == "active":
            return "singbox"
    except FileNotFoundError:
        pass
    for path in ["/etc/s-box/sing-box", "/usr/bin/sing-box", "/usr/local/bin/sing-box"]:
        if os.path.exists(path):
            return "singbox"
    return "unknown"


def get_cpu_temp() -> Optional[float]:
    """读取 CPU 温度"""
    thermal_paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/thermal/thermal_zone1/temp",
    ]
    for p in thermal_paths:
        try:
            with open(p) as f:
                val = int(f.read().strip())
                return val / 1000.0 if val > 1000 else float(val)
        except (FileNotFoundError, ValueError):
            continue
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for chip, entries in temps.items():
                for e in entries:
                    if e.label and ("core" in e.label.lower() or "cpu" in e.label.lower()):
                        return e.current
                return temps[chip][0].current if temps[chip] else None
    except Exception:
        pass
    return None


def get_nic_stats() -> dict:
    """解析 /proc/net/dev 获取所有网卡流量字节数"""
    stats = {}
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.strip().split()
                iface = parts[0].rstrip(":")
                stats[iface] = {"rx_bytes": int(parts[1]), "tx_bytes": int(parts[9])}
    except FileNotFoundError:
        pass
    return stats


# ──────────────────────────────────────────────────────────
# sing-box 解析
# ──────────────────────────────────────────────────────────

_sb_config_cache = None
_sb_config_cache_time = 0.0


def parse_singbox_config() -> list[dict]:
    """解析 sing-box 配置文件"""
    global _sb_config_cache, _sb_config_cache_time
    now = time.time()
    if _sb_config_cache and (now - _sb_config_cache_time) < 30:
        return _sb_config_cache

    protocols = []
    config_paths = [SINGBOX_CONFIG, "/etc/s-box/sb10.json", "/etc/s-box/sb11.json"]
    try:
        r = subprocess.run(
            ["systemctl", "show", "sing-box", "--property=ExecStart"],
            capture_output=True, text=True, timeout=3,
        )
        match = re.search(r'-c\s+(\S+)', r.stdout)
        if match and os.path.exists(match.group(1)):
            config_paths.insert(0, match.group(1))
    except Exception:
        pass

    for cfg_path in config_paths:
        if not os.path.exists(cfg_path):
            continue
        try:
            with open(cfg_path) as f:
                config = json.load(f)
            for inbound in config.get("inbounds", []):
                protocols.append({
                    "tag": inbound.get("tag", "unknown"),
                    "type": inbound.get("type", "unknown"),
                    "listen": inbound.get("listen", "::"),
                    "port": inbound.get("listen_port", 0),
                    "users": len(inbound.get("users", [])),
                    "transport": inbound.get("transport", {}).get("type", "tcp"),
                    "tls": inbound.get("tls", {}).get("enabled", False),
                    "reality": inbound.get("tls", {}).get("reality", {}).get("enabled", False),
                })
            if protocols:
                break
        except (json.JSONDecodeError, OSError):
            continue

    _sb_config_cache = protocols
    _sb_config_cache_time = now
    return protocols


# ── 端口流量追踪（iptables 计数器） ──

_port_bytes_last: dict = {}
_port_speeds: dict = {}
_port_bytes_time: float = 0.0


def setup_port_accounting():
    """为 sing-box 端口创建 iptables 计数规则（幂等）"""
    protocols = parse_singbox_config()
    for p in protocols:
        port = p["port"]
        proto = "tcp" if p["type"] in ("vmess", "vless", "anytls") else "udp"
        # 检查是否已有规则
        check = subprocess.run(
            ["iptables", "-L", "INPUT", "-n", "-v", "-x"],
            capture_output=True, text=True, timeout=5,
        )
        if f"dpt:{port}" not in check.stdout:
            subprocess.run(
                ["iptables", "-I", "INPUT", "1", "-p", proto, "--dport", str(port), "-j", "ACCEPT"],
                capture_output=True, timeout=5,
            )


def sample_port_bytes() -> dict[int, dict]:
    """采样 sit-box 端口的 iptables 累计字节数，计算实时速率"""
    global _port_bytes_last, _port_speeds, _port_bytes_time

    protocols = parse_singbox_config()
    result = {}
    now = time.time()

    try:
        r = subprocess.run(
            ["iptables", "-L", "INPUT", "-n", "-v", "-x"],
            capture_output=True, text=True, timeout=5,
        )
        lines = r.stdout.split("\n")
        # 解析 iptables 输出: pkts bytes target prot opt in out source destination
        port_bytes = {}
        for line in lines:
            for p in protocols:
                if f"dpt:{p['port']}" in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            port_bytes[p["port"]] = int(parts[1])  # bytes
                        except ValueError:
                            pass
                    break

        # 计算速率
        elapsed = now - _port_bytes_time if _port_bytes_time > 0 else NIC_SPEED_INTERVAL
        for p in protocols:
            port = p["port"]
            current = port_bytes.get(port, 0)
            last = _port_bytes_last.get(port, 0)
            speed_mbps = round((current - last) * 8 / elapsed / 1_000_000, 2) if last > 0 else 0
            p["speed_mbps"] = max(0, speed_mbps)
            _port_bytes_last[port] = current

        _port_bytes_time = now
    except Exception:
        for p in protocols:
            p["speed_mbps"] = 0

    return protocols


def parse_singbox_connections() -> list[dict]:
    """获取 sing-box 活跃连接：TCP ss + UDP journal 日志双源解析"""
    protocols = parse_singbox_config()
    if not protocols:
        return []

    port_map = {p["port"]: p for p in protocols}
    tag_map = {p["tag"]: p for p in protocols}  # tag -> protocol
    seen = set()
    clients = []

    # ── 方法1: ss 获取 TCP 已建立连接 ──
    try:
        result = subprocess.run(
            ["ss", "-tn", "state", "established"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.strip().split()
            if len(parts) < 6:
                continue
            local_addr, peer_addr = parts[4], parts[5]
            m = re.search(r':(\d+)$', local_addr) if local_addr else None
            if not m:
                continue
            local_port = int(m.group(1))
            if local_port not in port_map:
                continue
            proto = port_map[local_port]
            peer_ip = peer_addr.rsplit(":", 1)[0] if ":" in peer_addr else peer_addr
            if peer_ip in ("127.0.0.1", "::1"):
                continue
            # 同 IP 只保留最新（切节点后旧的自动消失）
            key = peer_ip
            if key in seen:
                continue
            seen.add(key)
            geo = get_ip_geo(peer_ip)
            clients.append({
                "ip": peer_ip, "port": local_port,
                "protocol": proto["type"], "tag": proto["tag"],
                "country": geo.get("country", ""), "countryCode": geo.get("countryCode", ""),
                "city": geo.get("city", ""), "isp": geo.get("isp", ""),
                "org": geo.get("org", ""), "since": int(time.time()),
            })
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    # ── 方法2: journalctl 解析 UDP 连接 (Hysteria2/TUIC/VLESS) ──
    # 日志格式: "inbound/tuic[tuic5-sb]: inbound connection from 1.2.3.4:12345"
    try:
        result = subprocess.run(
            ["journalctl", "-u", "sing-box", "--no-pager", "--since", "1 min ago"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            m = re.search(r'inbound/(\w+)\[.*?\]: inbound connection from ([^:]+):(\d+)', line)
            if not m:
                continue
            proto_type = m.group(1)
            inbound_tag = m.group(1)
            peer_ip = m.group(2)
            # 找到对应端口
            local_port = 0
            for p in protocols:
                if p["type"] == proto_type or proto_type in p.get("tag", ""):
                    local_port = p["port"]
                    break
            if peer_ip in ("127.0.0.1", "::1"):
                continue
            # 同 IP 只保留最新
            key = peer_ip
            if key in seen:
                continue
            seen.add(key)
            geo = get_ip_geo(peer_ip)
            clients.append({
                "ip": peer_ip, "port": local_port or 0,
                "protocol": proto_type, "tag": "",
                "country": geo.get("country", ""), "countryCode": geo.get("countryCode", ""),
                "city": geo.get("city", ""), "isp": geo.get("isp", ""),
                "org": geo.get("org", ""), "since": int(time.time()),
            })
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    return clients


# ──────────────────────────────────────────────────────────
# WireGuard / OpenVPN 解析（保留兼容）
# ──────────────────────────────────────────────────────────

def parse_wireguard_clients() -> list[dict]:
    clients = []
    try:
        result = subprocess.run(
            ["wg", "show", WG_INTERFACE, "dump"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return clients
        lines = result.stdout.strip().split("\n")
        for line in lines[1:]:
            cols = line.split("\t")
            if len(cols) >= 8:
                endpoint = cols[2]
                if endpoint and endpoint != "(none)":
                    try:
                        handshake_ts = int(cols[4])
                        if int(time.time()) - handshake_ts <= 180:
                            peer_ip = endpoint.split(":")[0] if ":" in endpoint else endpoint
                            geo = get_ip_geo(peer_ip)
                            clients.append({
                                "ip": peer_ip,
                                "port": 0,
                                "protocol": "wireguard",
                                "tag": "wg",
                                "country": geo.get("country", ""),
                                "countryCode": geo.get("countryCode", ""),
                                "city": geo.get("city", ""),
                                "isp": geo.get("isp", ""),
                                "since": handshake_ts,
                                "virtual_ip": cols[3].split(",")[0].strip() if cols[3] else "",
                                "rx_bytes": int(cols[6]) if len(cols) > 6 and cols[6].isdigit() else 0,
                                "tx_bytes": int(cols[5]) if len(cols) > 5 and cols[5].isdigit() else 0,
                            })
                    except (ValueError, IndexError):
                        pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return clients


def parse_openvpn_clients() -> list[dict]:
    clients = []
    if not os.path.exists(OVPN_STATUS_LOG):
        return clients
    try:
        with open(OVPN_STATUS_LOG) as f:
            content = f.read()
    except PermissionError:
        return clients

    common_name_map = {}
    in_client_list = False
    in_routing_table = False

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("Common Name"):
            in_client_list = True
            in_routing_table = False
            continue
        if line.startswith("ROUTING TABLE"):
            in_routing_table = True
            in_client_list = False
            continue
        if line.startswith("GLOBAL STATS"):
            break

        if in_client_list:
            parts = line.split(",")
            if len(parts) >= 5:
                common_name_map[parts[0]] = {
                    "user": parts[0],
                    "ip": parts[1].split(":")[0] if ":" in parts[1] else parts[1],
                    "rx_bytes": int(parts[2]) if parts[2].isdigit() else 0,
                    "tx_bytes": int(parts[3]) if parts[3].isdigit() else 0,
                    "since": int(time.time()),
                }
        if in_routing_table:
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip() in common_name_map:
                common_name_map[parts[1].strip()]["virtual_ip"] = parts[0].strip()

    for c in common_name_map.values():
        geo = get_ip_geo(c["ip"])
        c["country"] = geo.get("country", "")
        c["countryCode"] = geo.get("countryCode", "")
        c["city"] = geo.get("city", "")
        c["isp"] = geo.get("isp", "")
        c["protocol"] = "openvpn"
        c["port"] = 1194
    return list(common_name_map.values())


def get_vpn_clients() -> list[dict]:
    vpn_type = detect_vpn_type()
    if vpn_type == "wireguard":
        return parse_wireguard_clients()
    elif vpn_type == "openvpn":
        return parse_openvpn_clients()
    elif vpn_type == "singbox":
        return parse_singbox_connections()
    return []


def get_vpn_service_status() -> dict:
    """获取 VPN 服务状态 + 各端口实时连接数"""
    vpn_type = detect_vpn_type()
    service_map = {
        "wireguard": ["wg-quick@wg0"],
        "openvpn": ["openvpn-server"],
        "singbox": ["sing-box"],
        "unknown": [],
    }
    candidates = service_map.get(vpn_type, [])

    result = {"type": vpn_type, "services": [], "port_stats": []}
    for svc in candidates:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=3,
            )
            status = r.stdout.strip()
            r2 = subprocess.run(
                ["systemctl", "show", svc, "--property=ActiveEnterTimestamp,SubState,ExecMainPID"],
                capture_output=True, text=True, timeout=3,
            )
            extra = {}
            for line in r2.stdout.strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    extra[k] = v

            # 计算运行时长
            active_since = extra.get("ActiveEnterTimestamp", "")
            uptime_seconds = 0
            if active_since:
                try:
                    # 格式: "Thu 2026-06-25 01:00:02 BST"
                    parts = active_since.split()
                    dt_str = " ".join(parts[1:4]) if len(parts) >= 4 else active_since
                    active_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    uptime_seconds = int(time.time() - active_dt.timestamp())
                except (ValueError, IndexError):
                    pass

            result["services"].append({
                "name": svc,
                "status": status,
                "uptime_seconds": uptime_seconds,
                "pid": extra.get("ExecMainPID", ""),
            })
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 各端口实时连接数 + 实时速率
    if vpn_type == "singbox":
        protocols = sample_port_bytes()  # 采样 iptables 速率
        clients = parse_singbox_connections()
        port_counts = defaultdict(int)
        for c in clients:
            port_counts[c.get("port", 0)] += 1
        for p in protocols:
            p["connections"] = port_counts.get(p["port"], 0)
        result["port_stats"] = protocols

    return result


# ──────────────────────────────────────────────────────────
# vnStat 流量统计（汇总所有网卡，单位 GB）
# ──────────────────────────────────────────────────────────

def get_vnstat_summary() -> dict:
    """获取 vnStat 流量汇总：所有网卡合并，按小时/天/周/月聚合，单位 GB"""
    try:
        result = subprocess.run(
            ["vnstat", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise ValueError("vnstat failed")
        raw = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return {"error": "vnStat 数据暂不可用", "hourly": [], "daily": [], "weekly": [], "monthly": []}

    # 汇总所有网卡（vnStat 2.x 使用单数键: hour/day/month，无 weeks）
    hourly_agg = defaultdict(lambda: {"rx": 0, "tx": 0})
    daily_agg = defaultdict(lambda: {"rx": 0, "tx": 0})
    monthly_agg = defaultdict(lambda: {"rx": 0, "tx": 0})

    for iface in raw.get("interfaces", []):
        traffic = iface.get("traffic", {})

        for e in traffic.get("hour", [])[-24:]:
            ts = e.get("date", {})
            key = f"{ts.get('year',0)}-{ts.get('month',0):02d}-{ts.get('day',0):02d} {ts.get('hour',0):02d}:00"
            hourly_agg[key]["rx"] += e.get("rx", 0)
            hourly_agg[key]["tx"] += e.get("tx", 0)

        for e in traffic.get("day", [])[-30:]:
            ts = e.get("date", {})
            key = f"{ts.get('year',0)}-{ts.get('month',0):02d}-{ts.get('day',0):02d}"
            daily_agg[key]["rx"] += e.get("rx", 0)
            daily_agg[key]["tx"] += e.get("tx", 0)

        for e in traffic.get("month", [])[-12:]:
            ts = e.get("date", {})
            key = f"{ts.get('year',0)}-{ts.get('month',0):02d}"
            monthly_agg[key]["rx"] += e.get("rx", 0)
            monthly_agg[key]["tx"] += e.get("tx", 0)

    # 周数据：从日数据按 ISO 周聚合
    weekly_agg = defaultdict(lambda: {"rx": 0, "tx": 0})
    for day_key, vals in daily_agg.items():
        try:
            parts = day_key.split("-")
            dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            iso = dt.isocalendar()
            weekly_agg[f"{iso[0]}-W{iso[1]:02d}"]["rx"] += vals["rx"]
            weekly_agg[f"{iso[0]}-W{iso[1]:02d}"]["tx"] += vals["tx"]
        except (ValueError, IndexError):
            pass

    def fmt(agg: dict) -> list[dict]:
        return [{"time": k, "rx_gb": round(v["rx"]/(1024**3),3),
                 "tx_gb": round(v["tx"]/(1024**3),3),
                 "total_gb": round((v["rx"]+v["tx"])/(1024**3),3)}
                for k, v in sorted(agg.items())]

    return {
        "hourly": fmt(hourly_agg),
        "daily": fmt(daily_agg),
        "weekly": fmt(weekly_agg)[-8:],
        "monthly": fmt(monthly_agg),
    }


# ──────────────────────────────────────────────────────────
# 综合数据采集
# ──────────────────────────────────────────────────────────

_last_nic_stats: dict = {}
_last_client_stats: dict = {}
_last_sample_time: float = 0.0


async def collect_all_stats() -> dict:
    global _last_nic_stats, _last_client_stats, _last_sample_time

    now = time.time()
    elapsed = now - _last_sample_time if _last_sample_time > 0 else NIC_SPEED_INTERVAL

    # CPU
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    cpu_temp = get_cpu_temp()
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count()

    # Memory
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "loop" in part.device or "snap" in part.device:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "total_gb": round(usage.total / (1024**3), 1),
                "used_gb": round(usage.used / (1024**3), 1),
                "free_gb": round(usage.free / (1024**3), 1),
                "percent": usage.percent,
            })
        except PermissionError:
            continue

    # Disk I/O
    disk_io = psutil.disk_io_counters(perdisk=True) if psutil.disk_io_counters() else {}
    disk_io_speed = {}
    for name, io in disk_io.items():
        last = _last_nic_stats.get(f"disk_{name}", {})
        if last:
            disk_io_speed[name] = {
                "read_mbps": round((io.read_bytes - last.get("read_bytes", 0)) / elapsed / (1024 * 1024), 2),
                "write_mbps": round((io.write_bytes - last.get("write_bytes", 0)) / elapsed / (1024 * 1024), 2),
            }
        _last_nic_stats[f"disk_{name}"] = {"read_bytes": io.read_bytes, "write_bytes": io.write_bytes}

    # Load
    load_avg = os.getloadavg()

    # Network
    nic_stats = get_nic_stats()
    nic_speeds = {}
    for iface, stats in nic_stats.items():
        if iface in _last_nic_stats:
            last = _last_nic_stats[iface]
            nic_speeds[iface] = {
                "rx_mbps": round((stats["rx_bytes"] - last["rx_bytes"]) * 8 / elapsed / 1_000_000, 2),
                "tx_mbps": round((stats["tx_bytes"] - last["tx_bytes"]) * 8 / elapsed / 1_000_000, 2),
                "rx_total_gb": round(stats["rx_bytes"] / (1024**3), 2),
                "tx_total_gb": round(stats["tx_bytes"] / (1024**3), 2),
            }
        _last_nic_stats[iface] = stats

    # VPN
    vpn_clients = get_vpn_clients()
    vpn_service = get_vpn_service_status()

    _last_sample_time = now

    return {
        "timestamp": now,
        "cpu": {
            "percent": cpu_percent,
            "per_core": cpu_per_core,
            "temp": cpu_temp,
            "freq_current": cpu_freq.current if cpu_freq else None,
            "freq_max": cpu_freq.max if cpu_freq else None,
            "count": cpu_count,
        },
        "memory": {
            "total_gb": round(mem.total / (1024**3), 1),
            "available_gb": round(mem.available / (1024**3), 1),
            "used_gb": round(mem.used / (1024**3), 1),
            "percent": mem.percent,
            "swap_total_gb": round(swap.total / (1024**3), 1),
            "swap_used_gb": round(swap.used / (1024**3), 1),
            "swap_percent": swap.percent,
        },
        "disks": disks,
        "disk_io": disk_io_speed,
        "load": {"load1": load_avg[0], "load5": load_avg[1], "load15": load_avg[2]},
        "network": nic_speeds,
        "vpn": {
            "type": vpn_service["type"],
            "services": vpn_service["services"],
            "port_stats": vpn_service.get("port_stats", []),
            "clients": vpn_clients,
        },
    }


# ──────────────────────────────────────────────────────────
# 历史数据
# ──────────────────────────────────────────────────────────
MAX_HISTORY = 120

history_data = {
    "cpu": [],
    "memory_percent": [],
    "load": [],
    "network": defaultdict(list),
}


def update_history(stats: dict):
    now = stats["timestamp"]
    history_data["cpu"].append({"time": now, "value": stats["cpu"]["percent"]})
    history_data["memory_percent"].append({"time": now, "value": stats["memory"]["percent"]})
    history_data["load"].append({
        "time": now,
        "load1": stats["load"]["load1"],
        "load5": stats["load"]["load5"],
        "load15": stats["load"]["load15"],
    })
    for iface, data in stats["network"].items():
        history_data["network"][iface].append({
            "time": now, "rx_mbps": data["rx_mbps"], "tx_mbps": data["tx_mbps"],
        })
    for key in ["cpu", "memory_percent", "load"]:
        if len(history_data[key]) > MAX_HISTORY:
            history_data[key] = history_data[key][-MAX_HISTORY:]
    for iface in history_data["network"]:
        if len(history_data["network"][iface]) > MAX_HISTORY:
            history_data["network"][iface] = history_data["network"][iface][-MAX_HISTORY:]


# ──────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active_connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self):
        return len(self.active_connections)


manager = ConnectionManager()


async def background_pusher():
    while True:
        if manager.count > 0:
            try:
                stats = await collect_all_stats()
                update_history(stats)
                stats["history"] = {
                    "cpu": history_data["cpu"],
                    "memory": history_data["memory_percent"],
                    "load": history_data["load"],
                    "network": {k: list(v) for k, v in history_data["network"].items()},
                }
                await manager.broadcast(stats)
            except Exception as e:
                print(f"[Pusher Error] {e}")
        await asyncio.sleep(NIC_SPEED_INTERVAL)


@app.on_event("startup")
async def startup():
    # 初始化 iptables 端口流量统计
    try:
        setup_port_accounting()
    except Exception as e:
        print(f"[Setup] iptables accounting skipped: {e}")
    asyncio.create_task(background_pusher())


# ──────────────────────────────────────────────────────────
# REST API
# ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>")


@app.get("/api/status")
async def api_status():
    stats = await collect_all_stats()
    stats["vpn_type"] = detect_vpn_type()
    return JSONResponse(stats)


@app.get("/api/vnstat")
async def api_vnstat():
    """REST API: vnStat 汇总流量（GB）"""
    return JSONResponse(get_vnstat_summary())


@app.get("/api/vpn/clients")
async def api_vpn_clients():
    return JSONResponse({
        "type": detect_vpn_type(),
        "clients": get_vpn_clients(),
        "services": get_vpn_service_status(),
    })


@app.get("/api/health")
async def api_health():
    return JSONResponse({"status": "ok", "vpn_type": detect_vpn_type(), "ws_clients": manager.count})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(None)):
    if TOKEN and token != TOKEN:
        await ws.close(code=4001, reason="Unauthorized")
        return
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
            elif data == "vnstat":
                await ws.send_json({
                    "type": "vnstat",
                    "data": get_vnstat_summary(),
                })
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ──────────────────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"VPN Monitor starting on http://{host}:{port}")
    print(f"VPN Type: {detect_vpn_type()}")
    uvicorn.run(app, host=host, port=port, log_level="info")
