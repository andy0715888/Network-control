#!/bin/bash
# ============================================================
# 端口映射控制面板 - 更新脚本
# 重新拉取代码并重启服务，保留数据库和配置
# ============================================================
set -e

INSTALL_DIR="${PF_INSTALL_DIR:-/opt/port-forward-panel}"
SERVICE_NAME="port-forward-panel"

# 仓库信息（硬编码，指向 main 主分支）
GH_USER="andy0715888"
GH_REPO="Network-control"
GH_BRANCH="main"
RAW_BASE="${PF_REPO_URL:-https://raw.githubusercontent.com/${GH_USER}/${GH_REPO}/${GH_BRANCH}}"
JSDELIVR_BASE="https://cdn.jsdelivr.net/gh/${GH_USER}/${GH_REPO}@${GH_BRANCH}"
FASTLY_BASE="https://fastly.jsdelivr.net/gh/${GH_USER}/${GH_REPO}@${GH_BRANCH}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    error "请使用 root 用户运行此脚本"
    exit 1
fi

if [ ! -d "$INSTALL_DIR" ]; then
    error "未找到安装目录 $INSTALL_DIR，请先运行安装脚本"
    exit 1
fi

# 备份数据库（保留账号、配置、白名单）
if [ -f "$INSTALL_DIR/panel.db" ]; then
    BACKUP="$INSTALL_DIR/panel.db.bak.$(date +%Y%m%d%H%M%S)"
    cp "$INSTALL_DIR/panel.db" "$BACKUP"
    info "已备份数据库: $BACKUP"
fi

# 备份 .env
if [ -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak"
    info "已备份 .env"
fi

download_file() {
    local file=$1
    local urls=("${RAW_BASE}/${file}" "${JSDELIVR_BASE}/${file}" "${FASTLY_BASE}/${file}")
    local names=("raw" "jsdelivr" "fastly-jsdelivr")
    local i url
    for i in "${!urls[@]}"; do
        url="${urls[$i]}"
        if curl -fsSL --connect-timeout 15 --max-time 60 "$url" -o "$INSTALL_DIR/${file}.tmp"; then
            if [ -s "$INSTALL_DIR/${file}.tmp" ]; then
                mv "$INSTALL_DIR/${file}.tmp" "$INSTALL_DIR/$file"
                echo "   -> $file  [${names[$i]}]"
                return 0
            fi
        fi
        if wget -q --timeout=15 --tries=1 -O "$INSTALL_DIR/${file}.tmp" "$url" 2>/dev/null && [ -s "$INSTALL_DIR/${file}.tmp" ]; then
            mv "$INSTALL_DIR/${file}.tmp" "$INSTALL_DIR/$file"
            echo "   -> $file  [${names[$i]} wget]"
            return 0
        fi
    done
    rm -f "$INSTALL_DIR/${file}.tmp"
    return 1
}

# 如果是本地更新（脚本同目录有 app.py），则复制本地文件
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/app.py" ]; then
    info "使用本地文件更新 ..."
    cp -f "$SCRIPT_DIR/app.py" "$INSTALL_DIR/"
    cp -f "$SCRIPT_DIR/port_forward.py" "$INSTALL_DIR/"
    cp -f "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
    mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"
    cp -f "$SCRIPT_DIR/templates/"*.html "$INSTALL_DIR/templates/" 2>/dev/null || true
    cp -f "$SCRIPT_DIR/static/"* "$INSTALL_DIR/static/" 2>/dev/null || true
else
    info "从远程仓库更新代码 (多镜像自动回退) ..."
    info "  镜像1: raw.githubusercontent.com"
    info "  镜像2: cdn.jsdelivr.net"
    info "  镜像3: fastly.jsdelivr.net"
    echo ""
    for f in app.py port_forward.py requirements.txt; do
        download_file "$f" || { warn "更新 $f 失败"; }
    done
    mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"
    download_file "templates/login.html" || true
    download_file "templates/dashboard.html" || true
    download_file "static/style.css" || true
    download_file "static/script.js" || true
fi

# 更新依赖
info "更新 Python 依赖 ..."
cd "$INSTALL_DIR"
./venv/bin/pip install -r requirements.txt -q 2>/dev/null || warn "部分依赖更新失败（可忽略）"

# 重启服务
info "重启服务 ..."
systemctl daemon-reload
systemctl restart "${SERVICE_NAME}"

# 校验
sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    echo ""
    echo -e "${CYAN}========================================${NC}"
    echo -e "${GREEN}  面板更新完成!${NC}"
    echo -e "${CYAN}========================================${NC}"
    # 读取端口
    PANEL_PORT=$(grep -oP 'PF_PANEL_PORT=\K\d+' "$INSTALL_DIR/.env" 2>/dev/null || echo "8888")
    SERVER_IP=$(curl -s4 ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "服务器IP")
    echo -e " 访问地址 : http://${SERVER_IP}:${PANEL_PORT}"
    echo -e " 账号数据 : 已保留（无需重新登录设置）"
    echo ""
else
    error "服务启动失败，请查看日志: journalctl -u ${SERVICE_NAME} -f"
    warn "如需回滚，可恢复备份的 panel.db 和 .env.bak"
    exit 1
fi
