#!/bin/bash
# ============================================================
# 端口映射控制面板 - 一键安装脚本
# 功能: 安装面板 + systemd 服务 + 开机自启 + 重新应用规则
# ============================================================
set -e

# 可配置项（可用环境变量覆盖）
INSTALL_DIR="${PF_INSTALL_DIR:-/opt/port-forward-panel}"
PANEL_PORT="${PF_PANEL_PORT:-8888}"
SERVICE_NAME="port-forward-panel"
PYTHON_BIN="python3"

# 仓库信息（硬编码，指向 main 主分支）
GH_USER="andy0715888"
GH_REPO="Network-control"
GH_BRANCH="main"

# 三个镜像源 base URL（末尾不带斜杠）
# PF_REPO_URL 可覆盖主源；jsdelivr 镜像始终作为备用
RAW_BASE="${PF_REPO_URL:-https://raw.githubusercontent.com/${GH_USER}/${GH_REPO}/${GH_BRANCH}}"
JSDELIVR_BASE="https://cdn.jsdelivr.net/gh/${GH_USER}/${GH_REPO}@${GH_BRANCH}"
FASTLY_BASE="https://fastly.jsdelivr.net/gh/${GH_USER}/${GH_REPO}@${GH_BRANCH}"

# 颜色
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---- root 检查 ----
if [ "$EUID" -ne 0 ]; then
    error "请使用 root 用户运行此脚本"
    exit 1
fi

# ---- 警示：检测 URL 反引号误用 ----
# 如果通过管道运行，提示用户不要在 URL 两侧加反引号
if [ ! -t 0 ] || [ -p /dev/stdin ]; then
    warn "提示: 安装命令中 URL 两侧请不要加反引号 (\`...\`)"
    warn "  正确写法: curl -fsSL https://.../install.sh | bash"
    echo ""
fi

# ---- apt-get update 辅助（最多执行一次）----
_APT_UPDATED=0
apt_update_once() {
    if [ "$_APT_UPDATED" = "0" ] && command -v apt-get >/dev/null 2>&1; then
        info "刷新 APT 软件源缓存（首次安装）..."
        apt-get update -y >/dev/null 2>&1 || warn "apt-get update 失败，继续尝试安装（如源已缓存可忽略）"
        _APT_UPDATED=1
    fi
}

# ---- 系统检测 ----
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    info "安装 Python3 ..."
    if command -v apt-get >/dev/null 2>&1; then
        apt_update_once
        apt-get install -y python3 python3-pip python3-venv >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 python3-pip >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip >/dev/null 2>&1 || true
    fi
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        error "Python3 安装失败，请手动安装后重试: apt-get install -y python3 python3-pip python3-venv"
        exit 1
    fi
fi

# ---- iptables 检查 ----
if ! command -v iptables >/dev/null 2>&1; then
    info "安装 iptables ..."
    if command -v apt-get >/dev/null 2>&1; then
        apt_update_once
        # 兼容: Debian/Ubuntu 部分版本 iptables 为 transitional package，iptables-nft 是实际包
        apt-get install -y iptables iptables-nft >/dev/null 2>&1 || apt-get install -y iptables >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        yum install -y iptables >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y iptables >/dev/null 2>&1 || true
    fi
    if ! command -v iptables >/dev/null 2>&1; then
        warn "iptables 命令仍未找到，面板将启动但无法应用转发规则"
        warn "请手动执行：apt-get install -y iptables iptables-nft"
    fi
else
    # 有些系统 iptables 是旧版本，检查 iptables-legacy / iptables-nft 是否有可用的
    if ! iptables -L >/dev/null 2>&1; then
        warn "iptables 命令存在但执行失败（可能是容器 / nftables 替代）"
    fi
fi

# ---- 下载/更新代码 ----
mkdir -p "$INSTALL_DIR"
info "安装目录: $INSTALL_DIR"

