/* Dashboard controller. Read-only against the loopback API. */

import { createForceGraph } from '/static/vendor/forcegraph.js';
import {
  backupDiffHtml,
  updateGapsHtml,
  breakdownRows,
  setUsageCounts,
  renderMarkdown,
  vendorSourcesHtml,
  catalogueSections,
  num,
  shortPath,
  escapeHtml,
  metaBreakdownHtml,
  trendHtml,
  actionLabelHtml,
  whyNotBlockingHtml,
  howtoHtml,
  catalogueCard,
  updateConfirmHtml,
  updateRunningHtml,
  updateResultHtml,
  legendHtml,
  specsHtml,
  specReviewHtml,
  scheduleHtml,
  syncTargetSummary,
} from '/static/render.js';

const $ = (id) => document.getElementById(id);
const state = { summary: null, health: null, graph: null, inventory: null, updates: null, history: [], fixes: null };

const NODE_COLORS = {
  instruction: '#c0392b',
  skill: '#3d6fd6',
  workflow: '#2f7d4f',
  command: '#b8791f',
  agent: '#7b5ea7',
  hook: '#c85a9c',
  plugin: '#5a8f9c',
  canonical: '#8a8781',
};

const EDGE_LABELS = {
  references: '引用檔案',
  invokes: '呼叫',
  mirror: '宣告的鏡像',
  generated_from: '由此產生',
  duplicate: '未宣告的重複',
  provides: 'plugin 提供',
  collision: '命名衝突',
};

/* ---------------- utilities ---------------- */

let session = { token: null, allow_actions: false };

/* Write actions carry the session token. A cross-origin page cannot read
 * /api/session (CORS blocks the response), so it cannot forge one. */
async function action(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': session.token || '' },
    body: JSON.stringify(body || {}),
  });
}

async function api(path, opts) {
  // Every request from this page carries the session token. Gating an endpoint
  // and forgetting its caller is how the health button started returning 403;
  // attaching it once here means a newly gated route cannot silently break the
  // page. It is same-origin, so this costs nothing and leaks nothing.
  const o = { ...(opts || {}) };
  o.headers = { ...(o.headers || {}), 'X-Studio-Token': session.token || '' };
  const res = await fetch(path, o);
  const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok || body.error) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function showError(msg) {
  const el = $('error');
  if (!msg) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.textContent = msg;
}


function table(el, columns, rows) {
  const head = `<thead><tr>${columns.map((c) => `<th>${c.label}</th>`).join('')}</tr></thead>`;
  const body = rows.length
    ? rows
        .map(
          (r) =>
            `<tr>${columns
              .map((c) => `<td class="${c.cls || ''}">${c.get(r) ?? ''}</td>`)
              .join('')}</tr>`,
        )
        .join('')
    : `<tr><td colspan="${columns.length}" class="muted">沒有資料</td></tr>`;
  el.innerHTML = head + `<tbody>${body}</tbody>`;
}

/* ---------------- tabs ---------------- */

$('tabs').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-tab]');
  if (!btn) return;
  for (const b of $('tabs').querySelectorAll('button')) {
    const on = b === btn;
    b.setAttribute('aria-selected', String(on));
    $(`tab-${b.dataset.tab}`).hidden = !on;
  }
  if (btn.dataset.tab === 'graph') graph.kick(0.4);
  if (btn.dataset.tab === 'specs') loadSchedule();
});

/* ---------------- overview ---------------- */

/* Translate the verdict into the one thing a reader actually wants: do I need
 * to do something? Raw counts did not answer that - "important 1" sitting next
 * to "blocking 0" reads as a contradiction unless you already know that
 * vendor-owned findings never block. */
function renderStatus() {
  const h = state.health || {};
  const c = h.counts || {};
  const blocking = c.blocking ?? null;
  const el = $('status');
  const mark = $('status-mark');
  const title = $('status-title');
  const line = $('status-line');
  const actions = $('status-actions');

  if (blocking === null) {
    el.className = 'status unknown';
    mark.textContent = '–';
    title.textContent = '尚未健檢';
    line.textContent = '按右上角「執行健檢」。';
    actions.innerHTML = '';
    return;
  }

  const items = [];
  const u = state.updates;
  const updateCount = u ? (u.summary.updates_available || 0) + (u.summary.toolkit_updates_available || 0) : null;

  if (blocking > 0) {
    el.className = 'status unhealthy';
    mark.textContent = '✕';
    title.textContent = `不健康 — 有 ${blocking} 項需要你處理`;
    line.textContent = '這些是你自己的設定裡、會實際影響 agent 行為的問題。';
    items.push('到「合規問題」分頁，勾選<b>只看阻斷性</b>，每一條都附了修法與官方依據。');
  } else {
    el.className = 'status healthy';
    mark.textContent = '✓';
    title.textContent = '健康 — 沒有任何需要你動手的項目';
    line.textContent = '你的設定符合目前的官方指引。以下數字都不是待辦事項：';
  }

  if (c.vendor_owned) {
    items.push(
      `<b>${c.vendor_owned} 個 vendor</b>：來自 plugin 或工具組。改了會被下次升級覆蓋，` +
        '所以不算你的問題 —— 能做的是升級、移除，或記錄豁免。',
    );
  }
  // counts is a partition, so this is read straight off the report. It used to
  // be derived as minor - vendor_owned, which is only right when every vendor
  // finding is minor, and the two places that showed it disagreed.
  if (c.minor) {
    items.push(`<b>${c.minor} 個 minor</b>：可以改善但不影響運作的建議，例如沒在用的 plugin、殘留備份檔。`);
  }
  if (c.waived) {
    items.push(`<b>${c.waived} 個已豁免</b>：你明確記錄過理由、決定不修的項目。`);
  }
  if (updateCount === null) {
    items.push('雲端更新<b>尚未檢查</b> —— 到「套件與更新」分頁按一下就會比對。');
  } else if (updateCount > 0) {
    items.push(`<b>${updateCount} 個可用更新</b>，詳見「套件與更新」分頁。`);
  }

  // Spec drift comes from the report itself, so the daily run surfaces a moved
  // specification here without anyone opening a tab. A rule built on last year's
  // guidance goes wrong silently; this is the only thing that says so.
  const sp = h.specs || {};
  if ((sp.changed || []).length) {
    items.push(
      `<b>${sp.changed.length} 份官方規範已改版</b> —— 依據它們的規則要重新確認。` +
        '到「規範與排程」分頁看是哪幾條。',
    );
  }
  if ((sp.unreachable || []).length) {
    items.push(
      `<b>${sp.unreachable.length} 份規範抓不到</b> —— 這次沒檢查到，不能當成規則都還正確。`,
    );
  }
  actions.innerHTML = items.map((s) => `<li>${s}</li>`).join('');
}

