'use strict';

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const INTENT_CATEGORIES = [
  {id:'radio', label:'无线控制', description:'调整 DU 无线参数和切片无线资源上限。', intents:[
    {id:'dl-mcs', label:'调整 DU 下行 MCS 上限', action:'set_ran_dl_mcs',
      example:'将 DU 下行 MCS 上限设为 20',
      note:'作用于整个 DU 小区，运行中生效；DU 重启后恢复启动配置。'},
    {id:'slice-prb', label:'设置切片下行 PRB 上限', action:'set_ran_slice_prb',
      example:'将切片 SST 3 SD 000003 的下行 PRB 上限设为 30%',
      note:'同切片 UE 共享下行新传上限，不提供最低带宽保证；DU 重启后复位。'},
    {id:'clear-prb', label:'取消切片 PRB 限额', action:'set_ran_slice_prb',
      example:'取消切片 SST 3 SD 000003 的下行 PRB 上限',
      note:'恢复目标切片的新传 PRB 未限额状态，支持在意图账本中回滚。'},
  ]},
  {id:'qos', label:'QoS 与 SLA', description:'修改订户 QoS，或检测并改善业务表现。', intents:[
    {id:'set-qos', label:'修改 5QI / 上下行 AMBR', action:'set_qos',
      example:'把 {active_imsi} 的 5QI 改成 9，上下行各 100Mbps',
      note:'修改订户签约；活动 UE 需要重建会话后生效，会短暂断流。'},
    {id:'latency', label:'低时延优化', action:'reduce_latency',
      example:'给 {active_imsi} 提供低时延保障',
      note:'调整低时延 QoS 档并实测对比；可能重建目标 UE 会话。'},
    {id:'sla', label:'SLA 检测与补偿', action:'sla_guard',
      example:'SLA 保障 {active_imsi} 的 RTT 不超过 30ms',
      note:'先检测指标，未达标时尝试 QoS 补偿并复测；不代表无条件保证 SLA。'},
  ]},
  {id:'slice', label:'切片管理', description:'区分端到端新建切片与订户切片签约。', intents:[
    {id:'new-slice', label:'新建切片（核心网 + RAN）', action:'activate_slice',
      example:'新建切片 SST 4 SD 000123',
      note:'统一配置核心网、UPF 和 CU/DU，并验证接入；会中断所有会话。'},
    {id:'slice-subscription', label:'配置订户的切片与业务 QoS', action:'provision_service',
      example:'给 {active_imsi} 开通业务，切片 SST 3 SD 000003，5QI 6，上下行各 50Mbps',
      note:'为订户配置业务签约。请使用已就绪切片；此动作不会端到端新建切片。'},
  ]},
  {id:'ue', label:'UE / 用户管理', description:'管理用户签约和独立 UE 节点。', intents:[
    {id:'new-ue', label:'新增独立 UE 节点', action:'add_ue',
      example:'新增 UE 节点，IMSI {available_imsi}，切片 SST 3 SD 000003，5QI 6，上下行各 50Mbps',
      note:'补齐鉴权和签约，启动独立 UE；请选择未在线 IMSI 和已就绪切片。'},
    {id:'subscriber', label:'新增 / 补齐用户签约', action:'add_subscriber',
      example:'给新用户 IMSI {available_imsi} 开通，5QI 6，上下行各 50Mbps，切片 SST 3 SD 000003',
      note:'新增或补齐鉴权、签约记录；不启动独立 UE 节点。'},
    {id:'switch-ue', label:'切换主 UE 身份', action:'switch_ue',
      example:'切换活动 UE 到 IMSI {switch_imsi}',
      note:'主 UE 改用另一个已有身份并重新接入，会中断主 UE 当前会话。'},
  ]},
  {id:'core', label:'核心网与出口', description:'调整核心网容量和数据出口。', intents:[
    {id:'capacity', label:'调整 UPF worker 数', action:'boost_capacity',
      example:'把 UPF 的 worker 数扩到 2 个',
      note:'重建 UPF / SMF 并恢复接入，会中断所有会话。'},
    {id:'egress', label:'切换 N6 数据出口（实验）', action:'set_egress',
      example:'把数据出口切换到 192.168.73.136',
      note:'切换及回滚已实测通过；SMF 退出异常尚未根治。会重建 UPF / SMF 并中断所有会话。'},
  ]},
];

let currentJob = null;
let currentSource = null;
let statusTimer = null;
let selectedIntent = null;
let ueInventory = null;

function intentExample(template) {
  const values = {
    active_imsi: ueInventory?.ue_conf?.imsi || '208950000000097',
    available_imsi: ueInventory?.suggested_imsi || '208950000000132',
    switch_imsi: (ueInventory?.ues || []).find(u => u.can_activate && !u.is_active_ue)?.imsi || '208950000000098',
  };
  return template.example.replace(/\{(active_imsi|available_imsi|switch_imsi)\}/g, (_, key) => values[key]);
}

function selectedIntentTemplate() {
  const category = INTENT_CATEGORIES.find(c => c.id === $('#intent-category').value);
  return category?.intents.find(t => t.id === $('#intent-template').value);
}

function bindIntentCatalog() {
  const category = $('#intent-category');
  const templates = $('#intent-template');
  category.innerHTML = '<option value="">选择意图分类</option>' +
    INTENT_CATEGORIES.map(c => `<option value="${c.id}">${esc(c.label)}</option>`).join('');
  category.value = '';
  templates.disabled = true;
  $('#btn-use-template').disabled = true;
  category.onchange = () => {
    const group = INTENT_CATEGORIES.find(c => c.id === category.value);
    templates.innerHTML = `<option value="">${group ? '选择具体意图' : '请先选择分类'}</option>` +
      (group?.intents || []).map(t => `<option value="${t.id}">${esc(t.label)}</option>`).join('');
    templates.value = '';
    templates.disabled = !group;
    $('#intent-category-hint').textContent = group?.description || '先选择分类，再选择具体意图。也可直接在下方输入自然语言。';
    $('#intent-template-preview').hidden = true;
    $('#btn-use-template').disabled = true;
  };
  templates.onchange = () => {
    const template = selectedIntentTemplate();
    $('#intent-template-preview').hidden = !template;
    $('#btn-use-template').disabled = !template;
    if (!template) return;
    $('#intent-template-example').textContent = intentExample(template);
    $('#intent-template-note').textContent = template.note;
  };
  $('#btn-use-template').onclick = () => {
    const template = selectedIntentTemplate();
    if (!template) return;
    const text = intentExample(template);
    const draft = $('#nl').value.trim();
    if (draft && draft !== text && !confirm('用所选意图示例替换当前输入内容？')) return;
    $('#nl').value = text;
    $('#intent-template-example').textContent = text;
    $('#parse-card').style.display = 'none';
    $('#apply-hint').textContent = '示例已填入，请调整 IMSI、切片和参数，再点击“只解析”确认。';
    $('#apply-hint').className = 'hint';
    $('#nl').focus();
  };
}

