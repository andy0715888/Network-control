#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端口映射控制面板后端

功能:
- 账号登录 / 修改密码 / 修改账号
- 多组端口映射规则（每组独立 IP + 端口范围 + 协议）
- 白名单端口管理（白名单内端口不转发）
- 一键应用 / 清除 iptables 转发规则
"""

import os
import json
import sqlite3
import secrets
from functools import wraps
from hashlib import scrypt

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    jsonify, abort
)

import port_forward as pf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PF_DB_PATH", os.path.join(BASE_DIR, "panel.db"))

PANEL_PORT = int(os.environ.get("PF_PANEL_PORT", "8888"))
DEFAULT_USER = os.environ.get("PF_DEFAULT_USER", "admin")
DEFAULT_PASS = os.environ.get("PF_DEFAULT_PASS", "admin123")

app = Flask(__name__)
app.secret_key = os.environ.get("PF_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True


# ---------------------- 数据库 ----------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # 多组转发规则
    c.execute(
        """CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_ip TEXT NOT NULL,
            port_start INTEGER NOT NULL,
            port_end INTEGER NOT NULL,
            protocols TEXT DEFAULT 'tcp,udp',
            note TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # 白名单
    c.execute(
        """CREATE TABLE IF NOT EXISTS whitelist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            port INTEGER NOT NULL,
            protocol TEXT DEFAULT 'tcp,udp',
            note TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(port, protocol)
        )"""
    )
    # 旧版迁移：如果存在旧 config 表，把它的数据迁移到 rules
    old_config = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='config'").fetchone()
    if old_config:
        old = c.execute("SELECT * FROM config WHERE id = 1").fetchone()
        if old and old["target_ip"]:
            exists = c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            if not exists:
                c.execute(
                    "INSERT INTO rules (target_ip, port_start, port_end, protocols, note, enabled) VALUES (?,?,?,?,?,1)",
                    (old["target_ip"], old["port_start"], old["port_end"], old["protocols"] or "tcp,udp", "迁移自旧配置")
                )
        c.execute("DROP TABLE IF EXISTS config")

    # 默认账号
    if not c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        salt = secrets.token_hex(16)
        ph = hash_password(DEFAULT_PASS, salt)
        c.execute(
            "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
            (DEFAULT_USER, ph, salt),
        )
    conn.commit()
    conn.close()


def hash_password(password, salt):
    h = scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=2**14, r=8, p=1, dklen=32,
    )
    return h.hex()


def verify_password(password, salt, expected_hash):
    return secrets.compare_digest(hash_password(password, salt), expected_hash)


# ---------------------- 鉴权 ----------------------

def is_local_internal():
    """判断是否为 localhost 内部调用（开机自启 / systemd 回调）"""
    remote = request.remote_addr or ""
    return remote in ("127.0.0.1", "::1", "localhost")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 允许 localhost 内部调用 POST /api/reapply（开机自启）
        if is_local_internal() and request.path == "/api/reapply" and request.method == "POST":
            return f(*args, **kwargs)
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "未登录"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ---------------------- 页面路由 ----------------------

@app.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.form
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        if row and verify_password(password, row["salt"], row["password_hash"]):
            session["user"] = username
            session.permanent = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="账号或密码错误"), 401
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=session.get("user"))


# ---------------------- API: 账号 ----------------------

@app.route("/api/change_password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    old = data.get("old_password", "")
    new = data.get("new_password", "")
    if len(new) < 6:
        return jsonify({"ok": False, "msg": "新密码至少 6 位"})
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (session["user"],)
    ).fetchone()
    if not row or not verify_password(old, row["salt"], row["password_hash"]):
        conn.close()
        return jsonify({"ok": False, "msg": "原密码错误"})
    salt = secrets.token_hex(16)
    ph = hash_password(new, salt)
    conn.execute(
        "UPDATE users SET password_hash = ?, salt = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
        (ph, salt, session["user"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "密码修改成功"})


@app.route("/api/change_username", methods=["POST"])
@login_required
def change_username():
    data = request.get_json(silent=True) or {}
    new_user = (data.get("username") or "").strip()
    pwd = data.get("password", "")
    if not new_user:
        return jsonify({"ok": False, "msg": "用户名不能为空"})
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (session["user"],)
    ).fetchone()
    if not row or not verify_password(pwd, row["salt"], row["password_hash"]):
        conn.close()
        return jsonify({"ok": False, "msg": "密码错误"})
    try:
        conn.execute(
            "UPDATE users SET username = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_user, row["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"ok": False, "msg": "用户名已存在"})
    session["user"] = new_user
    conn.close()
    return jsonify({"ok": True, "msg": "用户名修改成功"})


# ---------------------- API: 转发规则 ----------------------

def _validate_rule(data):
    """校验规则数据，返回 (cleaned_dict, error_msg)"""
    target_ip = (data.get("target_ip") or "").strip()
    try:
        port_start = int(data.get("port_start", 0))
        port_end = int(data.get("port_end", 0))
    except (TypeError, ValueError):
        return None, "端口必须为整数"
    protocols = data.get("protocols", "tcp,udp")

    import re
    if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", target_ip):
        return None, "目标 IP 格式不正确"
    if not (1 <= port_start <= 65535 and 1 <= port_end <= 65535):
        return None, "端口范围应在 1-65535"
    if port_start > port_end:
        return None, "起始端口不能大于结束端口"

    proto_list = []
    if isinstance(protocols, str):
        for p in protocols.split(","):
            p = p.strip().lower()
            if p in ("tcp", "udp"):
                proto_list.append(p)
    protocols = ",".join(proto_list) if proto_list else "tcp,udp"

    note = (data.get("note") or "")[:200]
    enabled = 1 if data.get("enabled", 1) else 0

    return {
        "target_ip": target_ip,
        "port_start": port_start,
        "port_end": port_end,
        "protocols": protocols,
        "note": note,
        "enabled": enabled,
    }, None


