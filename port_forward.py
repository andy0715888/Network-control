#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iptables 端口映射管理模块

使用 nat 表的自定义链 PORT_FORWARD 实现端口转发：
- PREROUTING -> PORT_FORWARD 链
- 白名单端口在链内 RETURN（保留本地，不转发）
- 范围内其余端口 DNAT 到目标服务器
- POSTROUTING 对目标 IP 做 MASQUERADE（保证回程流量正常）
"""

import subprocess
import re

CHAIN_NAME = "PORT_FORWARD"


def run_cmd(cmd):
    """执行 shell 命令，返回 (成功与否, 输出)"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=60
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as e:
        return False, str(e)


def enable_ip_forwarding():
    """开启内核 IPv4 转发并持久化"""
    run_cmd("sysctl -w net.ipv4.ip_forward=1")
    # 持久化到 sysctl.conf
    run_cmd(
        "grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf && "
        "sed -i 's/^net.ipv4.ip_forward.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf || "
        "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf"
    )
    run_cmd("sysctl -p /etc/sysctl.conf >/dev/null 2>&1 || true")


def ensure_chain():
    """确保自定义链存在，并挂到 PREROUTING"""
    run_cmd(f"iptables -t nat -N {CHAIN_NAME} 2>/dev/null || true")
    run_cmd(
        f"iptables -t nat -C PREROUTING -j {CHAIN_NAME} 2>/dev/null || "
        f"iptables -t nat -A PREROUTING -j {CHAIN_NAME}"
    )


def clear_rules(old_target_ip=None):
    """清空转发链及旧目标的 MASQUERADE 规则"""
    run_cmd(f"iptables -t nat -F {CHAIN_NAME} 2>/dev/null || true")
    if old_target_ip:
        run_cmd(
            f"iptables -t nat -C POSTROUTING -d {old_target_ip} -j MASQUERADE 2>/dev/null && "
            f"iptables -t nat -D POSTROUTING -d {old_target_ip} -j MASQUERADE || true"
        )


def _valid_ip(ip):
    return bool(re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip or ""))


def _valid_port(p):
    try:
        p = int(p)
        return 1 <= p <= 65535
    except (TypeError, ValueError):
        return False


def apply_config(target_ip, port_start, port_end, whitelist, protocols="tcp,udp", panel_port=None):
    """应用端口映射配置

    参数:
        target_ip: 目标服务器 IP
        port_start/port_end: 转发端口范围
        whitelist: list[int] 白名单端口（不转发）
        protocols: "tcp" / "udp" / "tcp,udp"
        panel_port: 面板自身端口，自动加入白名单避免被转发导致失联
    返回: (成功与否, 消息)
    """
    if not _valid_ip(target_ip):
        return False, "目标 IP 格式不正确"
    if not (_valid_port(port_start) and _valid_port(port_end)):
        return False, "端口范围不合法"
    if int(port_start) > int(port_end):
        return False, "起始端口不能大于结束端口"

    protos = []
    if "tcp" in protocols:
        protos.append("tcp")
    if "udp" in protocols:
        protos.append("udp")
    if not protos:
        return False, "未选择协议"

    # 面板端口自动加入白名单
    wl_set = set()
    for p in whitelist:
        if _valid_port(p):
            wl_set.add(int(p))
    if panel_port and _valid_port(panel_port):
        wl_set.add(int(panel_port))

    enable_ip_forwarding()
    ensure_chain()

    # 清空链
    run_cmd(f"iptables -t nat -F {CHAIN_NAME} 2>/dev/null || true")
    # 删除旧目标的 MASQUERADE（若目标变更）
    run_cmd(
        f"iptables -t nat -D POSTROUTING -d {target_ip} -j MASQUERADE 2>/dev/null || true"
    )

    # 白名单优先 RETURN
    for port in sorted(wl_set):
        for proto in protos:
            run_cmd(
                f"iptables -t nat -A {CHAIN_NAME} -p {proto} --dport {port} -j RETURN"
            )

    # 范围 DNAT
    for proto in protos:
        ok, msg = run_cmd(
            f"iptables -t nat -A {CHAIN_NAME} -p {proto} "
            f"--dport {int(port_start)}:{int(port_end)} "
            f"-j DNAT --to-destination {target_ip}"
        )
        if not ok:
            return False, f"添加 {proto} DNAT 规则失败: {msg}"

    # MASQUERADE 回程
    ok, msg = run_cmd(
        f"iptables -t nat -A POSTROUTING -d {target_ip} -j MASQUERADE"
    )
    if not ok:
        return False, f"添加 MASQUERADE 失败: {msg}"

    return True, f"已应用: {target_ip} 端口 {port_start}-{port_end} ({','.join(protos)})"


def get_status():
    """获取当前 nat 表中相关规则"""
    _, nat_all = run_cmd("iptables -t nat -L -n -v --line-numbers")
    _, chain_rules = run_cmd(f"iptables -t nat -L {CHAIN_NAME} -n -v --line-numbers 2>/dev/null")
    return {
        "nat_table": nat_all,
        "forward_chain": chain_rules,
    }


def has_iptables():
    ok, _ = run_cmd("iptables --version")
    return ok