function selectUeSlice(value) {
  const [sst, sd] = value.split('/');
  $('#ue-sst').value = sst || '';
  $('#ue-sd').value = sd || '';
  $('#ue-slice-choice').value = value;
}

function useSubscriber(imsi) {
  const u = (ueInventory?.ues || []).find(x => x.imsi === imsi);
  if (!u || !u.can_add_node) return reportError(new Error(u?.status_reason || '订户不可用于新增'));
  $('#ue-imsi').value = imsi;
  const choices = (ueInventory.slices || []).filter(s => s.network_ready);
  const slice = choices.find(s => s.sst === u.slice.sst && s.sd === u.slice.sd) || choices[0];
  if (slice) selectUeSlice(`${slice.sst}/${slice.sd}`);
  $('#ue-add-hint').textContent = u.status_reason;
}

async function apiJSON(url, options) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回了无效响应 (${response.status})`); }
  if (!response.ok) {
    const error = new Error(data.error || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function valueText(value) {
  if (value === null || value === undefined) return '未获取';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function preflightHTML(p) {
  const rows = (p.changes || []).map(c => `<tr><td>${esc(c.label)}</td><td>${esc(valueText(c.current))}</td><td>${esc(valueText(c.target))}</td></tr>`).join('');
  return `<h3>${esc(ACTION_CN[p.intent?.action] || p.intent?.action || '')}</h3>
    <table><thead><tr><th>项目</th><th>当前值</th><th>目标值</th></tr></thead><tbody>${rows || '<tr><td colspan="3">检查签约和运行状态，详见执行计划</td></tr>'}</tbody></table>
    <h4>预检查</h4><div class="checks">${(p.checks || []).map(c => `<div class="check ${c.ok ? 'okc' : 'err'}"><span>${c.ok ? '✓' : '✗'}</span><span>${esc(c.check)} ${esc(c.detail || '')}</span></div>`).join('')}</div>
    <h4>影响的在线 UE（${(p.affected_ues || []).length}）</h4><div class="hint">${(p.affected_ues || []).map(r => esc(`${r.node} · ${r.imsi} · ${r.ip}`)).join('<br>') || '当前无受影响的在线 UE；订户修改将在后续会话生效。'}</div>
    ${(p.warnings || []).map(w => `<p class="preview-warning">${esc(w)}</p>`).join('')}
    <h4>执行计划</h4><ol>${(p.plan || []).map(s => `<li>${esc(s)}</li>`).join('')}</ol>
    <p class="hint">采样于 ${esc(new Date(p.sampled_at * 1000).toLocaleTimeString('zh-CN',{hour12:false}))}。确认后会再次检查；状态变化或预览过期时不会执行。</p>`;
}

function effectsHTML(observation) {
  if (!observation) return '<p class="hint">此记录尚无效果对比数据。</p>';
  const before = observation.before || {}, after = observation.after || {};
  const keys = new Map();
  for (const r of [...(before.ues || []),...(after.ues || [])]) keys.set(`${r.node}/${r.imsi}`,r);
  const cell = (r,k) => r && r[k] !== null && r[k] !== undefined ? esc(r[k]) : '未获取';
  let html = `<p class="hint">执行前 / 后使用独立实测窗口；速率反映当时业务负载，含 3 次 ICMP 探测，不代表最大容量或因果改善。</p><div class="effect-table"><table><thead><tr><th>UE</th><th>指标</th><th>执行前</th><th>执行后</th></tr></thead><tbody>`;
  for (const [key,r] of keys) {
    const b = (before.ues || []).find(v => `${v.node}/${v.imsi}`===key);
    const a = (after.ues || []).find(v => `${v.node}/${v.imsi}`===key);
    for (const [field,label] of [['dl_mbps','下行 Mbps'],['ul_mbps','上行 Mbps'],['rtt_ms','平均 RTT ms'],['loss_percent','丢包 %'],['window_s','速率窗口 s']]) {
      html += `<tr><td>${esc(r.node)} · ${esc(r.imsi)}</td><td>${label}</td><td>${cell(b,field)}</td><td>${cell(a,field)}</td></tr>`;
    }
  }
  html += '</tbody></table></div>';
  if (before.ran || after.ran) {
    html += '<h4>DU 实际策略与调度统计</h4><table><thead><tr><th>指标</th><th>执行前</th><th>执行后</th></tr></thead><tbody>';
    for (const [k,label] of [['dl_max_mcs','下行 MCS 上限'],['enabled','PRB 限额启用'],['max_prb_percent','PRB 上限 %'],['matched_users','目标切片 DRB 用户'],['peak_new_prbs','新传 PRB 峰值'],['limit_at_peak','峰值时限额'],['violations','策略累计超限次数']]) {
      if (before.ran?.[k] !== undefined || after.ran?.[k] !== undefined)
        html += `<tr><td>${label}</td><td>${cell(before.ran,k)}</td><td>${cell(after.ran,k)}</td></tr>`;
    }
    for (const [k,label] of [['window_s','PRB 计数窗口 s'],['new_prbs_per_s','新传 PRB / s']]) {
      if (before.prb_window || after.prb_window)
        html += `<tr><td>${label}</td><td>${cell(before.prb_window,k)}</td><td>${cell(after.prb_window,k)}</td></tr>`;
    }
    for (const [k,label] of [['limited_requests','窗口内限额命中'],['violations','窗口内超限次数'],['new_grants','窗口内新传授权']]) {
      if (before.prb_window || after.prb_window)
        html += `<tr><td>${label}</td><td>${cell(before.prb_window?.delta,k)}</td><td>${cell(after.prb_window?.delta,k)}</td></tr>`;
    }
    html += '</tbody></table><p class="hint">峰值和累计计数属于各自策略版本，策略变化会重置；只在同一 DU 进程和版本内计算窗口增量。无数据表示未获取。</p>';
  }
  for (const [label,p] of [['执行前',before],['执行后',after]]) {
    html += `<p class="hint">${label}采样：${p.finished_at ? esc(new Date(p.finished_at*1000).toLocaleString('zh-CN')) : '未获取'}${p.error ? ' · '+esc(p.error) : ''}</p>`;
  }
  return html;
}

let preparedSubmission = null, previewSequence = 0;
async function previewJob(url, label, body) {
  const sequence = ++previewSequence;
  const button = $('#btn-preview-confirm');
  preparedSubmission = null;
  button.disabled = true;
  $('#preview-body').innerHTML = '<p>正在读取当前状态并做预检查…</p>';
  $('#preview-overlay').classList.add('show');
  let result;
  try {
    result = await apiJSON('/api/preflight', {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({route:url,body:body || {}})});
  } catch (error) {
    if (sequence === previewSequence) $('#preview-body').innerHTML = `<p class="err">预检查失败：${esc(error.message)}</p>`;
    throw error;
  }
  if (sequence !== previewSequence) return;
  $('#preview-body').innerHTML = preflightHTML(result);
  preparedSubmission = result.valid && result.token ? {url,label,token:result.token,expires:result.expires_at} : null;
  button.disabled = !preparedSubmission;
}

async function confirmPreview() {
  const prepared = preparedSubmission;
  if (!prepared) return;
  if (Date.now()/1000 > prepared.expires) {
    preparedSubmission = null;
    $('#btn-preview-confirm').disabled = true;
    return reportError(new Error('预览已过期，请关闭后重新预检查'));
  }
  preparedSubmission = null;
  $('#btn-preview-confirm').disabled = true;
  try {
    const job = await apiJSON(prepared.url, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({preflight_token:prepared.token})});
    if (!job.id) throw new Error(job.error || '任务未创建');
    $('#preview-overlay').classList.remove('show');
    trackJob(job.id,prepared.label);
  } catch (error) {
    $('#preview-body').innerHTML += `<p class="err">${esc(error.message)}。请关闭后重新预检查，勿重复下发。</p>`;
    reportError(error);
  }
}

async function readE2JSON(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  try {
    return await apiJSON(url, {signal: controller.signal});
  } catch (error) {
    if (controller.signal.aborted) throw new Error('读取超时（60 秒），请检查 SSH 隧道或 E2 通道后重试');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function setReadBusy(button, busy, label) {
  button.disabled = busy;
  button.ariaBusy = String(busy);
  button.textContent = busy ? '读取中…' : label;
}

function reportError(error) {
  $('#apply-hint').textContent = error.message || String(error);
  $('#apply-hint').className = 'hint err';
}

function serialRefresh(fn) {
  let pending = false, flight = null;
  return function refresh() {
    pending = true;
    if (!flight) {
      flight = (async () => {
        do {
          pending = false;
          try { await fn(); } catch (error) { reportError(error); }
        } while (pending);
      })().finally(() => { flight = null; });
    }
    return flight;
  };
}


/* ---------------------------------------------------------------- 状态条 */
async function refreshSystem() {
  let s;
  try {
    s = await apiJSON('/api/system');
  } catch {
    $('#pills').innerHTML = '<div class="pill bad"><span class="dot"></span>后端不可达</div>';
    return;
  }
  const pills = [];
  const mk = (cls, label, val) =>
    pills.push(`<div class="pill ${cls}"><span class="dot"></span>${label} <b>${esc(val)}</b></div>`);

  mk(s.ue_ip ? (s.ue_ping ? 'ok' : 'warn') : 'bad', 'UE', s.ue_ip || '无会话');
  mk(s.n4_beat > 0 ? 'ok' : 'bad', 'N4', `${s.n4_beat}/60s`);
  mk(s.units_ok === 8 ? 'ok' : 'warn', 'RAN 单元', `${s.units_ok}/8`);
  mk('', '容器', s.containers);
  mk(s.upf_workers !== '?' ? 'ok' : 'warn', 'UPF worker', s.upf_workers);
  mk('', 'NWDAF', `qos ${s.qos}`);
  mk(s.deployments_ready > 0 ? 'ok' : 'warn', 'AI Fabric', `${s.deployments_ready} ready`);
  pills.push(`<div class="pill"><span class="dot"></span>更新 <b>${esc(s.ts)}</b></div>`);
  $('#pills').innerHTML = pills.join('');
}

/* ---------------------------------------------------------------- 账本 */
const ACTION_CN = {
  set_ran_slice_prb: 'E2 切片 PRB 上限',
  set_ran_dl_mcs: 'E2 下行 MCS 上限',
  add_subscriber: '开通用户', set_qos: '改 QoS', provision_service: '开业务档案',
  add_ue: '新增 UE 节点', activate_slice: '激活切片', switch_ue: '切换主 UE',
  set_egress: '切出口', boost_capacity: '扩容', reduce_latency: '降时延', sla_guard: 'SLA 保障',
};
const STATE_CN = { cancelled: '已取消', interrupted: '已中断', queued: '排队中', running: '执行中', verified: '已达成', failed: '失败', rolled_back: '已回滚', planned: '已规划', executing: '执行中' };

function parserTxt(it) {
  const p = it.parser;
  if (!p) return { name: '—', conf: it.confidence ?? '—' };
  if (typeof p === 'string') return { name: p, conf: it.confidence ?? '—' };
  return { name: p.type || JSON.stringify(p), conf: it.confidence ?? p.confidence ?? '—' };
}

function fmtTime(s) {
  // 2026-10-26T16:25:08 -> 10-26 16:25
  if (!s) return '—';
  const m = String(s).match(/^\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : String(s).slice(0, 16);
}

async function refreshIntents() {
  const d = await apiJSON('/api/intents?limit=40');
  const tb = $('#intents');
  tb.innerHTML = '';
  for (const it of d.items) {
    const tr = el('tr', 'click');
    tr.innerHTML = `
      <td class="mono dim nowrap">${esc(fmtTime(it.created_at))}</td>
      <td>${esc(ACTION_CN[it.action] || it.action)}</td>
      <td><span class="badge ${esc(it.state)}">${esc(STATE_CN[it.state] || it.state)}</span></td>
      <td class="mono dim">${esc(it.imsi || (it.params && it.params.workers ? 'UPF' : '—'))}</td>
      <td title="${esc(it.error || it.text)}">${esc((it.text || it.raw_text || '').slice(0, 30))}</td>`;
    tr.onclick = () => openDetail(it.id);
    tb.appendChild(tr);
  }
  $('#ledger-count').textContent = `${d.items.length} 条`;
}

/* ---------------------------------------------------------------- 详情 */
async function openDetail(id) {
  selectedIntent = id;
  const rec = await apiJSON(`/api/intents/${id}`);
  if (rec.error === 'not found') return;
  $('#m-title').textContent = `${ACTION_CN[rec.action] || rec.action} · ${id.slice(0, 17)}`;
  const st = $('#m-state');
  st.className = 'badge ' + rec.state;
  st.textContent = STATE_CN[rec.state] || rec.state;

  const parts = [];
  const intent = rec.intent || {};
  parts.push(`<div class="card" style="box-shadow:none"><h2>意图</h2>
    <div class="kv">
      <div class="k">原文</div><div class="v">${esc(intent.text || intent.raw_text || '—')}</div>
      <div class="k">动作</div><div class="v">${esc(rec.action)}</div>
      <div class="k">目标</div><div class="v">${esc(JSON.stringify(intent.target || {}))}</div>
      <div class="k">参数</div><div class="v">${esc(JSON.stringify(intent.params || {}))}</div>
      <div class="k">约束</div><div class="v">${esc(JSON.stringify(intent.constraints || {}))}</div>
      <div class="k">解析器</div><div class="v">${esc(parserTxt(intent).name)} / 置信度 ${esc(parserTxt(intent).conf)}</div>
    </div></div>`);

  if (Array.isArray(rec.plan) && rec.plan.length) {
    parts.push(`<div class="card" style="box-shadow:none"><h2>执行计划</h2><div class="checks">` +
      rec.plan.map((p, i) => `<div class="check"><span class="m dim">${i + 1}.</span><span>${esc(p)}</span></div>`).join('') +
      `</div></div>`);
  }
  if (Array.isArray(rec.verify) && rec.verify.length) {
    parts.push(`<div class="card" style="box-shadow:none"><h2>验证证据</h2><div class="checks">` +
      rec.verify.map((c) => `<div class="check">
        <span class="m ${c.ok ? 'okc' : 'err'}">${c.ok ? '✓' : '✗'}</span>
        <span class="${c.ok ? '' : 'err'}">${esc(c.check)}</span>
        <span class="dim">${esc(c.detail ?? '')}</span></div>`).join('') + `</div></div>`);
  }
  if (rec.error) {
    parts.push(`<div class="card" style="box-shadow:none"><h2>错误</h2>
      <pre class="err" style="border-color:#f8514955">${esc(rec.error)}</pre></div>`);
  }
  if (rec.snapshot) {
    parts.push(`<details class="card" style="box-shadow:none"><summary class="dim" style="cursor:pointer">快照（回滚依据）</summary>
      <pre>${esc(JSON.stringify(rec.snapshot, null, 1).slice(0, 4000))}</pre></details>`);
  }
  if (rec.exec?._observability) {
    parts.push(`<div class="card" style="box-shadow:none"><h2>执行前后效果</h2>${rec.state==='rolled_back' ? '<p class="preview-warning">此意图已回滚，以下为原始下发时的历史对比。当前策略请使用 DU 读取按钮确认。</p>' : ''}${effectsHTML(rec.exec._observability)}</div>`);
  }

  $('#m-body').innerHTML = parts.join('');
  $('#m-verify').onclick = () => runJob(`/api/intents/${id}/verify`, 'verify');
  $('#m-replay').onclick = () => runJob(`/api/intents/${id}/replay`, 'replay');
  $('#m-rollback').onclick = () => runJob(`/api/intents/${id}/rollback`, 'rollback');
  $('#overlay').classList.add('show');
}

/* ---------------------------------------------------------------- 解析 */
async function doParse() {
  const text = $('#nl').value.trim();
  if (!text) return;
  const out = $('#parsed');
  out.innerHTML = '<div class="dim">解析中…</div>';
  $('#plan').innerHTML = '';
  $('#parse-card').style.display = '';
  const r = await apiJSON('/api/preflight', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ route:'/api/apply',body:{ text } }),
  });

  if (!r.ok) {
    out.innerHTML = `<div class="k">错误</div><div class="v err">${esc(r.error || '解析失败')}</div>`;
    return;
  }
  const it = r.intent || {};
  out.innerHTML = `
    <div class="k">动作</div><div class="v">${esc(it.action)} <span class="dim">(${esc(ACTION_CN[it.action] || '')})</span></div>
    <div class="k">目标</div><div class="v">${esc(JSON.stringify(it.target || {}))}</div>
    <div class="k">参数</div><div class="v">${esc(JSON.stringify(it.params || {}))}</div>
    <div class="k">约束</div><div class="v">${esc(JSON.stringify(it.constraints || {}))}</div>
    <div class="k">解析器</div><div class="v">${esc(parserTxt(it).name)} / 置信度 ${esc(parserTxt(it).conf)}</div>
    <div class="k">说明</div><div class="v dim">${esc(r.description || '—')}</div>`;

  const rows = [];
  if (r.valid === false) {
    rows.push(`<div class="check"><span class="m err">✗</span><span class="err">校验不通过: ${esc(r.invalid)}</span></div>`);
  }
  (r.plan || []).forEach((p, i) => {
    rows.push(`<div class="check"><span class="m dim">${i + 1}.</span><span>${esc(p)}</span></div>`);
  });
  (it.warnings || []).forEach((w) => {
    rows.push(`<div class="check"><span class="m" style="color:var(--warn)">!</span><span style="color:var(--warn)">${esc(w)}</span></div>`);
  });
  $('#plan').innerHTML = preflightHTML(r);
}

/* ---------------------------------------------------------------- 任务 */
async function refreshAfterJob() {
  await Promise.allSettled([refreshIntents(), refreshSystem(), refreshPersist(), refreshUeNodes(), refreshSlices(), refreshNwdaf(), readRanMcs(), readPrbPolicy()]);
  if (selectedIntent && $('#overlay').classList.contains('show')) await openDetail(selectedIntent);
}

function finishJob(job, label) {
  if (currentJob !== job.id) return;
  currentJob = null;
  if (currentSource) currentSource.close();
  currentSource = null;
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = null;
  $('#job-tag').textContent = `${label} · ${STATE_CN[job.status] || job.status} (rc=${job.rc})`;
  refreshAfterJob().catch(reportError);
  apiJSON(`/api/jobs/${job.id}`).then(async record => {
    if (!record.intent_id) return;
    const intent = await apiJSON(`/api/intents/${record.intent_id}`);
    $('#latest-effects').innerHTML = `${intent.state==='rolled_back' ? '<p class="preview-warning">此意图已回滚，以下保留原始下发时的历史效果；当前策略请重新读取。</p>' : ''}${effectsHTML(intent.exec?._observability)}`;
    $('#effects-card').style.display = '';
  }).catch(reportError);
}

function trackJob(id, label) {
  $('#effects-card').style.display = 'none';
  if (currentSource) currentSource.close();
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = null;
  currentJob = id;
  try { localStorage.setItem('intent-current-job', id); } catch { /* storage disabled */ }
  const tag = $('#job-tag');
  tag.style.display = '';
  tag.textContent = `${label} · ${id}`;
  const log = $('#joblog');
  log.textContent = '';
  const es = new EventSource(`/api/jobs/${encodeURIComponent(id)}/stream`);
  currentSource = es;
  const poll = async () => {
    statusTimer = null;
    if (currentJob !== id) return;
    try {
      const job = await apiJSON(`/api/jobs/${encodeURIComponent(id)}`);
      if (!['running', 'queued'].includes(job.status)) {
        log.textContent = job.log || '';
        finishJob(job, label);
        return;
      }
    } catch (error) {
      if (error.status === 404) {
        finishJob({id, status: 'interrupted', rc: -2}, label);
        reportError(new Error('任务记录不可用，请检查意图账本后再决定是否重试'));
        return;
      }
    }
    statusTimer = setTimeout(poll, 2000);
  };
  es.onopen = () => {
    if (currentJob !== id) return;
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = null;
    tag.textContent = `${label} · ${id}`;
  };
  es.onmessage = (ev) => {
    if (currentJob !== id) return;
    const data = JSON.parse(ev.data);
    if (data.text !== undefined) log.textContent += data.text;
    if (data.line !== undefined) log.textContent += data.line + '\n';
    log.scrollTop = log.scrollHeight;
    if (data.done) finishJob({id, status: data.status, rc: data.rc}, label);
  };
  es.onerror = () => {
    if (currentJob !== id) return;
    tag.textContent = `${label} · 连接中断，正在恢复任务状态…`;
    // EventSource reconnects using Last-Event-ID; polling also recovers a terminal state.
    if (!statusTimer) statusTimer = setTimeout(poll, 2000);
  };
}

async function runJob(url, label, body) {
  try {
    if (['/api/apply','/api/ran/mcs','/api/ran/prb','/api/ues','/api/slices/activate','/api/ues/activate'].includes(url) || /\/replay$/.test(url)) {
      return await previewJob(url,label,body);
    }
    const job = await apiJSON(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
    if (!job.id) throw new Error(job.error || '任务未创建');
    trackJob(job.id, label);
  } catch (error) { reportError(error); }
}

async function resumeJob() {
  const data = await apiJSON('/api/jobs');
  let saved;
  try { saved = localStorage.getItem('intent-current-job'); } catch { /* storage disabled */ }
  const job = (data.items || []).find((item) => item.id === saved)
    || (data.items || []).find((item) => ['running', 'queued'].includes(item.status));
  if (job) trackJob(job.id, job.title || '任务');
}

/* ---------------------------------------------------------------- 持久层 */
async function refreshPersist() {
  const p = await apiJSON('/api/persist');
  const db = p.db || {};
  const tb = $('#persist-db');
  tb.innerHTML = '';
  const keys = Object.keys(db);
  if (!keys.length) tb.innerHTML = '<tr><td class="dim">暂无记录的 IMSI</td></tr>';
  for (const k of keys) {
    const v = db[k];
    tb.innerHTML += `<tr><td class="mono">${esc(k)}</td>
      <td class="dim">已保存鉴权 ${v.auth ? '✓' : '✗'} · 签约 ${v.sm ? '✓' : '✗'}</td></tr>`;
  }
  const cfg = (p.cfg || {}).compose_env || {};
  const conts = (p.cfg || {}).containers || {};
  const tc = $('#persist-cfg');
  tc.innerHTML = '';
  const ck = Object.keys(cfg);
  for (const k of ck) {
    tc.innerHTML += `<tr><td class="mono">${esc(k)}</td>
      <td class="mono">${esc(cfg[k].value)}</td><td class="dim">${esc(cfg[k].service || '')}</td></tr>`;
  }
  for (const n of Object.keys(conts)) {
    tc.innerHTML += `<tr><td class="mono">${esc(n)}</td><td class="dim">容器规格</td><td></td></tr>`;
  }
  const files = (p.cfg || {}).files || {};
  for (const k of Object.keys(files)) {
    const f = files[k];
    tc.innerHTML += `<tr><td class="mono">${esc(f.label || f.file)}</td>
      <td class="mono">${esc(f.value)}</td><td class="dim">${esc((f.file || '').split('/').pop())}</td></tr>`;
  }
  if (!ck.length && !Object.keys(conts).length && !Object.keys(files).length)
    tc.innerHTML = '<tr><td class="dim">暂无配置类变更</td></tr>';
  $('#sync-hint').textContent = p.error ? p.error : '';
}

/* ---------------------------------------------------------------- NWDAF 采集 */
function ago(s) {
  if (s === null || s === undefined) return '-';
  if (s < 60) return `${s}s 前`;
  if (s < 3600) return `${Math.floor(s / 60)}m 前`;
  return `${Math.floor(s / 3600)}h 前`;
}

async function refreshNwdaf() {
  let d;
  try {
    d = await apiJSON('/api/nwdaf');
  } catch {
    $('#nwdaf-metrics').innerHTML = '<div class="metric bad"><span>状态</span><b>不可达</b></div>';
    return;
  }
  const t = d.totals || {};
  const sbi = d.sbi || {};
  const freshest = (d.smf || []).reduce((m, r) => Math.min(m, r.age_s ?? 1e9), 1e9);
  const freshCls = freshest < 30 ? '' : freshest < 180 ? 'warn' : 'bad';
  const tiles = [
    ['SMF 采集文档', t.smf_docs ?? '?', ''],
    ['AMF 采集文档', t.amf_docs ?? '?', ''],
    ['QoS 监控样本', t.qos_samples ?? '?', ''],
    ['最近更新', freshest === 1e9 ? '-' : ago(freshest), freshCls],
  ];
  $('#nwdaf-metrics').innerHTML = tiles.map(([k, v, cls]) =>
    `<div class="metric ${cls}"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');

  const rows = (d.smf || []).map((r) => {
    const usage = r.dl_kb !== undefined
      ? `↓ ${r.dl_kb} KB / ↑ ${r.ul_kb} KB` : '-';
    const nssai = r.sst !== null && r.sst !== undefined
      ? `${r.sst}${(r.sd_hex || r.sd) ? '.' + (r.sd_hex || r.sd) : ''}` : '-';
    const age = r.age_s < 30 ? '' : (r.age_s < 180 ? 'dim' : 'bad');
    return `<tr>
      <td class="mono">${esc(r.supi)}</td>
      <td>${r.sessions ?? 0}</td>
      <td>${r.qos ?? 0}</td>
      <td class="${age}">${ago(r.age_s)}</td>
      <td class="mono">${esc(usage)}</td>
      <td class="mono">${esc(r.dnn || '-')} / ${esc(nssai)}</td>
    </tr>`;
  });
  $('#nwdaf-smf').innerHTML = rows.join('')
    || '<tr><td colspan="6" class="dim">暂无 SMF 采集记录（NWDAF 未订阅到 SMF 事件）</td></tr>';

  const amf = (d.amf || []).map((x) =>
    `${x.supi}：位置 ${x.location} / RM ${x.rm} / 断连 ${x.loss}，${ago(x.age_s)}`).join('；');
  $('#nwdaf-amf').textContent = amf ? `AMF 事件：${amf}` : 'AMF 事件：暂无记录';

  $('#nwdaf-sbi').innerHTML =
    `SBI <b>${esc(sbi.health || '?')}</b> · 近 2 分钟写入 <b>${sbi.writes_2m ?? '?'}</b> 条 SMF 通知`;
  $('#nwdaf-ts').textContent = `更新 ${d.ts || ''} · ${d.source || ''}`;
}