@app.route("/api/rules", methods=["GET"])
@login_required
def list_rules():
    conn = get_db()
    rules = conn.execute(
        "SELECT * FROM rules ORDER BY id"
    ).fetchall()
    wl = conn.execute(
        "SELECT id, port, protocol, note FROM whitelist ORDER BY port"
    ).fetchall()
    conn.close()
    diag = pf.diagnose()
    return jsonify({
        "ok": True,
        "rules": [dict(r) for r in rules],
        "whitelist": [dict(w) for w in wl],
        "panel_port": PANEL_PORT,
        "iptables_available": pf.has_iptables(),
        "diagnose": diag,
    })


@app.route("/api/rules", methods=["POST"])
@login_required
def add_rule():
    data = request.get_json(silent=True) or {}
    cleaned, err = _validate_rule(data)
    if err:
        return jsonify({"ok": False, "msg": err})
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO rules (target_ip, port_start, port_end, protocols, note, enabled)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cleaned["target_ip"], cleaned["port_start"], cleaned["port_end"],
         cleaned["protocols"], cleaned["note"], cleaned["enabled"]),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "msg": "规则已添加", "id": new_id})


@app.route("/api/rules/<int:rid>", methods=["PUT"])
@login_required
def update_rule(rid):
    data = request.get_json(silent=True) or {}
    cleaned, err = _validate_rule(data)
    if err:
        return jsonify({"ok": False, "msg": err})
    conn = get_db()
    cur = conn.execute(
        """UPDATE rules SET target_ip=?, port_start=?, port_end=?, protocols=?, note=?, enabled=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (cleaned["target_ip"], cleaned["port_start"], cleaned["port_end"],
         cleaned["protocols"], cleaned["note"], cleaned["enabled"], rid),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    if affected == 0:
        return jsonify({"ok": False, "msg": "规则不存在"})
    return jsonify({"ok": True, "msg": "规则已更新"})


@app.route("/api/rules/<int:rid>", methods=["DELETE"])
@login_required
def delete_rule(rid):
    conn = get_db()
    cur = conn.execute("DELETE FROM rules WHERE id = ?", (rid,))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    if affected == 0:
        return jsonify({"ok": False, "msg": "规则不存在"})
    return jsonify({"ok": True, "msg": "规则已删除"})


# ---------------------- API: 白名单 ----------------------

@app.route("/api/whitelist", methods=["POST"])
@login_required
def add_whitelist():
    data = request.get_json(silent=True) or {}
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "端口必须为数字"})
    if not (1 <= port <= 65535):
        return jsonify({"ok": False, "msg": "端口范围 1-65535"})
    protocol = data.get("protocol", "tcp,udp")
    note = (data.get("note") or "")[:100]
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO whitelist (port, protocol, note) VALUES (?, ?, ?)",
            (port, protocol, note),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"ok": False, "msg": "该端口+协议已存在"})
    conn.close()
    return jsonify({"ok": True, "msg": "已添加白名单"})


@app.route("/api/whitelist/<int:wid>", methods=["DELETE"])
@login_required
def del_whitelist(wid):
    conn = get_db()
    conn.execute("DELETE FROM whitelist WHERE id = ?", (wid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "已删除"})


# ---------------------- API: 应用 / 清除规则 ----------------------

@app.route("/api/apply", methods=["POST"])
@login_required
def apply_rules():
    conn = get_db()
    rules = conn.execute("SELECT * FROM rules WHERE enabled = 1 ORDER BY id").fetchall()
    wl = conn.execute("SELECT port FROM whitelist").fetchall()
    conn.close()

    if not rules:
        return jsonify({"ok": False, "msg": "没有启用的转发规则"})

    whitelist = [w["port"] for w in wl]
    rules_data = [
        {
            "target_ip": r["target_ip"],
            "port_start": r["port_start"],
            "port_end": r["port_end"],
            "protocols": r["protocols"],
        }
        for r in rules
    ]
    ok, msg = pf.apply_multi_rules(
        rules=rules_data,
        whitelist=whitelist,
        panel_port=PANEL_PORT,
    )
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/clear", methods=["POST"])
@login_required
def clear_rules():
    pf.clear_all_rules()
    return jsonify({"ok": True, "msg": "已清除所有转发规则"})


@app.route("/api/status", methods=["GET"])
@login_required
def status():
    return jsonify({"ok": True, "data": pf.get_status()})


@app.route("/api/diagnose", methods=["GET"])
@login_required
def diagnose_api():
    """系统诊断"""
    return jsonify({"ok": True, "data": pf.diagnose()})


@app.route("/api/reapply", methods=["POST"])
@login_required
def reapply():
    """开机自启调用：若有启用的规则则重新应用"""
    conn = get_db()
    rules = conn.execute("SELECT * FROM rules WHERE enabled = 1 ORDER BY id").fetchall()
    wl = conn.execute("SELECT port FROM whitelist").fetchall()
    conn.close()
    if not rules:
        return jsonify({"ok": True, "msg": "没有启用的规则，跳过"})
    whitelist = [w["port"] for w in wl]
    rules_data = [
        {
            "target_ip": r["target_ip"],
            "port_start": r["port_start"],
            "port_end": r["port_end"],
            "protocols": r["protocols"],
        }
        for r in rules
    ]
    ok, msg = pf.apply_multi_rules(
        rules=rules_data,
        whitelist=whitelist,
        panel_port=PANEL_PORT,
    )
    return jsonify({"ok": ok, "msg": msg})


# ---------------------- 启动 ----------------------

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PANEL_PORT, debug=False)
