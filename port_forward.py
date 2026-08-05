#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iptables 端口映射管理模块

完整的端口转发链路：
  外部请求 → PREROUTING (DNAT 改目的地址) → FORWARD (放行) → POSTROUTING (MASQUERADE 改源地址) → 目标服务器
  回程：目标服务器 → POSTROUTING → FORWARD → PREROUTING → 客户端
"""

import subprocess
import re
import os
import socket

CHAIN_NAME = "PORT_FORWARD"
FORWARD_CHAIN = "PORT_FORWARD_FWD"

# 检测实际使用的 iptables 命令
def _detect_iptables():
    """智能选择 iptables 后端（必须与系统默认一致，否则规则写入不生效）

    检测逻辑：
    1) 运行 `iptables --version`，读取括号里的标识：
       - '(nf_tables)' → 系统默认是 nftables，优先 iptables-nft
       - '(legacy)' → 系统默认是 legacy，优先 iptables-legacy
    2) 如果版本命令不可用或括号没读到，按系统实际 iptables 存在性回退
    3) 最终兜底：直接用 `iptables`（系统默认）

    不能简单按文件存在顺序乱选，否则 legacy/nft 表互不相干，规则白写。
    """
    # 第 1 步：读 iptables --version 判断系统默认后端
    prefer = None  # 'nft' / 'legacy' / None
    try:
        r = subprocess.run(
            ["iptables", "--version"],
            capture_output=True, text=True, timeout=5
        )
        ver = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"\(([^)]+)\)", ver)
        if m:
            tag = m.group(1).lower()
            if "nft" in tag:
                prefer = "nft"
            elif "legacy" in tag:
                prefer = "legacy"
    except Exception:
        pass

    # 第 2 步：按偏好顺序找可执行文件
    def _exists(cmd):
        for p in (f"/usr/sbin/{cmd}", f"/sbin/{cmd}"):
            if os.path.exists(p):
                return True
        # which 兜底
        try:
            r = subprocess.run(["which", cmd], capture_output=True, timeout=3)
            return r.returncode == 0
        except Exception:
            return False

    if prefer == "nft":
        for c in ("iptables-nft", "iptables"):
            if _exists(c):
                return c
    if prefer == "legacy":
        for c in ("iptables-legacy", "iptables"):
            if _exists(c):
                return c

    # 无法判断偏好 → 先按文件存在，最后一定是 iptables
    for c in ("iptables-nft", "iptables-legacy", "iptables"):
        if _exists(c):
            return c
    return "iptables"

IPTABLES = _detect_iptables()


def run_cmd(cmd):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=60
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as e:
        return False, str(e)


def ipt(table, args):
    """封装 iptables 调用"""
    return run_cmd(f"{IPTABLES} -t {table} {args}")


def enable_ip_forwarding():
    run_cmd("sysctl -w net.ipv4.ip_forward=1")
    run_cmd(
        "grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf && "
        "sed -i 's/^net.ipv4.ip_forward.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf || "
        "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf"
    )
    run_cmd("sysctl -p /etc/sysctl.conf >/dev/null 2>&1 || true")


def ensure_chains():
    """确保所有自定义链存在，并挂接到 PREROUTING / FORWARD"""
    # nat 表 PORT_FORWARD 链（DNAT）
    ipt("nat", f"-N {CHAIN_NAME} 2>/dev/null || true")
    ok, _ = ipt("nat", f"-C PREROUTING -j {CHAIN_NAME}")
    if not ok:
        ipt("nat", f"-A PREROUTING -j {CHAIN_NAME}")
    # filter 表 PORT_FORWARD_FWD 链（FORWARD 放行）
    ipt("filter", f"-N {FORWARD_CHAIN} 2>/dev/null || true")
    ok, _ = ipt("filter", f"-C FORWARD -j {FORWARD_CHAIN}")
    if not ok:
        ipt("filter", f"-A FORWARD -j {FORWARD_CHAIN}")


def clear_all_rules():
    """清空所有转发规则"""
    # nat 表
    ipt("nat", f"-F {CHAIN_NAME} 2>/dev/null || true")
    ipt("nat", f"-D PREROUTING -j {CHAIN_NAME} 2>/dev/null || true")
    ipt("nat", f"-X {CHAIN_NAME} 2>/dev/null || true")
    # filter 表
    ipt("filter", f"-F {FORWARD_CHAIN} 2>/dev/null || true")
    ipt("filter", f"-D FORWARD -j {FORWARD_CHAIN} 2>/dev/null || true")
    ipt("filter", f"-X {FORWARD_CHAIN} 2>/dev/null || true")
    # 清除所有与 PORT_FORWARD 相关的 MASQUERADE
    _, post = ipt("nat", "-L POSTROUTING -n 2>/dev/null")
    for line in post.split("\n"):
        if "MASQUERADE" in line:
            m = re.search(r"d\.?(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                ip = m.group(1)
                ipt("nat", f"-D POSTROUTING -d {ip} -j MASQUERADE 2>/dev/null || true")
    # 重置 FORWARD 默认策略到 ACCEPT（避免被锁死）
    ipt("filter", "-P FORWARD ACCEPT 2>/dev/null || true")


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

    完整链路：
    1. nat/PREROUTING → PORT_FORWARD 链 → DNAT 改目的 IP
    2. filter/FORWARD → PORT_FORWARD_FWD 链 → ACCEPT 放行
    3. nat/POSTROUTING → MASQUERADE 改源 IP（保证回程）
    """
    if not rules:
        return False, "没有要应用的规则"

    # 校验
    for i, r in enumerate(rules):
        if not _valid_ip(r["target_ip"]):
            return False, f"规则{i+1}: 目标 IP 格式不正确"
        if not (_valid_port(r["port_start"]) and _valid_port(r["port_end"])):
            return False, f"规则{i+1}: 端口范围不合法"
        if int(r["port_start"]) > int(r["port_end"]):
            return False, f"规则{i+1}: 起始端口大于结束端口"

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
    ensure_chains()

    # 清空旧规则
    ipt("nat", f"-F {CHAIN_NAME} 2>/dev/null || true")
    ipt("filter", f"-F {FORWARD_CHAIN} 2>/dev/null || true")
    for ip in target_ips:
        ipt("nat", f"-D POSTROUTING -d {ip} -j MASQUERADE 2>/dev/null || true")

    # 强制 FORWARD 默认策略为 ACCEPT
    ipt("filter", "-P FORWARD ACCEPT 2>/dev/null || true")

    # --- 第 1 步：白名单 RETURN（nat/PREROUTING 链）---
    for port in sorted(wl_set):
        for proto in ("tcp", "udp"):
            ok, msg = ipt("nat",
                f"-A {CHAIN_NAME} -p {proto} --dport {port} -j RETURN")
            if not ok:
                return False, f"白名单规则添加失败 (端口 {port}): {msg}"

    # --- 第 2 步：FORWARD 链放行规则（filter 表）---
    # 在 FORWARD 链中先添加白名单 RETURN（不走 FORWARD 链放行）
    for port in sorted(wl_set):
        for proto in ("tcp", "udp"):
            ipt("filter",
                f"-A {FORWARD_CHAIN} -p {proto} --dport {port} -j RETURN")

    # FORWARD 链放行规则（允许 DNAT 后的数据包通过）
    for r in rules:
        for proto in r["_protos"]:
            ok, msg = ipt("filter",
                f"-A {FORWARD_CHAIN} -p {proto} "
                f"-d {r['target_ip']} --dport {int(r['port_start'])}:{int(r['port_end'])} "
                f"-j ACCEPT")
            if not ok:
                return False, f"FORWARD 放行规则失败 ({r['target_ip']}, {proto}): {msg}"

    # --- 第 3 步：nat/PREROUTING 链 DNAT ---
    for r in rules:
        for proto in r["_protos"]:
            ok, msg = ipt("nat",
                f"-A {CHAIN_NAME} -p {proto} "
                f"--dport {int(r['port_start'])}:{int(r['port_end'])} "
                f"-j DNAT --to-destination {r['target_ip']}")
            if not ok:
                return False, f"DNAT 规则失败 ({r['target_ip']}, {proto}): {msg}"

    # --- 第 4 步：MASQUERADE 回程 ---
    for ip in target_ips:
        ok, msg = ipt("nat", f"-A POSTROUTING -d {ip} -j MASQUERADE")
        if not ok:
            return False, f"MASQUERADE 失败 ({ip}): {msg}"

    summary = "; ".join(
        f"{r['target_ip']}:{r['port_start']}-{r['port_end']}" for r in rules
    )
    return True, f"已应用 {len(rules)} 组规则: {summary}"