/* ---------------------------------------------------------------- 切片与 UE */
function tick(v, yes) {
  return `<span class="${yes ? 'yes' : 'no'}">${yes ? '✓' : '✗'}</span>${v ? '' : ''}`;
}

async function refreshSlices() {
  let d;
  try {
    d = await apiJSON('/api/slices');
  } catch {
    $('#slice-rows').innerHTML = '<tr><td colspan="7" class="bad">后端不可达</td></tr>';
    return;
  }
  if (!d.ok) {
    $('#slice-rows').innerHTML = `<tr><td colspan="7" class="bad">${esc(d.error || '读取失败')}</td></tr>`;
    return;
  }
  ueInventory = d;
  const choice = $('#ue-slice-choice');
  const saved = choice.value;
  choice.innerHTML = '<option value="">选择已就绪切片</option>' + (d.slices || []).filter(s => s.network_ready).map(s => `<option value="${s.sst}/${esc(s.sd)}">SST ${s.sst} / SD ${esc(s.sd)}</option>`).join('');
  choice.value = saved;
  const prbChoice = $('#prb-slice-choice');
  const savedPrb = prbChoice.value;
  prbChoice.innerHTML = '<option value="">选择 PRB 控制切片</option>' + (d.slices || []).filter(s => s.network_ready).map(s => `<option value="${s.sst}/${esc(s.sd)}">SST ${s.sst} / SD ${esc(s.sd)}</option>`).join('');
  prbChoice.value = savedPrb;
  const rows = (d.slices || []).map((s) => `<tr>
      <td class="mono">SST ${s.sst} / SD ${esc(s.sd)}</td>
      <td class="${s.core_configured ? 'yes' : 'no'}">${s.core_configured ? '✓' : '✗'}</td>
      <td class="${s.ran_advertised ? 'yes' : 'no'}">${s.ran_advertised ? '✓' : '✗'}</td>
      <td class="${s.upf_registered ? 'yes' : 'no'}">${s.upf_registered ? '✓' : '✗'}</td>
      <td class="${s.ue_requested ? 'yes' : 'no'}">${s.ue_requested ? '✓' : '✗'}</td>
      <td class="${s.session_on_it ? 'yes' : 'no'}">${s.session_on_it ? '✓ ' + (s.sessions || []).map((x) => x.node).join(', ') : '✗'}</td>
      <td>${(s.subscribers || []).length}</td>
      <td class="${s.usable ? 'yes' : 'no'}">${s.network_ready ? '配置就绪' : esc(s.unavailable_reason || '请先激活')}</td>
    </tr>`);
  $('#slice-rows').innerHTML = rows.join('') || '<tr><td colspan="7" class="dim">没有切片</td></tr>';

  const ues = (d.ues || []).map((u) => `<tr>
      <td class="mono">${esc(u.imsi)}</td>
      <td class="mono">SST ${u.slice.sst} / SD ${esc(u.slice.sd)}</td>
      <td>${u.fiveQI ?? '-'}</td>
      <td class="mono">${esc(u.ambr_dl || '-')}</td>
      <td>鉴权 ${u.has_auth ? '✓' : '✗'} / 签约 ${u.has_subscription ? '✓' : '✗'}<br>${esc(u.status_reason)}</td>
      <td class="mono">${esc(u.session_ip || '-')}</td>
      <td><button class="ghost sm" data-use-imsi="${esc(u.imsi)}" ${u.can_add_node ? '' : 'disabled'}>用于新增</button></td>
    </tr>`);
  $('#ue-rows').innerHTML = ues.join('') || '<tr><td colspan="6" class="dim">没有订户</td></tr>';

  $('#ue-rows').querySelectorAll('[data-use-imsi]').forEach(button => {
    button.onclick = () => useSubscriber(button.dataset.useImsi);
  });
  const sess = d.session || {};
  // upf 现在是“已注册切片列表”（多切片补丁后），兼容旧的单对象形式
  const upfList = Array.isArray(d.upf) ? d.upf : (d.upf && d.upf.sst != null ? [d.upf] : []);
  const upfTxt = upfList.length
    ? `${upfList.length} 个切片 (${upfList.map((x) => `${x.sst}/${x.sd}`).join(', ')})`
    : '未注册';
  const sessList = d.sessions || [];
  const sessTxt = sessList.length
    ? `${sessList.length} 个 (${sessList.map((x) => `${x.node}:${x.nssai ? x.nssai.join('.') : '?'}`).join(', ')})`
    : '无';
  $('#slice-tag').textContent = `UPF ${upfTxt} · 活动会话 ${sessTxt} · ${d.ts || ''}`;
}