function renderOverview() {
  const s = state.summary || {};
  const h = state.health || {};
  const counts = h.counts || s.counts || {};
  const meta = (h.metrics || {}).preloaded_skill_metadata || {};

  $('roots').textContent = s.roots ? `· ${shortPath(s.roots.claude)} + ${shortPath(s.roots.codex)}` : '';
  const verdict = h.verdict || s.verdict || 'UNKNOWN';
  const vEl = $('verdict');
  vEl.textContent = verdict;
  vEl.className = `verdict ${verdict === 'PASS' ? 'pass' : verdict === 'FAIL' ? 'fail' : 'unknown'}`;
  $('stamp').textContent = h.generated_at || s.last_report_at || '';

  renderStatus();

  $('m-blocking').textContent = num(counts.blocking ?? 0);
  // Per runtime, never summed: each one preloads only what it can load, so a
  // combined figure is a number no session ever pays.
  const pr = meta.per_runtime || {};
  const c = (pr.claude || {}).est_tokens;
  const x = (pr.codex || {}).est_tokens;
  $('m-meta').innerHTML =
    c === undefined
      ? `${num(meta.total_est_tokens)}<small> tokens</small>`
      : `${num(c)}<small> / </small>${num(x)}<small> tokens</small>`;
  $('m-meta-sub').innerHTML =
    (c === undefined
      ? ''
      : `Claude ${num(c)}（${(pr.claude || {}).skills} 個 skill）、Codex ${num(x)}（${(pr.codex || {}).skills} 個）。` +
        'plugin 與工具組都裝在 ~/.claude，Codex 讀不到。<br>') +
    (meta.avoidable_est_tokens
      ? `其中 ${num(meta.avoidable_est_tokens)} tokens 來自沒在用、也沒被你的設定引用的 plugin`
      : '沒有可避免的浪費');

  const instr = (h.metrics || {}).instruction_files || s.instruction_files || [];
  $('m-instr').textContent = instr.length ? instr.map((i) => i.lines).join(' / ') : '–';

  const u = state.updates;
  if (u) {
    const n = (u.summary.updates_available || 0) + (u.summary.toolkit_updates_available || 0);
    $('m-updates').textContent = num(n);
    $('m-updates-sub').textContent = `已比對 ${u.summary.checked} 個 plugin、${u.summary.toolkits_checked} 個工具組；${u.summary.unknown} 個無法比對`;
    $('tab-updates-count').textContent = n ? `(${n})` : '';
  }

  $('tab-findings-count').textContent = counts.blocking ? `(${counts.blocking})` : '';

  // Rows come from render.js so the breakdown can be tested without a browser.
  const rows = breakdownRows(counts);

  table(
    $('tbl-breakdown'),
    [
      { label: '類別', get: (r) => `<span class="tag ${r.cls}">${escapeHtml(r.k)}</span>` },
      { label: '數量', cls: 'num', get: (r) => num(r.n) },
      { label: '這代表什麼', get: (r) => `<span class="detail-text">${escapeHtml(r.why)}</span>` },
    ],
    rows,
  );

  $('vendor-sources').innerHTML = vendorSourcesHtml(h.vendor_by_source);

  $('meta-breakdown').innerHTML = metaBreakdownHtml(
    (h.metrics || {}).preloaded_skill_metadata,
    (h.inventory_counts || {}).skills,
  );
  const trend = trendHtml(state.history);
  $('trend-wrap').innerHTML = trend.chart;
  $('trend-sub').innerHTML = trend.caption;

  const cov = h.usage || {};
  $('coverage').innerHTML = cov.available
    ? renderCoverage(cov)
    : '<span class="muted">這份報告沒有使用量索引，因此「沒在用」的判斷被跳過而不是猜測。</span>';
}

/* ----- 預載 metadata：這些 token 是什麼、能不能省 ----- */

function renderCoverage(cov) {
  return cov.available
    ? `<div class="sub" style="margin:0 0 8px">「這個 plugin 有沒有在用」是從你的完整歷史算出來的，不是猜的。覆蓋率不足時相關判斷會被跳過。</div>
       <dl style="display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin:0;font-size:12.5px">
         <dt class="muted">覆蓋率</dt><dd class="mono">${cov.file_coverage_pct}% （${num(cov.files_read)} / ${num(cov.files_total ?? cov.transcripts_total)} 個檔案，含 ${num(cov.history_files_total ?? 0)} 個 history，${(cov.bytes_read / 1e9).toFixed(1)} GB）</dd>
         <dt class="muted">skill 呼叫</dt><dd class="mono">${num(cov.total_invocations)}</dd>
         <dt class="muted">MCP tool 呼叫</dt><dd class="mono">${num(cov.mcp_tool_calls)}</dd>
         <dt class="muted">subagent 啟動</dt><dd class="mono">${num(cov.agent_spawns)}</dd>
         <dt class="muted">Codex tool 呼叫</dt><dd class="mono">${num(cov.codex_tool_calls)}</dd>
         <dt class="muted">是否截斷</dt><dd class="mono">${cov.truncated ? '是（結論不可用於停用決策）' : '否'}</dd>
       </dl>`
    : '<span class="muted">這份報告沒有使用量索引，因此「沒在用」的判斷被跳過而不是猜測。</span>';
}

/* ---------------- findings ---------------- */

