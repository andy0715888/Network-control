# 端口映射控制面板 (Port Forward Panel)

基于 Flask + iptables 的轻量级端口转发控制面板。安装后可通过 Web 界面将本机指定端口范围的所有流量转发到另一台服务器，并支持白名单端口（白名单内端口保留在本机不转发）。

## 功能特性

- 账号密码登录，登录后可修改账号和密码
- 配置目标服务器 IP + 端口范围（推荐 10000-60000）
- 支持 TCP / UDP / 双协议转发
- 端口白名单管理（白名单内端口不转发）
- 一键应用 / 清除 iptables 转发规则
- 实时查看 iptables 规则状态
- 面板端口自动加入白名单，避免误转发导致失联
- 开机自启，重启后自动恢复转发规则
- systemd 服务管理

## 工作原理

通过 Linux iptables 的 nat 表实现：

1. `PREROUTING` 链跳转到自定义链 `PORT_FORWARD`
2. 白名单端口在链内 `RETURN`（保留本地，不转发）
3. 范围内其余端口 `DNAT` 到目标服务器 IP
4. `POSTROUTING` 对目标 IP 做 `MASQUERADE`（保证回程流量正常）

> 例如：面板装在服务器 A (1.1.1.1)，配置目标为服务器 B (2.2.2.2)，端口范围 10000-60000。
> 则所有访问 A 的 10000-60000 端口的流量都会被转发到 B，白名单内的端口除外。

## 一键安装

仓库地址：`https://github.com/andy0715888/Network-control`（main 主分支）。在**目标服务器（服务器 A）**以 root 执行：

```bash
curl -fsSL https://raw.githubusercontent.com/andy0715888/Network-control/main/install.sh | bash
```

或使用 wget：

```bash
wget -qO- https://raw.githubusercontent.com/andy0715888/Network-control/main/install.sh | bash
```

> **国内服务器注意**：`raw.githubusercontent.com` 在国内经常无法访问。
> 安装脚本已内置 jsdelivr CDN 自动回退（raw → jsdelivr → fastly jsdelivr）。
> 若 raw 拉取本脚本就失败，可直接用 jsdelivr 镜像拉取脚本：

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/andy0715888/Network-control@main/install.sh | bash
```

自定义面板端口 / 安装目录 / 仓库地址：

```bash
curl -fsSL https://raw.githubusercontent.com/andy0715888/Network-control/main/install.sh | \
  PF_PANEL_PORT=9999 bash
```

> **命令格式提示**：URL 两边**不要**加反引号 `` ` ``，bash 中反引号是命令替换符，会把 URL 当命令执行导致失败。

安装完成后：

- 访问地址：`http://服务器A_IP:8888`
- 默认账号：`admin`
- 默认密码：`admin123`
- **请立即登录并修改默认账号密码！**

## 一键更新

更新到最新版本（保留账号、配置、白名单等数据）：

```bash
curl -fsSL https://raw.githubusercontent.com/andy0715888/Network-control/main/update.sh | bash
```

国内服务器若 raw 不可用，用 jsdelivr 镜像：

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/andy0715888/Network-control@main/update.sh | bash
```

更新脚本会自动备份数据库与配置，拉取最新代码后重启服务。若启动失败可手动回滚备份。

## 使用说明

1. 浏览器访问面板，使用默认账号密码登录
2. 进入「账号设置」修改默认账号和密码
3. 进入「端口映射」填写：
   - 目标服务器 IP（服务器 B 的 IP）
   - 端口范围（如 10000-60000）
   - 选择协议（TCP/UDP）
4. 点击「保存配置」→「应用规则」即生效
5. 在「白名单」中添加不需要转发的端口（如 SSH 22、面板端口等）
6. 在「规则状态」中查看实际 iptables 规则

## 常用管理命令

```bash
systemctl status  port-forward-panel   # 查看状态
systemctl restart port-forward-panel   # 重启面板
systemctl stop    port-forward-panel   # 停止面板
journalctl -u port-forward-panel -f    # 查看日志
```

## 卸载

```bash
systemctl stop port-forward-panel
systemctl disable port-forward-panel
rm -rf /opt/port-forward-panel
rm -f /etc/systemd/system/port-forward-panel.service
systemctl daemon-reload

# 清除 iptables 转发规则
iptables -t nat -F PORT_FORWARD 2>/dev/null
iptables -t nat -D PREROUTING -j PORT_FORWARD 2>/dev/null
iptables -t nat -X PORT_FORWARD 2>/dev/null
```

## 环境要求

- Linux 系统（Debian/Ubuntu/CentOS/Rocky 等）
- root 权限
- Python 3.8+
- iptables

## 安全提示

- 务必修改默认账号密码
- 面板自身端口会自动加入白名单，不会被转发
- 转发依赖 iptables，请确保防火墙未禁用 nat 表
- 建议在受信任的网络环境下使用，面板通信为明文 HTTP，如需可自行套 Nginx + HTTPS