function renderUeNodes(nodes) {
  const rows = (nodes || []).map((n) => `
    <tr>
      <td class="mono">${esc(n.name)}</td>
      <td class="mono dim">${esc(n.unit || '-')}</td>
      <td class="mono dim">${esc(n.netns || '宿主')}</td>
      <td class="${n.active ? 'yes' : 'no'}">${n.active ? '运行' : '停止'}</td>
      <td class="mono">${esc(n.imsi || '-')}</td>
      <td class="mono">${esc(n.ip || '-')}</td>
    </tr>`);
  const tb = $('#ue-node-rows');
  if (tb) tb.innerHTML = rows.join('') || '<tr><td colspan="6" class="dim">没有 UE 节点</td></tr>';
}

async function refreshUeNodes() {
  try {
    const d = await apiJSON('/api/ue-nodes');
    renderUeNodes(d.nodes);
  } catch (e) { /* 忽略 */ }
}

async function readRanMcs() {
  const hint = $('#ran-mcs-hint');
  const button = $('#btn-ran-mcs-read');
  hint.textContent = '正在读取 DU 当前值，通常需要 5–25 秒，并发时可能更久，请稍候…';
  hint.className = 'hint';
  setReadBusy(button, true);
  try {
    const result = await readE2JSON('/api/ran/mcs');
    const s = result.state;
    hint.textContent = `DU PID ${s.pid} · 下行 MCS 上限 ${s.dl_max_mcs} · 配置下限 ${s.dl_min_mcs} · 策略版本 ${s.revision} · 读取于 ${new Date(result.read?.sampled_at ? result.read.sampled_at*1000 : Date.now()).toLocaleTimeString('zh-CN', {hour12:false})}${result.read?.shared ? ' · 合并读取，数据年龄 '+result.read.age_s+'s' : ''}`;
    hint.className = 'hint';
    $('#ran-mcs-value').min = s.dl_min_mcs;
  } catch (error) {
    hint.textContent = `读取失败：${error.message}；可点击按钮重试`;
    hint.className = 'hint err';
  } finally {
    setReadBusy(button, false, '读取 DU 当前值');
  }
}