# 多镜像下载：依次尝试 raw.githubusercontent -> jsdelivr -> fastly jsdelivr
# 任一成功即返回，并打印使用的镜像名
download_file() {
    local file=$1
    local urls=("${RAW_BASE}/${file}" "${JSDELIVR_BASE}/${file}" "${FASTLY_BASE}/${file}")
    local names=("raw" "jsdelivr" "fastly-jsdelivr")
    local i url
    for i in "${!urls[@]}"; do
        url="${urls[$i]}"
        # curl（15秒连接超时，60秒总超时）
        if curl -fsSL --connect-timeout 15 --max-time 60 "$url" -o "$INSTALL_DIR/$file"; then
            if [ -s "$INSTALL_DIR/$file" ]; then
                echo "   -> $file  [${names[$i]}]"
                return 0
            fi
        fi
        # wget 兜底
        if wget -q --timeout=15 --tries=1 -O "$INSTALL_DIR/$file" "$url" 2>/dev/null && [ -s "$INSTALL_DIR/$file" ]; then
            echo "   -> $file  [${names[$i]} wget]"
            return 0
        fi
    done
    return 1
}

# 如果是本地安装（脚本同目录有 app.py），则复制本地文件
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/app.py" ]; then
    info "使用本地文件安装 ..."
    # 避免源和目标相同时 cp 报 "are the same file"
    SAME_DIR=false
    [ "$SCRIPT_DIR" = "$INSTALL_DIR" ] && SAME_DIR=true
    if [ "$SAME_DIR" = false ]; then
        cp -f "$SCRIPT_DIR/app.py" "$INSTALL_DIR/"
        cp -f "$SCRIPT_DIR/port_forward.py" "$INSTALL_DIR/"
        cp -f "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
        mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"
        cp -f "$SCRIPT_DIR/templates/"*.html "$INSTALL_DIR/templates/" 2>/dev/null || true
        cp -f "$SCRIPT_DIR/static/"* "$INSTALL_DIR/static/" 2>/dev/null || true
    else
        info "  源和目标是同一目录，跳过文件复制"
    fi
else
    info "从远程仓库下载文件 (多镜像自动回退) ..."
    info "  镜像1: raw.githubusercontent.com"
    info "  镜像2: cdn.jsdelivr.net"
    info "  镜像3: fastly.jsdelivr.net"
    echo ""
    for f in app.py port_forward.py requirements.txt; do
        download_file "$f" || {
            error "下载 $f 失败！所有镜像均不可用。"
            error "可能原因: 服务器无法访问 GitHub / jsdelivr。"
            error "解决方案:"
            error "  1. 直接用 jsdelivr 镜像拉取本脚本并指定源:"
            error "     curl -fsSL https://cdn.jsdelivr.net/gh/${GH_USER}/${GH_REPO}@${GH_BRANCH}/install.sh | PF_REPO_URL=https://cdn.jsdelivr.net/gh/${GH_USER}/${GH_REPO}@${GH_BRANCH} bash"
            error "  2. 手动下载文件上传到 $INSTALL_DIR 后重新运行"
            error "  3. 使用 git clone 整个仓库后本地执行 install.sh"
            exit 1
        }
    done
    mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"
    download_file "templates/login.html" || { warn "templates/login.html 下载失败，登录页将不可用"; }
    download_file "templates/dashboard.html" || { warn "templates/dashboard.html 下载失败，面板页将不可用"; }
    download_file "static/style.css" || warn "static/style.css 下载失败"
    download_file "static/script.js" || warn "static/script.js 下载失败"
    echo ""
fi