def apply_config(target_ip, port_start, port_end, whitelist, protocols="tcp,udp", panel_port=None):
    """单规则兼容接口"""
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
    _, nat_all = ipt("nat", "-L -n -v --line-numbers")
    _, chain_rules = ipt("nat", f"-L {CHAIN_NAME} -n -v --line-numbers 2>/dev/null")
    _, fwd_rules = ipt("filter", f"-L {FORWARD_CHAIN} -n -v --line-numbers 2>/dev/null")
    _, filter_all = ipt("filter", "-L FORWARD -n -v --line-numbers 2>/dev/null")
    # 读取 ip_forward
    try:
        with open("/proc/sys/net/ipv4/ip_forward") as f:
            ip_forward = int(f.read().strip())
    except Exception:
        ip_forward = -1
    return {
        "iptables_cmd": IPTABLES,
        "ip_forward": ip_forward,
        "nat_table": nat_all,
        "forward_chain": chain_rules,
        "filter_forward_chain": fwd_rules,
        "filter_forward_policy": filter_all,
    }


def diagnose():
    """系统诊断，返回可能的问题列表"""
    issues = []
    results = {}

    # 1. iptables 是否可用
    ipt_ok, ipt_ver = run_cmd(f"{IPTABLES} --version")
    results["iptables_available"] = ipt_ok
    results["iptables_version"] = ipt_ver[:100] if ipt_ok else "不可用"
    if not ipt_ok:
        issues.append("iptables 不可用，无法应用转发规则")

    # 2. ip_forward 是否开启
    try:
        with open("/proc/sys/net/ipv4/ip_forward") as f:
            val = int(f.read().strip())
        results["ip_forward"] = val
        if val == 0:
            issues.append("ip_forward 未开启（net.ipv4.ip_forward=0），数据包不会被转发")
    except Exception:
        results["ip_forward"] = -1
        issues.append("无法读取 ip_forward 状态")

    # 3. FORWARD 默认策略
    ok, fwd_policy = ipt("filter", "-L FORWARD -n 2>/dev/null")
    results["forward_policy"] = fwd_policy
    if ok and "DROP" in fwd_policy.split("\n")[0] if fwd_policy else False:
        # 简单解析
        if "policy DROP" in fwd_policy:
            issues.append("FORWARD 链默认策略为 DROP，需要 ACCEPT 才能转发")

    # 4. 检测是否在容器内
    if os.path.exists("/proc/1/cgroup"):
        with open("/proc/1/cgroup") as f:
            cg = f.read()
        results["in_container"] = "docker" in cg or "lxc" in cg
        if results["in_container"]:
            issues.append("检测到容器环境，容器内 iptables 可能不生效（需要在宿主机配置）")

    # 5. 检查 iptables 后端
    ok_nft, _ = run_cmd("iptables-nft -L -n 2>/dev/null")
    ok_legacy, _ = run_cmd("iptables-legacy -L -n 2>/dev/null")
    results["backend_nft"] = ok_nft
    results["backend_legacy"] = ok_legacy
    results["using"] = IPTABLES
    if ok_nft and not ok_legacy:
        results["note"] = "系统默认使用 nftables 后端"
    elif ok_legacy and not ok_nft:
        results["note"] = "系统默认使用 legacy 后端"

    # 6. 防火墙
    ok_ufw, _ = run_cmd("which ufw")
    results["ufw"] = ok_ufw
    if ok_ufw:
        ok2, ufw_status = run_cmd("ufw status 2>/dev/null")
        results["ufw_status"] = ufw_status if ok2 else "不可查"

    return {
        "status": results,
        "issues": issues,
        "healthy": len(issues) == 0,
    }


def has_iptables():
    ok, _ = run_cmd(f"{IPTABLES} --version")
    return ok