async function readPrbPolicy() {
  const key = $('#prb-slice-choice').value;
  if (!key) return;
  const [sst, sd] = key.split('/');
  const button = $('#btn-prb-read');
  $('#prb-hint').textContent = `正在读取切片 ${key} 的 DU 策略与统计，通常需要 5–25 秒，并发时可能更久，请稍候…`;
  $('#prb-hint').className = 'hint';
  setReadBusy(button, true);
  try {
    const r = await readE2JSON(`/api/ran/prb?sst=${sst}&sd=${encodeURIComponent(sd)}`);
    const s = r.state;
    if ($('#prb-slice-choice').value !== key) return;
    $('#prb-hint').textContent = `DU PID ${s.pid} · ${sst}/${sd} · ${s.enabled ? '上限 ' + s.max_prb_percent + '%' : '未限额'} · ${s.limit_prbs}/${s.bwp_prbs} PRB · 同切片 UE ${s.matched_users} · 新传峰值 ${s.peak_new_prbs} · 超限 ${s.violations} · 读取于 ${new Date(r.read?.sampled_at ? r.read.sampled_at*1000 : Date.now()).toLocaleTimeString('zh-CN', {hour12:false})}${r.read?.shared ? ' · 合并读取，数据年龄 '+r.read.age_s+'s' : ''}`;
    $('#prb-hint').className = 'hint';
  } catch (e) {
    if ($('#prb-slice-choice').value !== key) return;
    $('#prb-hint').textContent=`读取失败：${e.message}；可点击按钮重试`;
    $('#prb-hint').className='hint err';
  } finally {
    setReadBusy(button, false, '读取策略与统计');
  }
}