# ---- Python 虚拟环境 ----
info "创建 Python 虚拟环境 ..."
cd "$INSTALL_DIR"
if [ ! -d "venv" ] || [ ! -f "venv/bin/pip" ]; then
    # 如果 venv 目录损坏或 pip 不存在，删除重建
    [ -d "venv" ] && rm -rf venv
    if ! "$PYTHON_BIN" -m venv venv 2>/dev/null; then
        # python3-venv 可能未安装，尝试安装
        warn "python3 -m venv 失败，尝试安装 python3-venv ..."
        if command -v apt-get >/dev/null 2>&1; then
            apt_update_once
            apt-get install -y python3-venv >/dev/null 2>&1 || true
        elif command -v yum >/dev/null 2>&1; then
            yum install -y python3-venv >/dev/null 2>&1 || true
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y python3-venv >/dev/null 2>&1 || true
        fi
        # 再试一次
        if ! "$PYTHON_BIN" -m venv venv 2>/dev/null; then
            # 最后回退：用 virtualenv 或直接用系统 pip
            warn "venv 创建失败，尝试使用 virtualenv ..."
            if ! "$PYTHON_BIN" -m pip install virtualenv -q 2>/dev/null; then
                pip3 install virtualenv -q 2>/dev/null || true
            fi
            if command -v virtualenv >/dev/null 2>&1; then
                virtualenv -p "$PYTHON_BIN" venv 2>/dev/null || {
                    error "虚拟环境创建失败，请手动执行: $PYTHON_BIN -m venv venv"
                    exit 1
                }
            else
                error "无法创建虚拟环境，需要 python3-venv 或 virtualenv"
                error "请手动安装后重试：apt-get install python3-venv"
                exit 1
            fi
        fi
    fi
fi
./venv/bin/pip install --upgrade pip -q 2>/dev/null || true
./venv/bin/pip install -r requirements.txt -q 2>/dev/null || {
    error "依赖安装失败，请检查网络连接"
    exit 1
}

# ---- 生成随机密钥（仅首次）----
ENV_FILE="$INSTALL_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    SECRET=$(head -c 32 /dev/urandom | xxd -p 2>/dev/null || openssl rand -hex 32)
    cat > "$ENV_FILE" <<EOF
PF_SECRET_KEY=$SECRET
PF_PANEL_PORT=$PANEL_PORT
PF_DB_PATH=$INSTALL_DIR/panel.db
PF_INSTALL_DIR=$INSTALL_DIR
EOF
    info "已生成配置: $ENV_FILE"
fi

# 加载环境变量用于初始化默认账号
set -a; . "$ENV_FILE"; set +a

# ---- systemd 服务 ----
info "配置 systemd 服务 ..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Port Forward Panel
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStartPre=-$INSTALL_DIR/venv/bin/python -c "import app; app.init_db()"
ExecStart=$INSTALL_DIR/venv/bin/python app.py
ExecStartPost=/bin/bash -c 'sleep 2; curl -s -X POST http://127.0.0.1:${PANEL_PORT}/api/reapply || true'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl restart "${SERVICE_NAME}"

# ---- 开放面板端口 ----
if command -v ufw >/dev/null 2>&1; then
    ufw allow ${PANEL_PORT}/tcp >/dev/null 2>&1 || true
fi

# ---- 输出信息 ----
SERVER_IP=$(curl -s4 ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "服务器IP")

echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${GREEN}  端口映射控制面板安装完成!${NC}"
echo -e "${CYAN}========================================${NC}"
echo -e " 访问地址 : ${GREEN}http://${SERVER_IP}:${PANEL_PORT}${NC}"
echo -e " 默认账号 : ${GREEN}admin${NC}"
echo -e " 默认密码 : ${GREEN}admin123${NC}"
echo -e " 安装目录 : ${INSTALL_DIR}"
echo -e " 服务名称 : ${SERVICE_NAME}"
echo ""
echo -e " ${YELLOW}请立即登录并修改默认账号密码!${NC}"
echo ""
echo -e " 常用命令:"
echo -e "   systemctl status  ${SERVICE_NAME}"
echo -e "   systemctl restart ${SERVICE_NAME}"
echo -e "   systemctl stop    ${SERVICE_NAME}"
echo -e "   journalctl -u ${SERVICE_NAME} -f"
echo ""
