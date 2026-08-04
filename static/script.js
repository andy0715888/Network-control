// ===== 工具函数 =====
const $ = (id) => document.getElementById(id);

function toast(msg, type = 'success') {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast show ' + type;
    setTimeout(() => t.classList.remove('show'), 3000);
}

async function api(url, method = 'GET', body = null) {
    const opts = { method, headers: {} };
    if (body) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({ ok: false, msg: '请求失败' }));
    if (res.status === 401) {
        toast('登录已过期，请重新登录', 'error');
        setTimeout(() => location.href = '/login', 1500);
        throw new Error('unauthorized');
    }
    return data;
}

// ===== 标签切换 =====
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        item.classList.add('active');
        $('tab-' + item.dataset.tab).classList.add('active');
        if (item.dataset.tab === 'status') loadStatus();
    });
});

// ===== 加载配置 =====
async function loadConfig() {
    const data = await api('/api/config');
    if (!data.ok) return;
    const c = data.config;
    $('target_ip').value = c.target_ip || '';
    $('port_start').value = c.port_start;
    $('port_end').value = c.port_end;
    $('proto_tcp').checked = c.protocols.includes('tcp');
    $('proto_udp').checked = c.protocols.includes('udp');
    updateStatus(c.enabled);
    renderWhitelist(data.whitelist);
    if (!data.iptables_available) {
        toast('警告：未检测到 iptables，请先安装', 'error');
    }
}

function updateStatus(enabled) {
    const dot = $('status_dot');
    const text = $('status_text');
    if (enabled) {
        dot.className = 'status-dot on';
        text.textContent = '转发已启用';
    } else {
        dot.className = 'status-dot off';
        text.textContent = '转发未启用';
    }
}

// ===== 保存配置 =====
$('btn_save').addEventListener('click', async () => {
    const protos = [];
    if ($('proto_tcp').checked) protos.push('tcp');
    if ($('proto_udp').checked) protos.push('udp');
    if (protos.length === 0) { toast('请至少选择一种协议', 'error'); return; }
    const data = await api('/api/config', 'POST', {
        target_ip: $('target_ip').value.trim(),
        port_start: parseInt($('port_start').value),
        port_end: parseInt($('port_end').value),
        protocols: protos.join(','),
    });
    toast(data.msg, data.ok ? 'success' : 'error');
});

// ===== 应用规则 =====
$('btn_apply').addEventListener('click', async () => {
    if (!$('target_ip').value.trim()) { toast('请先填写目标 IP', 'error'); return; }
    if (!confirm('确认应用端口映射规则？这将修改服务器 iptables。')) return;
    const btn = $('btn_apply');
    btn.disabled = true; btn.textContent = '应用中...';
    const data = await api('/api/apply', 'POST');
    btn.disabled = false; btn.textContent = '应用规则';
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) { await loadConfig(); loadStatus(); }
});

// ===== 清除规则 =====
$('btn_clear').addEventListener('click', async () => {
    if (!confirm('确认清除所有转发规则？')) return;
    const data = await api('/api/clear', 'POST');
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) updateStatus(false);
});

// ===== 白名单 =====
$('btn_add_wl').addEventListener('click', async () => {
    const port = $('wl_port').value;
    if (!port) { toast('请输入端口', 'error'); return; }
    const data = await api('/api/whitelist', 'POST', {
        port: parseInt(port),
        protocol: $('wl_protocol').value,
        note: $('wl_note').value,
    });
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) {
        $('wl_port').value = '';
        $('wl_note').value = '';
        await loadConfig();
    }
});

function renderWhitelist(list) {
    const tbody = $('wl_table');
    if (!list.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-dim);">暂无白名单端口</td></tr>';
        return;
    }
    tbody.innerHTML = list.map(w => `
        <tr>
            <td><strong>${w.port}</strong></td>
            <td>${w.protocol}</td>
            <td>${escapeHtml(w.note || '-')}</td>
            <td><button class="btn-link" onclick="delWl(${w.id})">删除</button></td>
        </tr>
    `).join('');
}

async function delWl(id) {
    if (!confirm('删除该白名单端口？')) return;
    const data = await api('/api/whitelist/' + id, 'DELETE');
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) await loadConfig();
}
window.delWl = delWl;

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

// ===== 规则状态 =====
$('btn_refresh_status').addEventListener('click', loadStatus);

async function loadStatus() {
    const data = await api('/api/status');
    if (!data.ok) return;
    $('chain_output').textContent = data.data.forward_chain || '（无 PORT_FORWARD 链）';
    $('nat_output').textContent = data.data.nat_table || '（无）';
}

// ===== 账号设置 =====
$('btn_change_username').addEventListener('click', async () => {
    const username = $('new_username').value.trim();
    const pwd = $('verify_pwd_username').value;
    if (!username) { toast('请输入新用户名', 'error'); return; }
    if (!pwd) { toast('请输入当前密码', 'error'); return; }
    const data = await api('/api/change_username', 'POST', { username, password: pwd });
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) {
        $('new_username').value = '';
        $('verify_pwd_username').value = '';
        setTimeout(() => location.reload(), 1200);
    }
});

$('btn_change_password').addEventListener('click', async () => {
    const oldp = $('old_password').value;
    const newp = $('new_password').value;
    const conf = $('confirm_password').value;
    if (!oldp || !newp) { toast('请填写完整', 'error'); return; }
    if (newp !== conf) { toast('两次新密码不一致', 'error'); return; }
    if (newp.length < 6) { toast('新密码至少 6 位', 'error'); return; }
    const data = await api('/api/change_password', 'POST', { old_password: oldp, new_password: newp });
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) {
        $('old_password').value = '';
        $('new_password').value = '';
        $('confirm_password').value = '';
    }
});

// ===== 初始化 =====
loadConfig();