function bindSliceForms() {
  const requestPrbRead = () => {
    if (!$('#prb-slice-choice').value) {
      $('#prb-hint').textContent = '请先选择要读取的切片（SST / SD），再读取策略与统计';
      $('#prb-hint').className = 'hint err';
      return Promise.resolve();
    }
    return readPrbPolicy();
  };
  $('#prb-slice-choice').onchange = requestPrbRead;
  $('#btn-prb-read').onclick = requestPrbRead;
  const prbJob = (enabled) => {
    const key=$('#prb-slice-choice').value;
    if (!key) return reportError(new Error('请选择 PRB 控制切片'));
    const [sst,sd]=key.split('/');const body={sst:Number(sst),sd,enabled};
    if (enabled) {
      const value=$('#prb-percent').value.trim();
      if (!/^\d+$/.test(value) || Number(value)<1 || Number(value)>100) return reportError(new Error('PRB 上限必须为 1-100 的整数百分比'));
      body.max_prb_percent=Number(value);
    }
    runJob('/api/ran/prb','E2 切片 PRB 上限',body);
  };
  $('#btn-prb-set').onclick = () => prbJob(true);
  $('#btn-prb-clear').onclick = () => prbJob(false);
  $('#btn-ran-mcs-read').onclick = () => readRanMcs();
  $('#btn-ran-mcs-set').onclick = () => {
    const value = $('#ran-mcs-value').value.trim();
    if (!/^\d+$/.test(value) || Number(value)>28) return reportError(new Error('MCS 必须为 0-28 的整数'));
    runJob('/api/ran/mcs','E2 下行 MCS 上限',{max_mcs:Number(value)});
  };
  $('#ue-slice-choice').onchange = () => selectUeSlice($('#ue-slice-choice').value);
  $('#btn-new-imsi').onclick = () => {
    if (!ueInventory?.suggested_imsi) return reportError(new Error('请等待切片与订户信息加载'));
    $('#ue-imsi').value = ueInventory.suggested_imsi;
    const first = (ueInventory.slices || []).find(s => s.network_ready);
    if (!$('#ue-sst').value && first) selectUeSlice(`${first.sst}/${first.sd}`);
    $('#ue-add-hint').textContent = '新 IMSI：将自动补齐鉴权和签约，再启动独立 UE。';
  };
  $('#btn-add-ue').onclick = async () => {
    const imsi = $('#ue-imsi').value.trim();
    if (!/^\d{15}$/.test(imsi)) return alert('IMSI 必须是 15 位数字');
    await refreshSlices();
    const selected = (ueInventory?.ues || []).find(u => u.imsi === imsi);
    if (selected?.is_active_ue) return reportError(new Error(`${imsi} 已在线，请点击“填入未占用 IMSI”新增其他 UE`));
    const body = { imsi };
    if ($('#ue-sst').value.trim()) body.sst = Number($('#ue-sst').value.trim());
    if ($('#ue-sd').value.trim()) body.sd = $('#ue-sd').value.trim();
    if ($('#ue-5qi').value.trim()) body.fiveQI = Number($('#ue-5qi').value.trim());
    if ($('#ue-ambr').value.trim()) { body.ambr_ul = $('#ue-ambr').value.trim(); body.ambr_dl = body.ambr_ul; }
    const ready = (ueInventory?.slices || []).some(s => s.network_ready && s.sst === body.sst && s.sd.toLowerCase() === String(body.sd || '').toLowerCase());
    if (!ready) return reportError(new Error('请选择已就绪切片；表中未注册的切片需要先激活'));
    await runJob('/api/ues', `新增 UE ${imsi}`, body);
  };

  $('#btn-new-slice').onclick = () => {
    const sst = Number($('#slice-sst').value.trim());
    if (!$('#slice-sst').value.trim() || !Number.isInteger(sst) || sst < 0 || sst > 255) return alert('SST 必须为 0-255 的整数');
    const body = { sst, sd: ($('#slice-sd').value.trim() || 'ffffff') };
    const imsi = $('#slice-imsi').value.trim();
    if (imsi) {
      if (!/^\d{15}$/.test(imsi)) return alert('IMSI 必须是 15 位数字');
      body.imsi = imsi;
    }
    runJob('/api/slices/activate', `切片 ${sst}/${body.sd}`, body).then(refreshSlices).catch(() => {});
  };

  $('#btn-act-ue').onclick = () => {
    const imsi = $('#slice-imsi').value.trim();
    if (!/^\d{15}$/.test(imsi)) return alert('请在“绑定 IMSI”中填写要切换到的身份；新增 UE 请使用上方表单');
    const u = (ueInventory?.ues || []).find(x => x.imsi === imsi);
    if (!u?.can_activate) return reportError(new Error(u?.status_reason || '该身份缺少完整鉴权/签约，请先通过新增表单补齐'));
    runJob('/api/ues/activate', `激活 UE ${imsi}`, { imsi }).then(refreshSlices).catch(() => {});
  };
}

