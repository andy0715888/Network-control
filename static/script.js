let editingRuleId = null;

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

// 标签切换
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

// ==================== 规则管理 ====================

async function loadRules() {
    const data = await api('/api/rules');
    if (!data.ok) return;
    renderRules(data.rules);
    renderWhitelist(data.whitelist);
    updateStatusIndicator(data.rules);
    if (!data.iptables_available) {
        toast('警告：未检测到 iptables，请先安装', 'error');
    }
}

function updateStatusIndicator(rules) {
    const enabledCount = rules.filter(r => r.enabled).length;
    const dot = $('status_dot');
    const text = $('status_text');
    if (enabledCount > 0) {
        dot.className = 'status-dot on';
        text.textContent = `${enabledCount} 条规则已启用`;
    } else {
        dot.className = 'status-dot off';
        text.textContent = '暂无启用的规则';
    }
}

function renderRules(rules) {
    const tbody = $('rules_tbody');
    if (!rules.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);">暂无转发规则，点击「+ 添加规则」创建</td></tr>';
        return;
    }
    tbody.innerHTML = rules.map(r => `
        <tr>
            <td>${r.id}</td>
            <td><strong>${escapeHtml(r.target_ip)}</strong></td>
            <td>${r.port_start} - ${r.port_end}</td>
            <td>${r.protocols}</td>
            <td>${escapeHtml(r.note || '-')}</td>
            <td>
                <label class="switch">
                    <input type="checkbox" ${r.enabled ? 'checked' : ''} onchange="toggleRule(${r.id})">
                    <span class="slider"></span>
                </label>
            </td>
            <td>
                <button class="btn-link" onclick="editRule(${r.id})">编辑</button>
                <button class="btn-link" onclick="deleteRule(${r.id})" style="color:var(--danger);">删除</button>
            </td>
        </tr>
    `).join('');
}

function showRuleForm(rule = null) {
    editingRuleId = rule ? rule.id : null;
    $('rule-form-title').textContent = rule ? '编辑转发规则' : '添加转发规则';
    $('rule_target_ip').value = rule ? rule.target_ip : '';
    $('rule_port_start').value = rule ? rule.port_start : 10000;
    $('rule_port_end').value = rule ? rule.port_end : 60000;
    $('rule_proto_tcp').checked = rule ? rule.protocols.includes('tcp') : true;
    $('rule_proto_udp').checked = rule ? rule.protocols.includes('udp') : true;
    $('rule_note').value = rule ? (rule.note || '') : '';
    $('rule-form-card').style.display = 'block';
    $('rule-form-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function hideRuleForm() {
    $('rule-form-card').style.display = 'none';
    editingRuleId = null;
}

async function toggleRule(id) {
    const data = await api(`/api/rules/${id}`);
    // 获取当前规则
    const rules = (await api('/api/rules')).rules;
    const rule = rules.find(r => r.id === id);
    if (!rule) return;
    const newEnabled = rule.enabled ? 0 : 1;
    const result = await api(`/api/rules/${id}`, 'PUT', { ...rule, enabled: newEnabled });
    toast(result.msg, result.ok ? 'success' : 'error');
    if (result.ok) await loadRules();
}

async function editRule(id) {
    const rules = (await api('/api/rules')).rules;
    const rule = rules.find(r => r.id === id);
    if (rule) showRuleForm(rule);
}

async function deleteRule(id) {
    if (!confirm('确认删除此规则？')) return;
    const data = await api(`/api/rules/${id}`, 'DELETE');
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) await loadRules();
}

window.toggleRule = toggleRule;
window.editRule = editRule;
window.deleteRule = deleteRule;

// 按钮事件
$('btn_new_rule').addEventListener('click', () => showRuleForm());
$('rule_cancel_btn').addEventListener('click', hideRuleForm);

$('rule_save_btn').addEventListener('click', async () => {
    const target_ip = $('rule_target_ip').value.trim();
    const port_start = parseInt($('rule_port_start').value);
    const port_end = parseInt($('rule_port_end').value);
    const protos = [];
    if ($('rule_proto_tcp').checked) protos.push('tcp');
    if ($('rule_proto_udp').checked) protos.push('udp');
    const note = $('rule_note').value.trim();

    if (!target_ip) { toast('请输入目标 IP', 'error'); return; }
    if (!protos.length) { toast('请至少选择一种协议', 'error'); return; }

    const body = {
        target_ip, port_start, port_end,
        protocols: protos.join(','),
        note,
        enabled: 1,
    };

    let data;
    if (editingRuleId) {
        body.enabled = (await api('/api/rules')).rules.find(r => r.id === editingRuleId)?.enabled ?? 1;
        data = await api(`/api/rules/${editingRuleId}`, 'PUT', body);
    } else {
        data = await api('/api/rules', 'POST', body);
    }
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) { hideRuleForm(); await loadRules(); }
});

$('btn_apply_all').addEventListener('click', async () => {
    const rules = (await api('/api/rules')).rules;
    const enabledCount = rules.filter(r => r.enabled).length;
    if (enabledCount === 0) { toast('没有启用的规则', 'error'); return; }
    if (!confirm(`确认应用 ${enabledCount} 条启用的转发规则？这将修改服务器 iptables。`)) return;
    const btn = $('btn_apply_all');
    btn.disabled = true; btn.textContent = '应用中...';
    const data = await api('/api/apply', 'POST');
    btn.disabled = false; btn.textContent = '应用所有启用规则';
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) { await loadRules(); loadStatus(); }
});

$('btn_clear').addEventListener('click', async () => {
    if (!confirm('确认清除所有转发规则？')) return;
    const data = await api('/api/clear', 'POST');
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) { await loadRules(); loadStatus(); }
});

// ==================== 白名单 ====================

async function addWhitelist() {
    const port = parseInt($('wl_port').value);
    if (!port) { toast('请输入端口', 'error'); return; }
    const data = await api('/api/whitelist', 'POST', {
        port,
        protocol: $('wl_protocol').value,
        note: $('wl_note').value,
    });
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) {
        $('wl_port').value = '';
        $('wl_note').value = '';
        await loadRules();
    }
}

async function delWl(id) {
    if (!confirm('删除该白名单端口？')) return;
    const data = await api(`/api/whitelist/${id}`, 'DELETE');
    toast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) await loadRules();
}
window.delWl = delWl;

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

$('btn_add_wl').addEventListener('click', addWhitelist);

// ==================== 状态 ====================

$('btn_refresh_status').addEventListener('click', loadStatus);

async function loadStatus() {
    const data = await api('/api/status');
    if (!data.ok) return;
    $('chain_output').textContent = data.data.forward_chain || '（无 PORT_FORWARD 链）';
    $('nat_output').textContent = data.data.nat_table || '（无）';
}

// ==================== 账号 ====================

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

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

// 初始化
loadRules();
