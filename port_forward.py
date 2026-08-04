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
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=60
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as e:
        return False, str(e)


def enable_ip_forwarding():
    run_cmd("sysctl -w net.ipv4.ip_forward=1")
    run_cmd(
        "grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf && "
        "sed -i 's/^net.ipv4.ip_forward.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf || "
        "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf"
    )
    run_cmd("sysctl -p /etc/sysctl.conf >/dev/null 2>&1 || true")


def ensure_chain():
    run_cmd(f"iptables -t nat -N {CHAIN_NAME} 2>/dev/null || true")
    run_cmd(
        f"iptables -t nat -C PREROUTING -j {CHAIN_NAME} 2>/dev/null || "
        f"iptables -t nat -A PREROUTING -j {CHAIN_NAME}"
    )


def clear_all_rules():
    """清空所有转发规则（链 + POSTROUTING MASQUERADE）"""
    run_cmd(f"iptables -t nat -F {CHAIN_NAME} 2>/dev/null || true")
    # 清除所有 PORT_FORWARD 链相关的 MASQUERADE 规则
    _, post = run_cmd("iptables -t nat -L POSTROUTING -n 2>/dev/null")
    for line in post.split("\n"):
        if "MASQUERADE" in line and "PORT_FORWARD" not in line:
            # 获取 -d 参数
            m = re.search(r"d\.?(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                ip = m.group(1)
                run_cmd(
                    f"iptables -t nat -D POSTROUTING -d {ip} -j MASQUERADE 2>/dev/null || true"
                )


def _valid_ip(ip):
    return bool(re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip or ""))


def _valid_port(p):
    try:
        p = int(p)
        return 1 <= p <= 65535
    except (TypeError, ValueError):
        return False


def apply_multi_rules(rules, whitelist, panel_port=None):
    """批量应用多组端口映射规则

    参数:
        rules: list[dict]  每项 {target_ip, port_start, port_end, protocols}
        whitelist: list[int]  白名单端口
        panel_port: 面板端口（自动加入白名单）
    返回: (成功与否, 消息)
    """
    if not rules:
        return False, "没有要应用的规则"

    # 校验所有规则
    for i, r in enumerate(rules):
        if not _valid_ip(r["target_ip"]):
            return False, f"规则{i+1}: 目标 IP 格式不正确"
        if not (_valid_port(r["port_start"]) and _valid_port(r["port_end"])):
            return False, f"规则{i+1}: 端口范围不合法"
        if int(r["port_start"]) > int(r["port_end"]):
            return False, f"规则{i+1}: 起始端口大于结束端口"

    # 收集所有涉及的目标 IP（用于 MASQUERADE）
    target_ips = list({r["target_ip"] for r in rules})

    # 规范化协议
    for r in rules:
        protos = []
        for p in (r.get("protocols", "tcp,udp") or "").split(","):
            p = p.strip().lower()
            if p in ("tcp", "udp"):
                protos.append(p)
        r["_protos"] = protos or ["tcp", "udp"]

    # 白名单
    wl_set = set()
    for p in whitelist:
        if _valid_port(p):
            wl_set.add(int(p))
    if panel_port and _valid_port(panel_port):
        wl_set.add(int(panel_port))

    enable_ip_forwarding()
    ensure_chain()

    # 清空旧链
    run_cmd(f"iptables -t nat -F {CHAIN_NAME} 2>/dev/null || true")

    # 清除旧 MASQUERADE
    for ip in target_ips:
        run_cmd(
            f"iptables -t nat -D POSTROUTING -d {ip} -j MASQUERADE 2>/dev/null || true"
        )

    # 白名单优先 RETURN
    for port in sorted(wl_set):
        for proto in ("tcp", "udp"):
            run_cmd(
                f"iptables -t nat -A {CHAIN_NAME} -p {proto} --dport {port} -j RETURN"
            )

    # 为每组规则添加 DNAT
    for r in rules:
        for proto in r["_protos"]:
            ok, msg = run_cmd(
                f"iptables -t nat -A {CHAIN_NAME} -p {proto} "
                f"--dport {int(r['port_start'])}:{int(r['port_end'])} "
                f"-j DNAT --to-destination {r['target_ip']}"
            )
            if not ok:
                return False, f"添加 DNAT 规则失败 (目标 {r['target_ip']}, {proto}): {msg}"

    # MASQUERADE 回程（每个目标 IP 一条）
    for ip in target_ips:
        ok, msg = run_cmd(
            f"iptables -t nat -A POSTROUTING -d {ip} -j MASQUERADE"
        )
        if not ok:
            return False, f"添加 MASQUERADE 失败 ({ip}): {msg}"

    summary = "; ".join(
        f"{r['target_ip']}:{r['port_start']}-{r['port_end']}" for r in rules
    )
    return True, f"已应用 {len(rules)} 组规则: {summary}"


def apply_config(target_ip, port_start, port_end, whitelist, protocols="tcp,udp", panel_port=None):
    """单规则兼容接口（内部转调 apply_multi_rules）"""
    return apply_multi_rules(
        rules=[{
            "target_ip": target_ip,
            "port_start": port_start,
            "port_end": port_end,
            "protocols": protocols,
        }],
        whitelist=whitelist,
        panel_port=panel_port,
    )


def get_status():
    _, nat_all = run_cmd("iptables -t nat -L -n -v --line-numbers")
    _, chain_rules = run_cmd(f"iptables -t nat -L {CHAIN_NAME} -n -v --line-numbers 2>/dev/null")
    return {
        "nat_table": nat_all,
        "forward_chain": chain_rules,
    }


def has_iptables():
    ok, _ = run_cmd("iptables --version")
    return ok