refreshSystem = serialRefresh(refreshSystem);
refreshIntents = serialRefresh(refreshIntents);
refreshPersist = serialRefresh(refreshPersist);
refreshUeNodes = serialRefresh(refreshUeNodes);
refreshNwdaf = serialRefresh(refreshNwdaf);
refreshSlices = serialRefresh(refreshSlices);
readRanMcs = serialRefresh(readRanMcs);
readPrbPolicy = serialRefresh(readPrbPolicy);

/* ---------------------------------------------------------------- 启动 */
(function init() {
  bindIntentCatalog();

  $('#btn-parse').onclick = () => doParse().catch(reportError);
  $('#btn-apply').onclick = () => {
    const text = $('#nl').value.trim();
    if (!text) return;
    runJob('/api/apply', 'apply', { text });
  };
  $('#btn-preview-confirm').onclick = () => confirmPreview();
  $('#btn-preview-close').onclick = () => {previewSequence++;preparedSubmission=null;$('#preview-overlay').classList.remove('show');};
  $('#btn-sync').onclick = () => runJob('/api/persist/apply', 'persist');
  $('#m-close').onclick = () => { selectedIntent = null; $('#overlay').classList.remove('show'); };
  $('#overlay').onclick = (e) => { if (e.target.id === 'overlay') $('#overlay').classList.remove('show'); };

  bindSliceForms();
  resumeJob().catch(reportError);
  refreshSystem(); refreshIntents(); refreshPersist(); refreshUeNodes(); refreshNwdaf(); refreshSlices();
  // Schedule from completion, pause hidden tabs, and avoid read load during a write job.
  for (const [fn,delay] of [[refreshSystem,10000],[refreshIntents,12000],[refreshPersist,20000],
      [refreshUeNodes,12000],[refreshNwdaf,12000],[refreshSlices,15000],[readPrbPolicy,30000]]) {
    const poll = async () => {
      try { if (document.visibilityState !== 'hidden' && !currentSource) await fn(); }
      finally { setTimeout(poll,delay); }
    };
    setTimeout(poll,delay);
  }
})();
