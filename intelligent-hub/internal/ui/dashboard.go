package ui

import "strings"

// RenderDashboardHTML returns the self-contained Trusted Player Security & Control Web UI.
// Served strictly on the loopback-isolated Trusted Consent port with anti-clickjacking & CSRF protection.
func RenderDashboardHTML(csrfToken string, coreAddr string, originAddr string) string {
	html := strings.ReplaceAll(dashboardTemplate, "__CSRF_TOKEN__", csrfToken)
	html = strings.ReplaceAll(html, "__CORE_ADDR__", coreAddr)
	html = strings.ReplaceAll(html, "__ORIGIN_ADDR__", originAddr)
	return html
}

const dashboardTemplate = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>G2A 智能枢纽 · 玩家安全与授权中心 (V0.0.1)</title>
<style>
  :root {
    --bg: #0f172a; --panel: #1e293b; --border: #334155; --text: #f8fafc;
    --muted: #94a3b8; --accent: #38bdf8; --ok: #22c55e; --warn: #f59e0b; --danger: #ef4444;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }
  header { padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; background: #0b1120; }
  header h1 { margin: 0; font-size: 18px; display: flex; align-items: center; gap: 10px; }
  .badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #1d4ed8; color: #fff; font-weight: 600; }
  .badge.ok { background: #15803d; } .badge.warn { background: #b45309; } .badge.danger { background: #b91c1c; }
  main { max-width: 1200px; margin: 0 auto; padding: 20px 24px; display: grid; gap: 20px; }
  .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 20px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }
  .card h2 { margin: 0 0 12px 0; font-size: 15px; color: var(--accent); display: flex; justify-content: space-between; align-items: center; }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
  .prompt-box { border: 1px solid var(--warn); background: rgba(245, 158, 11, 0.08); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
  .prompt-box.money { border-color: var(--danger); background: rgba(239, 68, 68, 0.12); }
  .steps { margin: 8px 0; padding-left: 18px; font-family: ui-monospace, monospace; font-size: 12px; color: #e2e8f0; }
  .btn-row { display: flex; gap: 8px; margin-top: 10px; align-items: center; flex-wrap: wrap; }
  button { cursor: pointer; border: none; border-radius: 6px; padding: 6px 12px; font-size: 13px; font-weight: 600; color: #fff; background: #475569; }
  button.approve { background: var(--ok); } button.decline { background: var(--danger); } button.secondary { background: #0284c7; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; }
  code { font-family: ui-monospace, monospace; font-size: 12px; background: rgba(15,23,42,0.7); padding: 2px 5px; border-radius: 4px; }
  .log-list { max-height: 220px; overflow-y: auto; font-family: ui-monospace, monospace; font-size: 12px; }
  .log-item { padding: 4px 0; border-bottom: 1px dashed rgba(148,163,184,0.2); }
</style>
</head>
<body>
<header>
  <h1>🛡️ G2A 智能枢纽控制台 <span class="badge">V0.0.1</span></h1>
  <div style="font-size:12px;color:var(--muted)">
    源点接口: <code>__ORIGIN_ADDR__</code> ｜ 中枢接口: <code>__CORE_ADDR__</code>
  </div>
</header>
<main>
  <section class="card">
    <h2>⚡ 待处理玩家审批请求（整队单次确认 / 会话授权 / 闪断恢复） <span id="pending-count" class="badge warn">0</span></h2>
    <div id="pending-list" class="empty">当前无挂起的审批请求。</div>
  </section>

  <div class="grid-2">
    <section class="card">
      <h2>🎮 已注册游戏槽位（交互源点）</h2>
      <div id="slots-list" class="empty">尚无游戏接入。</div>
    </section>
    <section class="card">
      <h2>🧠 活跃智能体会话（演算中枢）</h2>
      <div id="sessions-list" class="empty">尚无活跃会话。</div>
    </section>
  </div>

  <div class="grid-2">
    <section class="card">
      <h2>🧭 双轨制演算中枢接入目录（支持完全自定义接入，零锁定）</h2>
      <div id="directory-box" style="font-size:13px;color:#cbd5e1"></div>
    </section>
    <section class="card">
      <h2>📜 实时安全与队列审计日志</h2>
      <div id="audit-list" class="log-list empty">暂无审计事件。</div>
    </section>
  </div>
</main>
<script>
const CSRF = "__CSRF_TOKEN__";
async function apiPost(path, body) {
  await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-G2A-CSRF": CSRF },
    body: JSON.stringify(body)
  });
  await refresh();
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function refresh() {
  const res = await fetch("/trusted/status", { headers: { "X-G2A-CSRF": CSRF } });
  if (!res.ok) return;
  const st = await res.json();
  const pend = st.pending || {};
  const scopes = pend.scope_prompts || [];
  const queues = pend.queue_prompts || [];
  const resumes = pend.resume_prompts || [];
  const totalPend = scopes.length + queues.length + resumes.length;
  document.getElementById("pending-count").textContent = totalPend;

  let pHtml = "";
  for (const sp of scopes) {
    pHtml += '<div class="prompt-box"><div><strong>新会话授权请求</strong> · 中枢 <code>' + esc(sp.scope.core_id) + '</code> 请求接入槽位 <code>' + esc(sp.scope.slot_id) + '</code></div>' +
      '<div style="font-size:12px;color:var(--muted)">Scope 摘要: <code>' + esc(sp.scope_digest) + '</code></div>' +
      '<div class="btn-row">' +
      '<button class="approve" onclick="apiPost(\'/trusted/decide-scope\',{prompt_id:\'' + esc(sp.prompt_id) + '\',approved:true,remember_auto_join:false})">仅本次批准</button>' +
      '<button class="secondary" onclick="apiPost(\'/trusted/decide-scope\',{prompt_id:\'' + esc(sp.prompt_id) + '\',approved:true,remember_auto_join:true})">批准并记住精确 Scope</button>' +
      '<button class="decline" onclick="apiPost(\'/trusted/decide-scope\',{prompt_id:\'' + esc(sp.prompt_id) + '\',approved:false})">拒绝</button></div></div>';
  }
  for (const qp of queues) {
    const cls = qp.require_native_real_money_ui ? "prompt-box money" : "prompt-box";
    const stepsHtml = (qp.steps || []).map(s => '<li><code>' + esc(s.method_id) + '</code> [actor=<code>' + esc(s.actor) + '</code>] args=' + esc(JSON.stringify(s.args)) + '</li>').join("");
    pHtml += '<div class="' + cls + '"><div><strong>整队单次确认门禁</strong> · <code>' + esc(qp.queue_id) + '</code> — ' + esc(qp.intent_summary) + '</div>' +
      (qp.require_native_real_money_ui ? '<div style="color:#fca5a5;font-size:12px;font-weight:700">⚠️ 含真实资金风险标记，仅限本原生安全界面真人确认</div>' : '') +
      '<ol class="steps">' + stepsHtml + '</ol>' +
      '<div style="font-size:12px;color:var(--muted)">防篡改摘要: <code>' + esc(qp.queue_digest) + '</code></div>' +
      '<div class="btn-row">' +
      '<button class="approve" onclick="apiPost(\'/trusted/decide-queue\',{session_id:\'' + esc(qp.session_id) + '\',queue_id:\'' + esc(qp.queue_id) + '\',queue_digest:\'' + esc(qp.queue_digest) + '\',fingerprint:\'' + esc(qp.fingerprint) + '\',approved:true})">批准整队执行</button>' +
      '<button class="decline" onclick="apiPost(\'/trusted/decide-queue\',{session_id:\'' + esc(qp.session_id) + '\',queue_id:\'' + esc(qp.queue_id) + '\',queue_digest:\'' + esc(qp.queue_digest) + '\',fingerprint:\'' + esc(qp.fingerprint) + '\',approved:false})">拒绝并冷却</button></div></div>';
  }
  for (const rp of resumes) {
    pHtml += '<div class="prompt-box"><div><strong>长时断线恢复确认</strong> · 会话 <code>' + esc(rp.session_id) + '</code> 请求恢复行动</div>' +
      '<div class="btn-row">' +
      '<button class="approve" onclick="apiPost(\'/trusted/decide-resume\',{session_id:\'' + esc(rp.session_id) + '\',approved:true})">允许恢复</button>' +
      '<button class="decline" onclick="apiPost(\'/trusted/decide-resume\',{session_id:\'' + esc(rp.session_id) + '\',approved:false})">关闭会话</button></div></div>';
  }
  document.getElementById("pending-list").innerHTML = pHtml || '<span class="empty">当前无挂起的审批请求。</span>';

  const slots = st.slots || [];
  if (!slots.length) {
    document.getElementById("slots-list").innerHTML = '<div class="empty">尚无游戏接入。</div>';
  } else {
    let sRows = '<table><tr><th>槽位</th><th>角色/计费</th><th>在线/修订</th><th>控制</th></tr>';
    for (const sl of slots) {
      sRows += '<tr><td><code>' + esc(sl.slot_id) + '</code></td><td>' + esc(sl.role_kind) + ' / ' + esc(sl.billing_mode) + '</td>' +
        '<td>' + (sl.player_online ? '<span class="badge ok">在线</span>' : '<span class="badge danger">离线</span>') + ' epoch=' + sl.epoch + ' rev=' + sl.rev + '</td>' +
        '<td><button class="decline" onclick="apiPost(\'/trusted/emergency-stop\',{slot_id:\'' + esc(sl.slot_id) + '\'})">紧急叫停</button></td></tr>';
    }
    document.getElementById("slots-list").innerHTML = sRows + '</table>';
  }

  const sessions = st.sessions || [];
  if (!sessions.length) {
    document.getElementById("sessions-list").innerHTML = '<div class="empty">尚无活跃会话。</div>';
  } else {
    let cRows = '<table><tr><th>会话 ID</th><th>中枢 / 槽位</th><th>状态 / 配额</th><th>操作</th></tr>';
    for (const se of sessions) {
      cRows += '<tr><td><code>' + esc(se.session_id) + '</code></td><td><code>' + esc(se.core_id) + '</code> → <code>' + esc(se.slot_id) + '</code></td>' +
        '<td><code>' + esc(se.state) + '</code> · 已执行 ' + se.consumption_used + (se.consumption_cap ? '/' + se.consumption_cap : '') + ' 步</td>' +
        '<td>' + (se.state !== 'closed' ? '<button class="decline" onclick="apiPost(\'/trusted/revoke-session\',{session_id:\'' + esc(se.session_id) + '\'})">撤权断开</button>' : '-') + '</td></tr>';
    }
    document.getElementById("sessions-list").innerHTML = cRows + '</table>';
  }

  const dir = st.core_directory || {};
  document.getElementById("directory-box").innerHTML =
    '<p style="margin-top:0"><strong>自由自定义接入命令：</strong><br><code>g2a-core run --hub http://__CORE_ADDR__ --slot &lt;slot_id&gt; --allow-loopback</code></p>' +
    '<p style="margin-bottom:4px;color:var(--muted)">优选兼容中枢参考目录（明确标识，不排他锁定）：</p><ul>' +
    (dir.featured || []).map(f => '<li><strong>' + esc(f.name) + '</strong> (' + esc(f.mode) + ') — ' + esc(f.note) + '</li>').join("") + '</ul>';

  const logs = (st.audit_log || []).slice().reverse();
  document.getElementById("audit-list").innerHTML = logs.length
    ? logs.map(l => '<div class="log-item"><span style="color:var(--muted)">[' + new Date(l.timestamp_ms).toLocaleTimeString() + ']</span> <strong>' + esc(l.category) + '</strong> ' + esc(l.summary) + '</div>').join("")
    : '<span class="empty">暂无审计事件。</span>';
}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
`