function renderFindings() {
  const all = (state.health || {}).findings || [];
  const sev = $('f-sev').value;
  const owner = $('f-owner').value;
  const cat = $('f-cat').value;
  const onlyBlocking = $('f-blocking').checked;
  const q = $('f-search').value.trim().toLowerCase();

  const cats = [...new Set(all.map((f) => f.category).filter(Boolean))];
  if (cats.length && $('f-cat').options.length === 1) {
    for (const c of cats.sort()) {
      const o = document.createElement('option');
      o.value = o.textContent = c;
      $('f-cat').append(o);
    }
  }

  const rows = all.filter((f) => {
    if (sev && f.severity !== sev) return false;
    if (owner && f.owner !== owner) return false;
    if (cat && f.category !== cat) return false;
    if (onlyBlocking && (f.waived || f.owner !== 'local' || f.severity === 'minor')) return false;
    if (q) {
      const hay = `${f.rule} ${f.path} ${f.detail} ${f.title}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  $('f-stats').textContent = `${rows.length} / ${all.length}`;
  table(
    $('tbl-findings'),
    [
      { label: '規則', cls: 'mono nowrap', get: (f) => `<a href="${f.spec}" target="_blank" rel="noreferrer">${f.rule}</a>` },
      {
        label: '要不要處理',
        get: (f) => actionLabelHtml(f),
      },
      {
        label: '嚴重度',
        get: (f) =>
          `<span class="tag ${f.severity}">${f.severity}</span>` +
          (f.owner === 'vendor' ? ' <span class="tag vendor">vendor</span>' : ''),
      },
      { label: '位置', cls: 'mono', get: (f) => escapeHtml(shortPath(f.location || f.path)) },
      {
        label: '說明',
        get: (f) =>
          `<div class="detail-text">${escapeHtml(f.detail)}</div>` +
          (f.remedy ? `<div class="remedy"><b>修法：</b>${escapeHtml(f.remedy)}</div>` : '') +
          (f.waiver_reason ? `<div class="remedy"><b>豁免理由：</b>${escapeHtml(f.waiver_reason)}</div>` : '') +
          whyNotBlockingHtml(f),
      },
      {
        label: '動作',
        get: (f) => {
          const info = ((state.fixes || {}).fixes || {})[f.key];
          const dis = session.allow_actions ? '' : 'disabled';
          // A waiver is available on every finding, fixable or not: deciding not
          // to fix something is itself a management action, and it was the one
          // the dashboard had no way to record.
          const waive = f.waived
            ? `<button class="linkish" data-unwaive="${escapeHtml(f.rule)}" data-path="${escapeHtml(f.path)}" ${dis}>取消豁免</button>`
            : `<button class="linkish" data-waive="${escapeHtml(f.rule)}" data-path="${escapeHtml(f.path)}" ${dis}>記錄豁免</button>`;
          let main = '';
          if (!info) main = '';
          else if (!info.fixable) {
            main = `<span class="muted" style="font-size:11.5px" title="${escapeHtml(info.why)}">需人工判斷</span>`;
          } else {
            main = `<button data-fixkey="${escapeHtml(f.key)}" ${dis} title="${escapeHtml(info.detail)}">${escapeHtml(info.label)}</button>`;
          }
          return `${main}<div style="margin-top:5px">${waive}</div>`;
        },
      },
    ],
    rows,
  );

  $('tbl-findings').onclick = async (ev) => {
    const w = ev.target.closest('button[data-waive], button[data-unwaive]');
    if (w) {
      try {
        if (w.hasAttribute('data-unwaive')) {
          await removeWaiver(w.dataset.unwaive, w.dataset.path);
        } else {
          askWaiverReason(w.dataset.waive, w.dataset.path);
        }
      } catch (e) {
        showError(`豁免失敗：${e.message}`);
      }
      return;
    }
    const btn = ev.target.closest('button[data-fixkey]');
    if (!btn) return;
    const cell = btn.closest('td');
    const row = btn.closest('tr');
    btn.disabled = true;
    // 修完會整表重畫，被點的那列通常就消失了。先把結果釘在畫面上，
    // 不然使用者只會看到列不見、沒有任何說明。
    cell.innerHTML = '<span class="running"><span class="spin"></span>修復中…</span>';
    try {
      const r = await runFix([btn.dataset.fixkey], false);
      if (row) row.classList.add('fixed-row');
      pinResult(
        `<b>✓ 已修復 ${r.applied} 項</b>
         <div>備份還原點 <code class="mono">${escapeHtml(r.backup || '')}</code> ——
         按錯了到 <b>同步狀態</b> 分頁最下面按「還原」。</div>
         <div class="sub">下表已重新掃描，修好的項目會從列表消失。</div>`,
        'ok',
      );
      await loadAll(true);
    } catch (e) {
      pinResult(`<b>✕ 修復失敗</b><div>${escapeHtml(e.message)}</div>`, 'bad');
      cell.innerHTML = '';
      btn.disabled = false;
      cell.appendChild(btn);
    }
  };
}

/* Waivers go through the same backed-up path as every other change.
 *
 * The reason is collected inline, not with window.prompt: a browser dialog
 * blocks the whole page, which is the same objection that removed confirm()
 * from the update flow. */
function askWaiverReason(rule, path) {
  const host = $('fix-result');
  host.hidden = false;
  host.className = 'panel result';
  host.innerHTML = `<div class="body">
      <b>為什麼決定不修 <span class="mono">${escapeHtml(rule)}</span>？</b>
      <div class="sub" style="margin:4px 0 8px">
        豁免是留在紀錄上的決定，不是靜音鍵 —— 理由會寫進 canonical/governance.json，
        之後任何人（包括未來的你）看得到當初為什麼這樣決定。
      </div>
      <input id="waive-reason" placeholder="例如：gstack 上游的問題，已回報，等它修" style="width:100%;max-width:560px" />
      <div style="margin-top:9px;display:flex;gap:8px">
        <button id="waive-go" class="primary">記錄豁免</button>
        <button id="waive-cancel">取消</button>
      </div>
    </div>`;
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  const input = $('waive-reason');
  input.focus();

  const submit = async () => {
    const reason = input.value.trim();
    if (!reason) {
      input.focus();
      showError('沒有寫理由，沒有記錄。');
      return;
    }
    try {
      await action('/api/actions/waive', { rule, path, reason });
      pinResult(
        `<b>✓ 已記錄豁免</b><div><span class="mono">${escapeHtml(rule)}</span> 之後不再列入判準。` +
          '理由存在 <code class="mono">canonical/governance.json</code>，隨時可以取消。</div>',
        'ok',
      );
      await loadAll(true);
    } catch (e) {
      showError(`豁免失敗：${e.message}`);
    }
  };
  $('waive-go').onclick = submit;
  input.onkeydown = (ev) => {
    if (ev.key === 'Enter') submit();
  };
  $('waive-cancel').onclick = () => {
    host.hidden = true;
  };
}

async function removeWaiver(rule, path) {
  await action('/api/actions/waive', { rule, path, remove: true });
  pinResult(
    `<b>✓ 已取消豁免</b><div><span class="mono">${escapeHtml(rule)}</span> 重新列入判準。</div>`,
    'ok',
  );
  await loadAll(true);
}

/** 釘一張結果卡在 findings 表格正上方，不會自己消失，也不需要捲到頁首才看得到。 */
function pinResult(html, kind) {
  const host = $('fix-result');
  host.hidden = false;
  host.className = `panel result ${kind}`;
  host.innerHTML = `<div class="body">${html}<button class="dismiss" type="button">知道了</button></div>`;
  host.querySelector('.dismiss').onclick = () => {
    host.hidden = true;
  };
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

for (const id of ['f-sev', 'f-owner', 'f-cat', 'f-blocking', 'f-search']) {
  $(id).addEventListener('input', renderFindings);
}

/* ---------------- fixes ---------------- */

async function loadFixes() {
  try {
    state.fixes = await api('/api/fixes');
  } catch {
    state.fixes = null;
  }
  const s = state.fixes;
  const bar = $('fix-stats');
  const applyBtn = $('btn-fix-apply');
  if (!s) {
    bar.textContent = '';
    return;
  }
  const n = s.auto_fixable_count;
  bar.innerHTML =
    `<b>${n}</b> 項可一鍵修復` +
    (s.individual_count ? ` · ${s.individual_count} 項只能單獨決定（下表的按鈕）` : '') +
    (session.allow_actions
      ? ''
      : ' · 唯讀模式，用 <code class="mono">serve --allow-actions</code> 啟動才能按');
  applyBtn.disabled = !n || !session.allow_actions;
  // Preview posts to /api/actions/fix, which always goes through the write
  // gate - so in read-only mode an enabled button only ever produced a 403.
  $('btn-fix-preview').disabled = !n || !session.allow_actions;
  renderFindings();
}

async function runFix(keys, dryRun) {
  return action('/api/actions/fix', { keys: keys || [], dry_run: !!dryRun });
}

$('btn-fix-preview').addEventListener('click', async () => {
  const b = $('btn-fix-preview');
  b.disabled = true;
  b.textContent = '產生中…';
  try {
    const r = await runFix([], true);
    $('fix-preview-panel').hidden = false;
    $('fix-preview').innerHTML = renderDiff(r.diff || '') +
      (r.remove_dirs && r.remove_dirs.length ? `\n\n刪除空目錄：\n${r.remove_dirs.join('\n')}` : '');
  } catch (e) {
    showError(`預覽失敗：${e.message}`);
  } finally {
    b.disabled = false;
    b.textContent = '預覽可一鍵修復的變更';
  }
});

$('btn-fix-apply').addEventListener('click', async () => {
  const n = (state.fixes || {}).auto_fixable_count || 0;
  if (!window.confirm(`套用 ${n} 項自動修復？\n每個檔案都會先備份，可從「同步狀態」分頁還原。`)) return;
  const b = $('btn-fix-apply');
  b.disabled = true;
  b.textContent = '修復中…';
  try {
    const r = await runFix([], false);
    await loadAll(true);
    alertPanel(`已修復 ${r.applied} 項；還原點 ${r.backup}`);
  } catch (e) {
    showError(`修復失敗：${e.message}`);
  } finally {
    b.disabled = false;
    b.textContent = '一鍵修復';
  }
});

/* AI-planned consolidation. The model proposes a plan; code validates it against
 * the file it claims to act on and rejects it whole if anything fails. Nothing
 * is written unless you press apply on a proposal that passed. */
$('btn-consolidate').addEventListener('click', async () => {
  const b = $('btn-consolidate');
  const host = $('consolidate-panel');
  b.disabled = true;
  b.textContent = '產生中（會呼叫模型）…';
  host.hidden = false;
  host.innerHTML = '<div class="body"><span class="running"><span class="spin"></span>模型正在提方案，接著由程式逐項驗證…</span></div>';
  try {
    const out = await action('/api/actions/consolidate', { apply: false });
    const rows = out.proposals || [];
    host.innerHTML = `<div class="body">
        <div class="sub" style="margin-bottom:8px">
          ${rows.length ? `${rows.length} 個方案` : '沒有適用的項目'}${
            out.cost_usd ? `　·　花費 $${out.cost_usd}` : ''
          }。模型只提方案，<b>程式驗證過才可能套用</b>；驗證不過的整案退回，不會部分套用。
        </div>
        ${
          out.error
            ? `<div class="result bad"><b>無法產生</b><div>${escapeHtml(out.error)}</div></div>`
            : rows
                .map(
                  (r) => `<div class="result ${r.ok ? 'ok' : 'bad'}">
              <b>${r.ok ? '✓ 通過驗證' : '✕ 已退回'}</b>
              <span class="mono" style="font-size:12px"> ${escapeHtml(r.rule)} · ${escapeHtml(shortPath(r.path))}</span>
              <div>${escapeHtml(r.summary || '')}</div>
              ${
                (r.rejected_because || []).length
                  ? `<div class="sub">退回原因：${r.rejected_because.map(escapeHtml).join('；')}</div>`
                  : ''
              }
              ${r.diff ? `<details><summary>看變更</summary><pre class="steps">${escapeHtml(r.diff)}</pre></details>` : ''}
            </div>`,
                )
                .join('')
        }
        <div class="sub" style="margin-top:8px">
          要真的套用，用 <code class="mono">python3 -m studio.cli consolidate --apply</code>，
          它一樣會先備份。
        </div>
      </div>`;
  } catch (e) {
    host.innerHTML = `<div class="body"><div class="result bad"><b>失敗</b><div>${escapeHtml(e.message)}</div></div></div>`;
  } finally {
    b.disabled = false;
    b.textContent = 'AI 整合建議（拆分過大的 skill / 處理重複）';
  }
});

/* ---------------- graph ---------------- */

let lastCounts = { nodes: 0, edges: 0 };

function updateGraphStats() {
  const { nodes, edges } = lastCounts;
  let msg = `${nodes} 節點 / ${edges} 連線 · 紅圈＝有阻斷性問題`;
  if (labelStats) {
    const hidden = labelStats.total - labelStats.shown;
    msg += hidden
      ? ` · 標籤 ${labelStats.shown}/${labelStats.total}，${hidden} 個因為會疊在一起被藏起來（滑鼠移到圓點上看名字，或放大）`
      : ` · 標籤 ${labelStats.shown}/${labelStats.total} 全部顯示`;
  }
  if (nodes > 260) {
    msg += ' · 節點很多，建議用左側篩選或勾「只看有連線的」';
  }
  const el = $('g-stats');
  if (el) el.textContent = msg;
}

const graph = createForceGraph($('graph'), {
  color: (n) => NODE_COLORS[n.kind] || '#888',
  radius: (n) => {
    if (n.kind === 'instruction') return 10;
    if (n.kind === 'plugin') return 5 + Math.min(6, (n.skill_count || 0) / 6);
    if (n.kind === 'skill') return 4 + Math.min(5, (n.body_lines || 0) / 130);
    return 6;
  },
  flag: (n) => flaggedPaths.has(n.path),
});

let flaggedPaths = new Set();

let labelStats = null;
graph.on('labels', (s) => {
  labelStats = s;
  updateGraphStats();
});

/* Focus: the whole graph is unreadable and a neighbourhood is not.
 *
 * 232 nodes in a force layout is a hairball - half the labels get suppressed
 * because they collide, and zooming in only magnifies one part of the knot
 * while pushing the rest off screen. The questions this page is actually asked
 * are local ("what is this?", "what pulls this in?"), so clicking a node
 * narrows the graph to that node and what it touches. Ten nodes lay out
 * legibly, every label fits, and the detail panel shows the file itself.
 */
let focusId = null;
let focusHops = 1;

function neighbourhood(g, rootId, hops) {
  const keep = new Set([rootId]);
  let frontier = new Set([rootId]);
  for (let d = 0; d < hops; d += 1) {
    const next = new Set();
    for (const e of g.edges) {
      if (frontier.has(e.source) && !keep.has(e.target)) next.add(e.target);
      if (frontier.has(e.target) && !keep.has(e.source)) next.add(e.source);
    }
    for (const id of next) keep.add(id);
    frontier = next;
    if (!next.size) break;
  }
  return keep;
}

graph.on('select', (n) => {
  if (!n) {
    focusId = null;
    renderGraph();
    renderNodeDetail(null);
    return;
  }
  // Clicking is how you narrow: the full graph answers no question well, and
  // the neighbourhood answers most of them.
  focusId = n.id;
  renderGraph();
  renderNodeDetail(n);
});

async function renderNodeDetail(n) {
  const el = $('detail');
  if (!n) {
    el.innerHTML =
      '<div class="empty">點任一個圓點：圖會縮到它和它直接相關的東西，這裡顯示它的內容。</div>';
    return;
  }
  const g = state.graph || { nodes: [], edges: [] };
  const rel = g.edges.filter((e) => e.source === n.id || e.target === n.id);
  const findings = ((state.health || {}).findings || []).filter((f) => f.path === n.path);
  const hits = ((state.usage || {}).counts || {})[String(n.label).toLowerCase()];

  const relHtml = rel.length
    ? rel
        .map((e) => {
          const other = e.source === n.id ? e.target : e.source;
          const node = g.nodes.find((x) => x.id === other);
          const dir = e.source === n.id ? '→' : '←';
          return `<li><span class="tag">${EDGE_LABELS[e.kind] || e.kind}</span> ${dir}
            <button class="linkish" data-goto="${escapeHtml(other)}">${escapeHtml(node ? node.label : other)}</button></li>`;
        })
        .join('')
    : '<li class="muted">沒有連線</li>';

  el.innerHTML =
    `<div class="detail-head">
       <div><b>${escapeHtml(n.label)}</b> <span class="tag">${escapeHtml(n.kind)}</span>
         ${n.runtime ? `<span class="tag">${escapeHtml(n.runtime)}</span>` : ''}
         ${typeof hits === 'number' ? `<span class="tag ok">用過 ${num(hits)} 次</span>` : ''}</div>
       <div class="detail-actions">
         <label class="chk" title="也顯示鄰居的鄰居">
           <input type="checkbox" id="d-hops" ${focusHops > 1 ? 'checked' : ''} /> 看兩層
         </label>
         <button id="d-clear">看全部</button>
       </div>
     </div>
     ${n.path ? `<div class="mono muted" style="font-size:11px;margin:2px 0 8px">${escapeHtml(shortPath(n.path))}</div>` : ''}
     ${
       findings.length
         ? `<div class="detail-findings">${findings
             .map((f) => `<span class="tag ${f.severity}">${f.rule}</span> ${escapeHtml(f.title)}`)
             .join('<br />')}</div>`
         : ''
     }
     <h3 class="detail-h">關聯 (${rel.length})</h3>
     <ul class="detail-rel">${relHtml}</ul>
     <h3 class="detail-h">內容</h3>
     <div id="d-body" class="md"><span class="running"><span class="spin"></span>讀取中…</span></div>`;

  $('d-clear').onclick = () => {
    focusId = null;
    renderGraph();
    graph.select(null);
  };
  $('d-hops').onchange = (ev) => {
    focusHops = ev.target.checked ? 2 : 1;
    renderGraph();
  };
  el.querySelectorAll('button[data-goto]').forEach((b) => {
    b.onclick = () => {
      const target = (state.graph.nodes || []).find((x) => x.id === b.dataset.goto);
      if (target) {
        focusId = target.id;
        renderGraph();
        renderNodeDetail(target);
      }
    };
  });

  const body = $('d-body');
  if (!n.path) {
    body.innerHTML = '<span class="muted">這個節點沒有對應的檔案。</span>';
    return;
  }
  try {
    const r = await api(`/api/file?path=${encodeURIComponent(shortPath(n.path))}`);
    body.innerHTML = renderMarkdown(r.text);
  } catch (e) {
    body.innerHTML = `<span class="muted">讀不到：${escapeHtml(e.message)}</span>`;
  }
}

function renderGraph() {
  const g = state.graph;
  if (!g) return;
  const kind = $('g-kind').value;
  const runtime = $('g-runtime').value;
  const onlyConnected = $('g-connected').checked;
  const q = $('g-search').value.trim().toLowerCase();

  let nodes = g.nodes.filter((n) => {
    if (kind && n.kind !== kind) return false;
    if (runtime && n.runtime !== runtime) return false;
    if (q && !String(n.label).toLowerCase().includes(q)) return false;
    return true;
  });
  const ids = new Set(nodes.map((n) => n.id));
  let edges = g.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  if (onlyConnected) {
    const linked = new Set(edges.flatMap((e) => [e.source, e.target]));
    nodes = nodes.filter((n) => linked.has(n.id));
  }

  if (focusId && g.nodes.some((n) => n.id === focusId)) {
    const keep = neighbourhood(g, focusId, focusHops);
    nodes = g.nodes.filter((n) => keep.has(n.id));
    const kept = new Set(nodes.map((n) => n.id));
    edges = g.edges.filter((e) => kept.has(e.source) && kept.has(e.target));
  } else if (focusId) {
    focusId = null; // the focused node was filtered away
  }

  flaggedPaths = new Set(
    ((state.health || {}).findings || [])
      .filter((f) => !f.waived && f.owner === 'local' && f.severity !== 'minor')
      .map((f) => f.path),
  );

  graph.render({ nodes, edges });
  // Fit only when focused: the overview is meant to be panned and zoomed by
  // hand, but a neighbourhood should arrive already framed.
  if (focusId) setTimeout(() => graph.fit(), 450);
  lastCounts = { nodes: nodes.length, edges: edges.length };
  updateGraphStats();

  $('legend').innerHTML = legendHtml(
    [...new Set(g.nodes.map((n) => n.kind))].sort(),
    g.edges.map((e) => e.kind),
    NODE_COLORS,
    EDGE_LABELS,
  );
}

for (const id of ['g-kind', 'g-runtime', 'g-connected', 'g-search']) {
  $(id).addEventListener('input', renderGraph);
}
$('g-expand').addEventListener('change', () => loadGraph($('g-expand').checked));
$('g-reset').addEventListener('click', () => graph.reset());

async function loadGraph(expand = false) {
  state.graph = await api(`/api/graph?expand=${expand ? 1 : 0}`);
  renderGraph();
}

/* ---------------- plugins & updates ---------------- */

function renderPlugins() {
  const u = state.updates;
  const plugins = u ? u.plugins : ((state.inventory || {}).plugins || []).map((p) => ({ ...p, note: '' }));

  table(
    $('tbl-toolkits'),
    [
      { label: '名稱', cls: 'mono', get: (t) => escapeHtml(t.name) },
      { label: '狀態', get: (t) => (t.update_available ? '<span class="tag important">有更新</span>' : t.update_available === false ? '<span class="tag ok">最新</span>' : '<span class="tag minor">未知</span>') },
      { label: '本機版本', cls: 'mono', get: (t) => escapeHtml(t.local_version || t.commit?.slice(0, 8) || '?') },
      { label: '遠端版本', cls: 'mono', get: (t) => escapeHtml(t.remote_version || '?') },
      { label: '管理的 skill', cls: 'num', get: (t) => t.manages_count },
      { label: '說明', get: (t) => `<span class="detail-text">${escapeHtml(t.note || '')}</span>` },
    ],
    (u ? u.toolkits : (state.inventory || {}).toolkits) || [],
  );

  table(
    $('tbl-plugins'),
    [
      { label: 'Plugin', cls: 'mono', get: (p) => escapeHtml(p.key) },
      { label: 'Runtime', get: (p) => `<span class="tag">${p.runtime}</span>` },
      { label: '啟用', get: (p) => (p.enabled ? '<span class="tag ok">是</span>' : '<span class="tag minor">否</span>') },
      { label: 'Skills', cls: 'num', get: (p) => p.skill_count ?? 0 },
      { label: '更新', get: (p) => (p.update_available ? '<span class="tag important">有更新</span>' : p.update_available === false ? '<span class="tag ok">最新</span>' : '<span class="tag minor">未知</span>') },
      { label: '說明', get: (p) => `<span class="detail-text">${escapeHtml(p.note || p.update_note || '')}</span>` },
    ],
    plugins.slice().sort((a, b) => Number(b.enabled) - Number(a.enabled) || (b.skill_count || 0) - (a.skill_count || 0)),
  );

  if (u) {
    $('u-stats').textContent = `${u.summary.updates_available} 個 plugin 與 ${u.summary.toolkit_updates_available} 個工具組有更新；${u.summary.unknown} 個無法比對（誠實標為未知，不當成最新）`;
    const gaps = $('u-gaps');
    if (gaps) gaps.innerHTML = updateGapsHtml(u);
  }

  // Updates run each package's own updater. The tool never reimplements one.
  const plan = (u && u.plan) || [];
  $('update-howto').innerHTML = plan.length
    ? `<div class="sub" style="margin:0 0 10px">更新一律呼叫套件自己的更新器：plugin 走 <code class="mono">claude plugin update</code>，git checkout 的工具組走它文件寫的 git 升級序列。本工具不自己實作套件管理。</div>` +
      plan
        .map(
          (i) => `<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line)">
            <div style="flex:1 1 auto;min-width:0">
              <b>${escapeHtml(i.target)}</b> <span class="tag">${i.kind}</span><br>
              <span class="mono" style="font-size:12px">${escapeHtml(String(i.from))} → ${escapeHtml(String(i.to))}</span>
              ${i.manages ? `<span class="muted" style="font-size:12px"> · 管理 ${i.manages} 個 skill</span>` : ''}
              <div class="sub" style="margin-top:2px"><code class="mono">${escapeHtml(i.method)}</code></div>
            </div>
            <div class="nowrap">${
              i.automatic
                ? `<button data-update="${escapeHtml(i.root ? `${i.target}@${i.root}` : i.target)}" class="primary" ${
                    session.allow_actions ? '' : 'disabled title="唯讀模式：用 --allow-actions 啟動才能按"'
                  }>更新</button>`
                : '<span class="muted" style="font-size:12px">需手動</span>'
            }</div>
          </div>
          <div data-rowstate="${escapeHtml(i.root ? `${i.target}@${i.root}` : i.target)}"></div>`,
        )
        .join('') +
      `<div class="sub" style="margin-top:10px">plugin 更新後 Claude Code 需重啟才會套用。工具組升級會先記下目前的 commit，失敗時可用它回退。</div>`
    : '<span class="muted">目前沒有偵測到可用更新。到上方按「檢查雲端更新」。</span>';

  $('update-howto').onclick = (ev) => {
    const go = ev.target.closest('button[data-go]');
    if (go) return runUpdate(go.dataset.go);
    const no = ev.target.closest('button[data-cancel]');
    if (no) return setRowState(no.dataset.cancel, '');
    const btn = ev.target.closest('button[data-update]');
    if (!btn) return;
    // 用頁內確認，不用 window.confirm：瀏覽器對話框會擋住整頁，也看不出後面在跑什麼。
    const t = btn.dataset.update;
    setRowState(
      t,
      `<div class="confirm">
         <b>要更新 ${escapeHtml(t)} 嗎？</b>
         這會執行該套件自己的更新器，過程可能要 <b>數十秒到數分鐘</b>，期間這一列會顯示進度。
         <div class="row"><button data-go="${escapeHtml(t)}" class="primary">確定更新</button>
         <button data-cancel="${escapeHtml(t)}">取消</button></div>
       </div>`,
    );
  };
}

/** 把某一列的狀態區塊換掉。列在 renderPlugins 重畫後仍能對上，因為是用 target 當 key。 */
function setRowState(target, html) {
  const el = document.querySelector(`[data-rowstate="${CSS.escape(target)}"]`);
  if (el) el.innerHTML = html;
}

async function runUpdate(target) {
  const started = Date.now();
  let done = false;
  // 有進度才知道它在跑。沒有這個，長時間的升級看起來就跟當掉一樣。
  const tick = () => {
    if (done) return;
    const s = Math.round((Date.now() - started) / 1000);
    setRowState(target, updateRunningHtml(target, s));
  };
  tick();
  const timer = setInterval(tick, 1000);

  try {
    const r = await action('/api/actions/update', { target });
    const res = (r.results || [])[0] || {};
    const secs = Math.round((Date.now() - started) / 1000);
    done = true;
    clearInterval(timer);
    setRowState(
      target,
      `<div class="result ${res.ok ? 'ok' : 'bad'}">
         <b>${res.ok ? '✓ 更新完成' : '✕ 更新失敗'}</b>（耗時 ${secs}s）
         <div>${escapeHtml(res.message || '沒有回傳訊息')}</div>
         ${res.needs_restart ? '<div class="warn">要<b>重開 Claude Code</b> 才會套用。</div>' : ''}
         ${res.restore_hint ? `<div class="sub">要回退：<code class="mono">${escapeHtml(res.restore_hint)}</code></div>` : ''}
         ${
           (res.steps || []).length
             ? `<details><summary>看它實際執行了什麼（${res.steps.length} 步）</summary>
                <pre class="steps">${escapeHtml(
                  res.steps
                    .map((s) => `$ ${s.cmd}\n  rc=${s.rc}${s.stderr ? '\n  ' + String(s.stderr).slice(0, 500) : ''}`)
                    .join('\n\n'),
                )}</pre></details>`
             : ''
         }
       </div>`,
    );
    state.updates = await api('/api/updates?fresh=1');
    renderOverview();
  } catch (e) {
    done = true;
    clearInterval(timer);
    setRowState(target, `<div class="result bad"><b>✕ 更新失敗</b><div>${escapeHtml(e.message)}</div></div>`);
  }
}

$('btn-updates').addEventListener('click', async () => {
  const b = $('btn-updates');
  b.disabled = true;
  b.textContent = '檢查中…';
  try {
    state.updates = await api('/api/updates?fresh=1');
    renderPlugins();
    renderOverview();
  } catch (e) {
    showError(`更新檢查失敗：${e.message}`);
  } finally {
    b.disabled = false;
    b.textContent = '檢查雲端更新';
  }
});

/* ---------------- inventory ---------------- */

function renderInventory() {
  const inv = state.inventory;
  if (!inv) return;
  const kind = $('i-kind').value;
  const origin = $('i-origin').value;
  const q = $('i-search').value.trim().toLowerCase();
  const cards = $('i-cards').checked;

  $('i-howto').innerHTML = howtoHtml(kind);

  $('i-origin').disabled = kind !== 'skills';
  $('i-cards-panel').hidden = !cards;
  $('i-table-panel').hidden = cards;

  let rows = inv[kind] || [];
  if (kind === 'skills' && origin === 'usable') {
    rows = rows.filter((r) => r.origin !== 'orphan-library');
  } else if (kind === 'skills' && origin) {
    rows = rows.filter((r) => r.origin === origin);
  }
  if (q) rows = rows.filter((r) => JSON.stringify(r).toLowerCase().includes(q));

  if (cards) {
    $('i-stats').textContent = `${rows.length} 筆`;
    $('cards-inventory').innerHTML = catalogueSections(kind, rows, { expandAll: Boolean(q) });
    return;
  }

  const columnsFor = {
    skills: [
      { label: '名稱', cls: 'mono', get: (r) => escapeHtml(r.name || r.dir_name) },
      { label: '來源', get: (r) => `<span class="tag">${r.origin}</span>` },
      { label: 'Runtime', get: (r) => `<span class="tag">${r.runtime}</span>` },
      { label: 'Body 行數', cls: 'num', get: (r) => (r.body_lines > 500 ? `<span class="tag important">${r.body_lines}</span>` : r.body_lines) },
      { label: 'Desc 長度', cls: 'num', get: (r) => r.description.length },
      { label: '路徑', cls: 'mono', get: (r) => escapeHtml(shortPath(r.path)) },
    ],
    instructions: [
      { label: '檔案', cls: 'mono', get: (r) => escapeHtml(shortPath(r.path)) },
      { label: 'Runtime', get: (r) => `<span class="tag">${r.runtime}</span>` },
      { label: '行數', cls: 'num', get: (r) => (r.lines > 200 ? `<span class="tag important">${r.lines}</span>` : r.lines) },
      { label: 'Bytes', cls: 'num', get: (r) => num(r.bytes) },
    ],
    workflows: [
      { label: '檔案', cls: 'mono', get: (r) => escapeHtml(shortPath(r.path)) },
      { label: 'Runtime', get: (r) => `<span class="tag">${r.runtime}</span>` },
      { label: '行數', cls: 'num', get: (r) => r.lines },
    ],
    commands: [
      { label: '名稱', cls: 'mono', get: (r) => `/${escapeHtml(r.name)}` },
      { label: 'Runtime', get: (r) => `<span class="tag">${r.runtime}</span>` },
      { label: '行數', cls: 'num', get: (r) => r.lines },
      { label: '路徑', cls: 'mono', get: (r) => escapeHtml(shortPath(r.path)) },
    ],
    agents: [
      { label: '名稱', cls: 'mono', get: (r) => escapeHtml(r.name) },
      { label: '行數', cls: 'num', get: (r) => r.lines },
      { label: '路徑', cls: 'mono', get: (r) => escapeHtml(shortPath(r.path)) },
    ],
    hooks: [
      { label: '事件', cls: 'mono', get: (r) => escapeHtml(r.event) },
      { label: 'Matcher', cls: 'mono', get: (r) => escapeHtml(r.matcher || '*') },
      { label: 'if 條件', cls: 'mono', get: (r) => escapeHtml(r.if_rule || '（無）') },
      { label: '注入內容', get: (r) => `<span class="detail-text">${escapeHtml((r.injects || '').slice(0, 160))}</span>` },
    ],
  };

  $('i-stats').textContent = `${rows.length} 筆`;
  table($('tbl-inventory'), columnsFor[kind], rows);
}

for (const id of ['i-kind', 'i-origin', 'i-search', 'i-cards']) $(id).addEventListener('input', renderInventory);

/* Reading a file was the missing half of "read and manage": the catalogue could
 * tell you a skill's trigger but never let you see what it actually does. */
$('cards-inventory').addEventListener('click', async (ev) => {
  const q = ev.target.closest('button[data-quarantine]');
  if (q) return quarantineFile(q.dataset.quarantine, q);
  const btn = ev.target.closest('button[data-peek]');
  if (!btn) return;
  const path = btn.dataset.peek;
  const host = $('peek');
  host.hidden = false;
  host.innerHTML = '<div class="body"><span class="running"><span class="spin"></span>讀取中…</span></div>';
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  try {
    const r = await api(`/api/file?path=${encodeURIComponent(path)}`);
    renderPeek(host, r.path, r.text);
  } catch (e) {
    host.innerHTML = `<div class="body"><div class="result bad"><b>讀不到</b><div>${escapeHtml(e.message)}</div></div></div>`;
  }
});

/* Viewing, then editing, in one panel.
 *
 * The catalogue could show you that a description had no trigger and then leave
 * you to find the file yourself. Saving goes through the same backed-up change
 * set as every other write, so an edit made here is as reversible as a fix -
 * and the server refuses vendor-owned and canonical-generated files with the
 * reason, because those two edits look like they work and then disappear.
 */
function renderPeek(host, path, text) {
  const lines = text.split('\n').length;
  host.innerHTML = `<div class="body">
      <div class="peek-head">
        <b class="mono">${escapeHtml(shortPath(path))}</b>
        <span class="muted" style="font-size:12px" id="peek-lines">${num(lines)} 行</span>
        <span class="grow"></span>
        <button id="peek-edit" type="button">編輯</button>
        <button class="dismiss" type="button">關閉</button>
      </div>
      <div id="peek-body" class="md">${renderMarkdown(text)}</div>
    </div>`;
  host.querySelector('.dismiss').onclick = () => {
    host.hidden = true;
  };
  $('peek-edit').onclick = () => startEdit(host, path, text);
}

function startEdit(host, path, text) {
  // Editing gets the raw source: what you save is bytes, not rendered output.
  const body = $('peek-body');
  body.outerHTML = `<textarea id="peek-edit-area" spellcheck="false">${escapeHtml(text)}</textarea>`;
  const head = host.querySelector('.peek-head');
  $('peek-edit').outerHTML =
    '<button id="peek-save" class="primary" type="button">儲存</button>' +
    '<button id="peek-cancel" type="button">取消</button>';
  head.insertAdjacentHTML('beforeend', '<div id="peek-msg" class="sub" style="flex-basis:100%"></div>');

  $('peek-cancel').onclick = () => renderPeek(host, path, text);
  $('peek-save').onclick = async () => {
    const next = $('peek-edit-area').value;
    if (next === text) {
      $('peek-msg').textContent = '沒有變更。';
      return;
    }
    $('peek-msg').textContent = '儲存中…';
    try {
      const r = await action('/api/actions/edit', { path, text: next });
      renderPeek(host, path, next);
      host.querySelector('.peek-head').insertAdjacentHTML(
        'beforeend',
        `<div class="sub" style="flex-basis:100%">✓ 已儲存。還原點 <span class="mono">${escapeHtml(
          (r.backup || '').split('/').pop(),
        )}</span> —— 到「同步狀態」分頁可以還原。</div>`,
      );
      await loadAll(true);
    } catch (e) {
      $('peek-msg').innerHTML = `<span class="bad">存不了：${escapeHtml(e.message)}</span>`;
    }
  };
}

/* Moving a file out is the action the catalogue was missing: CB007 could report
 * 71 never-invoked skills and there was no way to act on any of them.
 *
 * Quarantine rather than delete, and per file rather than in bulk: "never
 * invoked" means "has not come up yet", not "useless", so this stays one
 * deliberate decision at a time. */
async function quarantineFile(path, btn) {
  const host = $('peek');
  host.hidden = false;
  host.innerHTML = `<div class="body">
      <b>把 <span class="mono">${escapeHtml(path)}</span> 移出設定目錄？</b>
      <div class="sub" style="margin:4px 0 8px">
        會複製到本 repo 的 <code class="mono">var/quarantine/</code> 再從設定樹移除，
        <b>不是刪除</b>，而且有還原點。agent 之後就看不到它了。
      </div>
      <div style="display:flex;gap:8px">
        <button id="q-go" class="primary">確定隔離</button>
        <button id="q-cancel">取消</button>
      </div>
    </div>`;
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  $('q-cancel').onclick = () => {
    host.hidden = true;
  };
  $('q-go').onclick = async () => {
    host.innerHTML = '<div class="body"><span class="running"><span class="spin"></span>隔離中…</span></div>';
    try {
      const r = await action('/api/actions/quarantine', { path });
      host.innerHTML = `<div class="body"><div class="result ok">
          <b>✓ 已隔離</b>
          <div>副本在 <span class="mono">${escapeHtml(shortPath(r.copy))}</span></div>
          <div class="sub">要拿回來：到「同步狀態」分頁按還原點 <span class="mono">${escapeHtml((r.backup || '').split('/').pop())}</span> 的「還原」。</div>
          <button class="dismiss" type="button">知道了</button>
        </div></div>`;
      host.querySelector('.dismiss').onclick = () => {
        host.hidden = true;
      };
      await loadAll(true);
    } catch (e) {
      host.innerHTML = `<div class="body"><div class="result bad"><b>隔離失敗</b><div>${escapeHtml(e.message)}</div></div></div>`;
    }
  };
}

/* ---------------- sync ---------------- */

function renderDiff(text) {
  if (!text.trim()) return '<span class="muted">沒有待套用的差異。</span>';
  return text
    .split('\n')
    .map((l) => {
      const e = escapeHtml(l);
      if (l.startsWith('+++') || l.startsWith('---') || l.startsWith('@@')) return `<span class="hdr">${e}</span>`;
      if (l.startsWith('+')) return `<span class="add">${e}</span>`;
      if (l.startsWith('-')) return `<span class="del">${e}</span>`;
      return e;
    })
    .join('\n');
}

async function loadSync() {
  try {
    const s = await api('/api/sync-preview');
    $('sync-status').innerHTML =
      (s.in_sync
        ? `<span class="tag ok">已同步</span> ${syncTargetSummary(s.targets)} 都與 canonical 一致。`
        : `<span class="tag important">有差異</span> ${s.pending.length} 個檔案與 canonical 不一致。`) +
      (s.errors.length ? `<div class="banner err" style="margin-top:8px">${s.errors.map(escapeHtml).join('<br>')}</div>` : '') +
      `<div class="sub">要改規則就改 <code>canonical/</code> 底下的來源檔。直接改產生出來的檔案會被規則 MR003 抓到。</div>` +
      (s.in_sync
        ? ''
        : `<div style="margin-top:10px;display:flex;gap:9px;align-items:center;flex-wrap:wrap">
             <button id="btn-sync-apply" class="primary" ${session.allow_actions ? '' : 'disabled'}>套用同步</button>
             <span class="muted" style="font-size:12px">${
               session.allow_actions
                 ? '會先自動備份，可從下方還原點復原。'
                 : '唯讀模式。用 <code class="mono">python3 -m studio.cli serve --allow-actions</code> 啟動即可從這裡套用。'
             }</span>
           </div>
           <div class="sub">等同指令：<code class="mono">${escapeHtml(s.apply_command)}</code></div>`) +
      (s.pending.length
        ? `<ul style="font-size:12.5px">${s.pending.map((p) => `<li class="mono">${escapeHtml(shortPath(p.path))} <span class="muted">+${p.added} -${p.removed}</span></li>`).join('')}</ul>`
        : '');
    $('sync-diff').innerHTML = renderDiff(s.diff || '');
  } catch (e) {
    $('sync-status').innerHTML = `<span class="tag critical">錯誤</span> ${escapeHtml(e.message)}`;
    $('sync-diff').textContent = '';
  }
  const applyBtn = $('btn-sync-apply');
  if (applyBtn) {
    applyBtn.addEventListener('click', async () => {
      applyBtn.disabled = true;
      applyBtn.textContent = '套用中…';
      try {
        const r = await action('/api/actions/sync-apply');
        showError(null);
        await loadAll(true);
        alertPanel(`已套用 ${r.applied} 個檔案；還原點 ${r.backup}`);
      } catch (e) {
        showError(`套用失敗：${e.message}`);
      } finally {
        applyBtn.disabled = false;
        applyBtn.textContent = '套用同步';
      }
    });
  }

  try {
    const backups = await api('/api/backups');
    table(
      $('tbl-backups'),
      [
        {
          label: '還原點',
          cls: 'mono',
          get: (b) =>
            `<button class="linkish" data-diff="${escapeHtml(b.id)}" title="看這次改了什麼">${escapeHtml(b.id)}</button>`,
        },
        { label: '變更集', get: (b) => escapeHtml(b.change_set || '') },
        { label: '檔案數', cls: 'num', get: (b) => (b.changes || []).length },
        { label: '說明', get: (b) => `<span class="detail-text">${escapeHtml(b.description || '')}</span>` },
        {
          label: '',
          get: (b) =>
            session.allow_actions
              ? `<button data-rollback="${escapeHtml(b.id)}">還原</button>`
              : `<span class="muted mono" style="font-size:11px">rollback ${escapeHtml(b.id)}</span>`,
        },
      ],
      backups,
    );
    // Assigned, not added: loadSync() runs on load, on refresh and after every
    // apply, and addEventListener stacked a new handler each time. One click
    // then fired N concurrent rollbacks of the same backup.
    $('tbl-backups').onclick = async (ev) => {
      const show = ev.target.closest('button[data-diff]');
      if (show) return showBackupDiff(show.dataset.diff, show);
      const btn = ev.target.closest('button[data-rollback]');
      if (!btn) return;
      const id = btn.dataset.rollback;
      // Restoring overwrites live config, so it asks first.
      if (!window.confirm(`還原 ${id}？\n這會把備份當時的檔案內容寫回 ~/.claude 與 ~/.codex。`)) return;
      btn.disabled = true;
      try {
        const r = await action('/api/actions/rollback', { id });
        await loadAll(true);
        alertPanel(`已還原 ${(r.restored || []).length} 個檔案`);
      } catch (e) {
        showError(`還原失敗：${e.message}`);
      } finally {
        btn.disabled = false;
      }
    };
  } catch {
    /* backups are optional */
  }
}

function alertPanel(msg) {
  const el = $('error');
  el.hidden = false;
  el.className = 'banner';
  el.textContent = msg;
  setTimeout(() => {
    el.hidden = true;
    el.className = 'banner err';
  }, 6000);
}

/* ---------------- specs & schedule ---------------- */

async function loadSchedule() {
  try {
    $('schedule-body').innerHTML = scheduleHtml(await api('/api/schedule'));
  } catch (e) {
    $('schedule-body').innerHTML = `<span class="muted">讀不到排程狀態：${escapeHtml(e.message)}</span>`;
  }
}

async function runSpecs() {
  const b = $('btn-specs');
  b.disabled = true;
  b.textContent = '抓取中…';
  $('specs-body').innerHTML = '<span class="running"><span class="spin"></span>正在抓取每一份被引用的官方文件…</span>';
  try {
    state.specs = await api('/api/specs');
    $('specs-body').innerHTML = specsHtml(state.specs);
    const n = (state.specs.changed || []).length + (state.specs.unreachable || []).length;
    $('tab-specs-count').textContent = n ? `(${n})` : '';
    $('specs-stats').textContent =
      `${state.specs.specs.length} 份文件；${(state.specs.changed || []).length} 份有變動`;
  } catch (e) {
    $('specs-body').innerHTML = `<div class="result bad"><b>抓取失敗</b><div>${escapeHtml(e.message)}</div></div>`;
  } finally {
    b.disabled = false;
    b.textContent = '檢查規範是否更新';
  }
}

$('btn-specs').addEventListener('click', runSpecs);

$('btn-specs-review').addEventListener('click', async () => {
  const changed = ((state.specs || {}).changed || []).concat(((state.specs || {}).new) || []);
  if (!changed.length) {
    alertPanel('沒有變動的規範需要分析。先按「檢查規範是否更新」。');
    return;
  }
  const b = $('btn-specs-review');
  b.disabled = true;
  b.textContent = '分析中（會呼叫模型）…';
  try {
    // Reviewing is the only part that calls a model, and it produces an opinion
    // about the rules - never an edit to them.
    const out = await action('/api/actions/specs-review', { urls: changed });
    $('specs-body').innerHTML =
      (out.reviews || []).map(specReviewHtml).join('') + specsHtml(state.specs);
  } catch (e) {
    showError(`分析失敗：${e.message}`);
  } finally {
    b.disabled = false;
    b.textContent = '分析變動對規則的影響（用 AI）';
  }
});

$('btn-specs-accept').addEventListener('click', async () => {
  const b = $('btn-specs-accept');
  b.disabled = true;
  try {
    const out = await action('/api/actions/specs-accept', {});
    alertPanel(`已把 ${(out.recorded || []).length} 份文件記為已檢視。`);
    await runSpecs();
  } catch (e) {
    showError(`記錄失敗：${e.message}`);
  } finally {
    b.disabled = false;
  }
});

/* ---------------- load ---------------- */

async function loadAll(fresh = false) {
  showError(null);
  // 重新掃描要好幾秒。沒有這個指示，頁面在這段時間看起來就像沒反應。
  setBusy(fresh ? '重新掃描設定中…' : '載入中…');
  try {
    return await loadAllInner(fresh);
  } finally {
    setBusy(null);
  }
}

function setBusy(label) {
  let el = document.getElementById('busy');
  if (!label) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = document.createElement('div');
    el.id = 'busy';
    document.body.appendChild(el);
  }
  el.innerHTML = `<span class="spin"></span>${escapeHtml(label)}`;
}

async function loadAllInner(fresh = false) {
  try {
    session = await api('/api/session');
  } catch {
    session = { token: null, allow_actions: false };
  }
  const q = fresh ? '?fresh=1' : '';
  const [summary, health, inventory, history, usage] = await Promise.all([
    api(`/api/summary${q}`),
    api(`/api/health${q}`),
    api(`/api/inventory${q}`),
    api('/api/history'),
    // Read from the cached index, so ordering the catalogue by what you use
    // costs nothing. Never fatal: a page that will not draw because a count is
    // missing is worse than a page drawn in an arbitrary order.
    api('/api/usage-map').catch(() => ({ available: false, counts: {} })),
  ]);
  state.summary = summary;
  state.usage = usage;
  setUsageCounts((usage || {}).counts);
  state.health = health;
  state.inventory = inventory;
  state.history = history;
  // Attach rule categories so the findings filter can group by them.
  try {
    const rules = await api('/api/rules');
    const cat = new Map(rules.map((r) => [r.code, r.category]));
    for (const f of state.health.findings || []) f.category = cat.get(f.rule) || 'other';
  } catch {
    /* categories are cosmetic */
  }
  renderOverview();
  renderFindings();
  renderInventory();
  renderPlugins();
  await loadFixes();
  await loadGraph($('g-expand').checked);
  loadSync();

}

$('btn-refresh').addEventListener('click', async () => {
  const b = $('btn-refresh');
  b.disabled = true;
  b.textContent = '掃描中…';
  try {
    await loadAll(true);
  } catch (e) {
    showError(e.message);
  } finally {
    b.disabled = false;
    b.textContent = '重新掃描';
  }
});

$('btn-check').addEventListener('click', async () => {
  const b = $('btn-check');
  b.disabled = true;
  b.textContent = '健檢中（會掃全部歷史，約需 1 分鐘）…';
  try {
    // action(), not api(): this route is token-gated, and calling it without
    // the header made the button return 403 every time.
    state.health = await action('/api/health/run', {});
    state.history = await api('/api/history');
    const rules = await api('/api/rules');
    const cat = new Map(rules.map((r) => [r.code, r.category]));
    for (const f of state.health.findings || []) f.category = cat.get(f.rule) || 'other';
    renderOverview();
    renderFindings();
    renderGraph();
  } catch (e) {
    showError(`健檢失敗：${e.message}`);
  } finally {
    b.disabled = false;
    b.textContent = '執行健檢';
  }
});

loadAll().catch((e) => showError(e.message));

/* Expand a restore point in place, like a commit in a file list. */
async function showBackupDiff(id, btn) {
  const row = btn.closest('tr');
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('diff-row')) {
    existing.remove();
    return;
  }
  const tr = document.createElement('tr');
  tr.className = 'diff-row';
  const td = document.createElement('td');
  td.colSpan = row.children.length;
  td.innerHTML = '<span class="running"><span class="spin"></span>讀取差異…</span>';
  tr.append(td);
  row.after(tr);
  try {
    const d = await api(`/api/backup?id=${encodeURIComponent(id)}`);
    td.innerHTML = backupDiffHtml(d);
  } catch (e) {
    td.innerHTML = `<span class="muted">讀不到：${escapeHtml(e.message)}</span>`;
  }
}
