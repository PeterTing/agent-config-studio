/* Pure HTML builders.
 *
 * Everything here takes data and returns a string. No DOM, no fetch, no state.
 * That is the whole point: the dashboard's judgement calls - which skills are
 * safe to tell you to invoke, which findings need action, what the token figure
 * actually counts - live in this file so they can be tested without a browser.
 *
 * Loaded as a plain script before app.js, and as a CommonJS module by the Node
 * test runner. No bundler, no npm, matching the rest of the project.
 */

const num = (n) => (typeof n === 'number' ? n.toLocaleString('en-US') : '–');
const shortPath = (p) => (p || '').replace(/^\/Users\/[^/]+/, '~');

function escapeHtml(s) {
  return String(s ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
}

/* ---------------- preloaded metadata ---------------- */

const BUCKET_LABEL = {
  'local:claude': ['你自己寫的（Claude）', '你手寫在 ~/.claude/skills/ 的 skill。要省只能自己刪或縮描述。'],
  'local:codex': ['你自己寫的（Codex）', '你手寫在 ~/.codex/skills/ 的 skill。'],
  toolkit: ['工具組帶的', 'gstack 這類 git 工具組安裝的。要省就是不裝那個工具組。'],
  plugin: ['Plugin 帶的', '已啟用 plugin 帶進來的。停用沒在用的 plugin 就能直接省掉。'],
};

/**
 * The token figure counts only skills that actually load, so the skill count
 * shown beside it must be the sum of the buckets - not the inventory total,
 * which includes an unreferenced library that costs nothing.
 */
function metaBreakdownHtml(metrics, inventorySkillCount) {
  if (!metrics) return '';
  const buckets = Object.entries(metrics.by_bucket || {}).sort((a, b) => b[1].bytes - a[1].bytes);
  const totalBytes = metrics.total_bytes || 1;
  const avoidTok = metrics.avoidable_est_tokens || 0;
  const counted = buckets.reduce((n, [, v]) => n + v.skills, 0);
  const notLoaded = Math.max(0, (inventorySkillCount || 0) - counted);

  return `
    <p class="sub" style="margin:0 0 10px">
      每個 skill 的 <b>名稱</b>和<b>描述</b>（不是內文）會在每次對話開始時全部載入，
      agent 才知道有哪些 skill 可用、什麼時候該用。內文只有在真的呼叫該 skill 時才讀。
      <b>每個 runtime 只載入自己讀得到的</b>：Claude ${num(((metrics.per_runtime||{}).claude||{}).est_tokens)} tokens、
      Codex ${num(((metrics.per_runtime||{}).codex||{}).est_tokens)} tokens。
      plugin 與工具組都裝在 <code class="mono">~/.claude</code>，Codex 讀不到，
      所以<b>沒有任何一次對話會付兩者相加的 ${num(metrics.total_est_tokens)}</b>。
      下表是 ${num(counted)} 個會被載入的 skill 依來源拆開。${
        notLoaded
          ? `（你機器上另外還有 ${num(notLoaded)} 個 skill 在 agent 讀不到的目錄裡，那些完全不花錢，也沒算進來。）`
          : ''
      }
    </p>
    <table style="margin-bottom:10px">
      <thead><tr><th>來源</th><th class="num">skill 數</th><th class="num">約 tokens</th><th>佔比</th><th>能不能省</th></tr></thead>
      <tbody>${buckets
        .map(([k, v]) => {
          const [label, how] = BUCKET_LABEL[k] || [k, ''];
          const pct = Math.round((v.bytes / totalBytes) * 100);
          return `<tr>
            <td>${escapeHtml(label)}</td>
            <td class="num mono">${num(v.skills)}</td>
            <td class="num mono">${num(Math.round(v.bytes / 4))}</td>
            <td><div class="barwrap"><div class="bar" style="width:${pct}%"></div><span>${pct}%</span></div></td>
            <td><span class="detail-text">${escapeHtml(how)}</span></td>
          </tr>`;
        })
        .join('')}</tbody>
    </table>
    <div class="sub">
      ${
        avoidTok > 0
          ? `<span class="tag important">可省 ${num(avoidTok)} tokens</span> 來自沒在用又沒被你的設定引用的 plugin。到 Findings 分頁看 <code class="mono">CB001</code>。`
          : `<span class="tag ok">沒有可避免的浪費</span> 剩下的都是你正在用的東西的必要成本 ——
             要再降只能<b>真的少裝一些 skill</b>，或把描述寫短一點。這不是問題，是帳單。`
      }
    </div>`;
}

/* ---------------- health trend ---------------- */

function trendHtml(history) {
  const hist = (history || []).slice(-60);
  if (!hist.length) return { chart: '<span class="muted">尚無歷史紀錄。</span>', caption: '' };

  const max = Math.max(1, ...hist.map((p) => (p.counts || {}).blocking || 0));
  const fails = hist.filter((p) => p.verdict !== 'PASS').length;
  const last = hist[hist.length - 1];

  const chart = `
    <div class="trend2">
      <div class="ylab"><span>${escapeHtml(max)}</span><span>0</span></div>
      <div class="bars">${hist
        .map((p) => {
          const b = (p.counts || {}).blocking || 0;
          const pass = p.verdict === 'PASS';
          const pct = b === 0 ? 0 : Math.max(8, Math.round((b / max) * 100));
          const when = (p.generated_at || '').replace('T', ' ').slice(0, 16);
          return `<div class="slot" title="${escapeHtml(when)} ／ ${pass ? '通過' : '未通過'}：${escapeHtml(b)} 項待處理">
              <div class="fill ${pass ? 'pass' : 'fail'}" style="height:${pass ? 0 : pct}%"></div>
            </div>`;
        })
        .join('')}</div>
    </div>
    <div class="trend-axis"><span>${escapeHtml((hist[0].generated_at || '').slice(0, 10))}（最舊）</span><span>${escapeHtml(
      (last.generated_at || '').slice(0, 10),
    )}（最新）</span></div>`;

  // Says where the failures actually are, rather than asserting they are old.
  // The caption used to claim "都在早期" unconditionally, and it kept saying it
  // on a history whose two most recent runs had both failed - which is the one
  // case where a reader needs to be told something different.
  const lastFailIdx = hist.map((p) => p.verdict !== 'PASS').lastIndexOf(true);
  let status;
  if (!fails) {
    status = '全部通過';
  } else if (last.verdict !== 'PASS') {
    // num() formats and escapes; the value comes from a JSON file on disk, and
    // "it is our own file" is not a reason to interpolate it raw.
    const b = num((last.counts || {}).blocking || 0);
    status = `有 <b>${escapeHtml(fails)}</b> 次未通過，<b>包含最新這次</b>（${escapeHtml(b)} 項待處理）`;
  } else {
    const since = hist.length - 1 - lastFailIdx;
    const when = escapeHtml((hist[lastFailIdx].generated_at || '').slice(0, 10));
    status = `有 <b>${escapeHtml(fails)}</b> 次未通過，最近一次在 ${when}，之後連續 ${escapeHtml(since)} 次通過`;
  }
  const caption = `
    一根 = 一次健檢，左舊右新，共 ${escapeHtml(hist.length)} 次。<b>柱子越高代表當時待處理的問題越多</b>；
    貼齊底線的綠色細線代表那次是 0 問題、通過。滑鼠移上去看日期與數量。
    目前 ${status}。`;

  return { chart, caption };
}

/* ---------------- findings ---------------- */

function blocks(f) {
  return !f.waived && f.owner === 'local' && f.severity !== 'minor';
}

function actionLabelHtml(f) {
  if (blocks(f)) return '<span class="tag critical">要你處理</span>';
  if (f.waived) return '<span class="tag ok">已豁免</span>';
  if (f.owner === 'vendor') return '<span class="tag vendor">不用，不是你的</span>';
  return '<span class="tag minor">可選</span>';
}

/**
 * "important, but you do not need to act" reads as a contradiction, so the
 * reason goes in the same row rather than leaving the reader to infer it.
 */
function whyNotBlockingHtml(f) {
  if (f.owner !== 'vendor' || f.severity === 'minor') return '';
  return `<div class="why-not"><b>為什麼標 ${escapeHtml(f.severity)} 卻不用你處理：</b>
    這個檔案是 plugin / 工具組帶進來的，不是你寫的。你改了，它下次升級就會蓋回去。
    能做的是升級那個套件、把它移除，或記一筆豁免。</div>`;
}

/* ---------------- inventory catalogue ---------------- */

const HOWTO = {
  skills: {
    trigger: '自動。agent 每次對話開始就看得到所有 skill 的<b>名稱＋描述</b>，判斷你的話符合哪個描述就自動載入該 skill 的內文。',
    invoke: '你不用記名稱。描述寫得夠準就會自己觸發；想強制用就直接說「用 <b>xxx</b> skill」。',
    note: '描述欄就是觸發條件 —— 它同時寫了「做什麼」和「什麼時候用」。下面每張卡片的內文就是它。',
  },
  instructions: {
    trigger: '永遠載入。每一次對話、每一個 token 都帶著它，不需要任何條件。',
    invoke: '不用取用，它一直在。這也是為什麼官方建議單檔 &lt; 200 行 —— 寫越多，每次對話固定成本越高。',
    note: '你的兩個檔案由 canonical/ 產生，改要改來源，不要改它們。',
  },
  workflows: {
    trigger: '不自動。只有你的 instruction 明確叫 agent 去讀，它才會被讀進來。',
    invoke: '在 CLAUDE.md 寫「需要完整步驟時讀 ~/.claude/workflows/fix.md」這種指路，或你自己說「照 fix workflow 做」。',
    note: '沒有被任何 instruction 指到的 workflow 等於不存在（規則 WF001 會抓）。',
  },
  commands: {
    trigger: '手動。你在對話框打 <code class="mono">/名稱</code> 才會執行。',
    invoke: '直接打斜線加名稱，例如 <code class="mono">/codex:review</code>。',
    note: '和 skill 同名會互相遮蔽，只有一個會生效。',
  },
  agents: {
    trigger: '由主 agent 決定。當它判斷某個任務適合外派時，會依 description 挑一個 subagent。',
    invoke: '你也可以直接說「用 <b>xxx</b> agent 做這件事」。',
    note: 'subagent 有自己的 context，適合大而獨立、可平行的工作。',
  },
  hooks: {
    trigger: '事件觸發。指定的事件發生時（例如每次 Bash 呼叫前）自動執行，你不會看到它被呼叫。',
    invoke: '不能主動取用，只能改 ~/.claude/settings.json 的條件。',
    note: '下面「if 條件」就是它何時開火，「注入內容」是它會塞給 agent 的文字。',
  },
};

function howtoHtml(kind) {
  const h = HOWTO[kind];
  if (!h) return '';
  return `<dl class="howto">
      <dt>什麼時候被觸發</dt><dd>${h.trigger}</dd>
      <dt>你怎麼取用</dt><dd>${h.invoke}</dd>
      <dt>要注意</dt><dd>${h.note}</dd>
    </dl>`;
}

/* Which actions a catalogue row may honestly offer.
 *
 * Quarantine moves a file out of the config tree. That is the right offer for
 * something you wrote, and the wrong one for vendor content: a plugin upgrade
 * puts the file straight back, and removing one file out of an install leaves
 * the plugin half-present. The button was shown on every row regardless, so the
 * page invited an action that could not stick and said nothing about why.
 */
const VENDOR_ORIGINS = { plugin: 'plugin', toolkit: '工具組' };

function rowActionsHtml(r) {
  const path = escapeHtml(shortPath(r.path));
  const peek = `<button class="linkish" data-peek="${path}">看內容</button>`;
  const vendor = VENDOR_ORIGINS[r.origin];
  if (!vendor) {
    return `${peek}<button class="linkish danger" data-quarantine="${path}">隔離</button>`;
  }
  const how =
    r.origin === 'plugin'
      ? r.plugin
        ? `到「plugin」分頁停用 <code class="mono">${escapeHtml(r.plugin)}</code>`
        : '到「plugin」分頁停用它所屬的 plugin'
      : '用該工具組自己的指令移除';
  return `${peek}<span class="muted">這是${vendor}帶進來的，隔離會被下次升級覆蓋 —— 要移除請${how}</span>`;
}

/* The catalogue, grouped instead of poured out in one column.
 *
 * 222 skills rendered flat is 42 screens of uninterrupted scroll with no way to
 * tell where your own files end and a plugin's begin - the page could tell you a
 * skill's trigger and still not let you find it. Grouping by who owns the file
 * matches the only question a reader actually has here ("is this mine to
 * change?"), and the group you own opens by default because it is the one you
 * can act on.
 */
const GROUP_ORDER = ['local', 'toolkit', 'plugin', 'orphan-library'];
const GROUP_LABEL = {
  local: '你自己寫的',
  toolkit: '工具組裝的',
  plugin: 'plugin 帶的',
  'orphan-library': '載入不到的（放在 agent 讀不到的目錄）',
  other: '其他',
};
const GROUP_NOTE = {
  local: '你可以直接改，改了就生效。',
  toolkit: '工具組升級會覆蓋，要改請改工具組那邊。',
  plugin: 'plugin 升級會覆蓋。要移除請停用該 plugin。',
  'orphan-library': '這些完全不花錢，也叫不動 —— 它們不在任何 runtime 的載入路徑上。',
  other: '',
};

function catalogueSections(kind, rows, { expandAll = false } = {}) {
  if (!rows || !rows.length) return '<span class="muted">沒有符合的項目。</span>';

  const groups = new Map();
  for (const r of rows) {
    const key = GROUP_ORDER.includes(r.origin) ? r.origin : 'other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  // One group is not a grouping - skip the chrome and render the list.
  if (groups.size < 2) {
    return sortRows(rows)
      .map((r) => catalogueCard(kind, r))
      .join('');
  }

  const order = [...GROUP_ORDER, 'other'].filter((k) => groups.has(k));
  return order
    .map((key) => {
      const items = sortRows(groups.get(key));
      // Closed by default: with 222 items the first screen should be an index
      // of what exists, not the first 92 cards of one group with the other two
      // headers 17 screens down. A search is the opposite case - the reader has
      // already narrowed it and wants to see what matched, so everything opens.
      const open = expandAll ? ' open' : '';
      const note = GROUP_NOTE[key] ? `<span class="muted">${escapeHtml(GROUP_NOTE[key])}</span>` : '';
      return `<details class="cat-group"${open}>
        <summary><b>${escapeHtml(GROUP_LABEL[key])}</b> <span class="tag">${items.length}</span> ${note}</summary>
        ${items.map((r) => catalogueCard(kind, r)).join('')}
      </details>`;
    })
    .join('');
}

function sortRows(rows) {
  return rows
    .slice()
    .sort((a, b) => String(a.name || a.path).localeCompare(String(b.name || b.path)));
}

function catalogueCard(kind, r) {
  const name = r.name || r.dir_name || shortPath(r.path);
  const src = r.origin ? `<span class="tag">${escapeHtml(r.origin)}</span>` : '';
  const rt = r.runtime ? `<span class="tag">${escapeHtml(r.runtime)}</span>` : '';

  if (kind === 'skills') {
    const big = r.body_lines > 500;
    // An orphan-library skill is on no runtime's load path. Telling the reader
    // how to invoke it would be a lie: calling it does nothing.
    const orphan = r.origin === 'orphan-library';
    return `<article class="cat${orphan ? ' cat-off' : ''}">
      <div class="cat-h">
        <b class="mono">${escapeHtml(name)}</b>${src}${rt}
        ${r.plugin ? `<span class="tag">來自 ${escapeHtml(r.plugin)}</span>` : ''}
        <span class="grow"></span>
        <span class="muted mono" style="font-size:12px">${r.body_lines} 行${big ? ' ⚠' : ''}</span>
      </div>
      <div class="cat-d">${
        r.description
          ? escapeHtml(r.description)
          : '<span class="muted">沒有描述 —— 沒有描述就等於不會被自動觸發。</span>'
      }</div>
      <div class="cat-f">
        ${rowActionsHtml(r)}
        ${
          orphan
            ? '<span class="off">✕ 這個載入不到 —— 它不在 agent 會讀的目錄裡，叫了也不會有反應</span>'
            : `<span>要它做事：<code class="mono">用 ${escapeHtml(name)} skill</code></span>`
        }
        ${(r.refs || []).length ? `<span>附 ${r.refs.length} 個參考檔</span>` : ''}
        <span class="mono muted">${escapeHtml(shortPath(r.path))}</span>
      </div>
    </article>`;
  }
  if (kind === 'commands') {
    return `<article class="cat">
      <div class="cat-h"><b class="mono">/${escapeHtml(name)}</b>${rt}<span class="grow"></span>
        <span class="muted mono" style="font-size:12px">${r.lines} 行</span></div>
      <div class="cat-d">${escapeHtml(r.description || '（這個 command 沒有描述）')}</div>
      <div class="cat-f">${rowActionsHtml(r)}<span>打 <code class="mono">/${escapeHtml(name)}</code> 執行</span>
        <span class="mono muted">${escapeHtml(shortPath(r.path))}</span></div>
    </article>`;
  }
  if (kind === 'agents') {
    return `<article class="cat">
      <div class="cat-h"><b class="mono">${escapeHtml(name)}</b><span class="grow"></span>
        <span class="muted mono" style="font-size:12px">${r.lines} 行</span></div>
      <div class="cat-d">${escapeHtml(r.description || '（沒有描述）')}</div>
      <div class="cat-f">${rowActionsHtml(r)}<span>要它做事：<code class="mono">用 ${escapeHtml(name)} agent</code></span>
        <span class="mono muted">${escapeHtml(shortPath(r.path))}</span></div>
    </article>`;
  }
  if (kind === 'hooks') {
    return `<article class="cat">
      <div class="cat-h"><b class="mono">${escapeHtml(r.event)}</b>
        <span class="tag">matcher ${escapeHtml(r.matcher || '*')}</span></div>
      <div class="cat-d"><b>何時開火：</b><code class="mono">${escapeHtml(r.if_rule || '（無條件，一律開火）')}</code></div>
      <div class="cat-d" style="margin-top:6px"><b>塞給 agent：</b>${escapeHtml((r.injects || '（無）').slice(0, 300))}</div>
    </article>`;
  }
  const stem = String(name).replace(/\.md$/, '');
  return `<article class="cat">
    <div class="cat-h"><b class="mono">${escapeHtml(shortPath(r.path))}</b>${rt}<span class="grow"></span>
      <span class="muted mono" style="font-size:12px">${r.lines} 行</span></div>
    ${r.description ? `<div class="cat-d">${escapeHtml(r.description)}</div>` : ''}
    ${kind === 'workflows' ? `<div class="cat-f">${rowActionsHtml(r)}<span>要用它：<code class="mono">照 ${escapeHtml(stem)} workflow 做</code></span></div>` : ''}
  </article>`;
}

/* ---------------- update flow ---------------- */

/** Targets are identified as `name@root` to stay unique; readers want the name. */
function displayName(target) {
  return String(target || '').split('@')[0];
}

function updateConfirmHtml(target) {
  return `<div class="confirm">
         <b>要更新 ${escapeHtml(displayName(target))} 嗎？</b>
         這會執行該套件自己的更新器，過程可能要 <b>數十秒到數分鐘</b>，期間這一列會顯示進度。
         <div class="row"><button data-go="${escapeHtml(target)}" class="primary">確定更新</button>
         <button data-cancel="${escapeHtml(target)}">取消</button></div>
       </div>`;
}

function updateRunningHtml(target, seconds) {
  return `<div class="running"><span class="spin"></span>
         正在更新 <b>${escapeHtml(displayName(target))}</b>… 已經跑了 <b class="mono">${seconds}s</b>
         <span class="muted">（呼叫套件自己的更新器，請不要關掉這頁）</span></div>`;
}

function updateResultHtml(res, seconds) {
  const r = res || {};
  return `<div class="result ${r.ok ? 'ok' : 'bad'}">
         <b>${r.ok ? '✓ 更新完成' : '✕ 更新失敗'}</b>（耗時 ${seconds}s）
         <div>${escapeHtml(r.message || '沒有回傳訊息')}</div>
         ${r.needs_restart ? '<div class="warn">要<b>重開 Claude Code</b> 才會套用。</div>' : ''}
         ${r.restore_hint ? `<div class="sub">要回退：<code class="mono">${escapeHtml(r.restore_hint)}</code></div>` : ''}
         ${
           (r.steps || []).length
             ? `<details><summary>看它實際執行了什麼（${r.steps.length} 步）</summary>
                <pre class="steps">${escapeHtml(
                  r.steps
                    .map((s) => `$ ${s.cmd}\n  rc=${s.rc}${s.stderr ? '\n  ' + String(s.stderr).slice(0, 500) : ''}`)
                    .join('\n\n'),
                )}</pre></details>`
             : ''
         }
       </div>`;
}

/**
 * Describe what sync covers, from the targets it actually rendered.
 *
 * Hardcoding this named two files while six were checked. Anything the page
 * says about coverage has to come from the coverage itself.
 */
function syncTargetSummary(targets) {
  const names = (targets || []).filter(Boolean);
  if (!names.length) return '產生出來的檔案';
  const counts = new Map();
  for (const n of names) counts.set(n, (counts.get(n) || 0) + 1);
  const parts = [...counts.entries()].map(([name, n]) => (n > 1 ? `${n} 個 ${name}` : name));
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join('、')} 與 ${parts[parts.length - 1]}`;
}

/* ---------------- spec freshness ---------------- */

const SPEC_STATE = {
  unchanged: ['ok', '沒有變動'],
  changed: ['important', '已變動 —— 依據它的規則要重新確認'],
  new: ['minor', '尚未建立基準'],
  unreachable: ['critical', '抓不到，這次沒檢查到'],
  unknown: ['minor', '未連線檢查'],
};

function specsHtml(payload) {
  const rows = (payload && payload.specs) || [];
  if (!rows.length) return '<span class="muted">沒有資料。</span>';
  const changed = rows.filter((r) => r.status === 'changed').length;
  const unreachable = rows.filter((r) => r.status === 'unreachable').length;

  const banner = unreachable
    ? `<div class="result bad"><b>${unreachable} 份文件抓不到</b><div>這次的結果不涵蓋它們，不能當成「規則都還正確」。</div></div>`
    : changed
      ? `<div class="result"><b>${changed} 份規範有變動</b><div>依據它們的規則需要重新確認。按「分析變動對規則的影響」讓模型逐條檢視，或自己開連結比對。</div></div>`
      : '<div class="result ok"><b>✓ 所有引用的規範都跟基準一致</b><div>規則依據的文件沒有改版。</div></div>';

  return (
    banner +
    rows
      .map((r) => {
        const [cls, label] = SPEC_STATE[r.status] || SPEC_STATE.unknown;
        return `<div class="spec-row">
          <div class="spec-head">
            <span class="tag ${cls}">${escapeHtml(label)}</span>
            <a href="${escapeHtml(r.url)}" target="_blank" rel="noreferrer">${escapeHtml(r.url)}</a>
          </div>
          <div class="sub">依據它的 ${r.rules.length} 條規則：<span class="mono">${r.rules.map(escapeHtml).join(', ')}</span></div>
          ${r.note ? `<div class="sub">${escapeHtml(r.note)}</div>` : ''}
        </div>`;
      })
      .join('')
  );
}

function specReviewHtml(review) {
  if (!review || !review.ok) {
    return `<div class="result bad"><b>分析失敗</b><div>${escapeHtml((review && review.error) || '未知錯誤')}</div></div>`;
  }
  const verdictTag = { 'still-valid': 'ok', 'needs-update': 'important', 'no-longer-supported': 'critical' };
  return `<div class="result">
      <b>${escapeHtml(review.url)}</b>
      <div class="sub" style="margin:6px 0">${escapeHtml(review.summary || '')}</div>
      ${(review.reviews || [])
        .map(
          (r) => `<div class="spec-row">
            <div class="spec-head">
              <span class="tag ${verdictTag[r.verdict] || 'minor'}">${escapeHtml(r.verdict)}</span>
              <b class="mono">${escapeHtml(r.rule)}</b>
            </div>
            <div class="sub">${escapeHtml(r.what_changed || '')}</div>
            ${r.suggested_change ? `<div class="remedy"><b>建議：</b>${escapeHtml(r.suggested_change)}</div>` : ''}
          </div>`,
        )
        .join('')}
      <div class="sub" style="margin-top:8px">
        這是意見，不是動作。<b>規則永遠不會被自動修改</b> —— 一條規則是一個關於「規範說了什麼」的主張，
        那個主張只該在人同意時才改變。
      </div>
    </div>`;
}

function scheduleHtml(s) {
  if (!s || !s.available) {
    return `<span class="muted">沒有排程安裝程式${s && s.reason ? `（${escapeHtml(s.reason)}）` : ''}。</span>`;
  }
  if (!s.installed) {
    return `<div class="result">
        <b>尚未安裝每日排程</b>
        <div class="sub">安裝後每天自動跑一次健檢，結果會出現在總覽的趨勢圖。</div>
        <div class="sub">在 repo 根目錄執行：<code class="mono">${escapeHtml(s.install_command)}</code></div>
      </div>`;
  }
  if (s.drifted) {
    return `<div class="result bad">
        <b>⚠ 排程用的是舊版程式碼</b>
        <div>排程從套件的<b>副本</b>執行。你改過 <code class="mono">studio/</code> 之後沒重新安裝，
        所以每天自動跑的還是舊規則 —— 它可能回報早就修好的問題，或漏掉新加的檢查。</div>
        <div class="sub">修正：在 repo 根目錄跑 <code class="mono">${escapeHtml(s.install_command)}</code></div>
        <details><summary>看狀態輸出</summary><pre class="steps">${escapeHtml(s.output || '')}</pre></details>
      </div>`;
  }
  return `<div class="result ok"><b>✓ 每日排程已安裝，且與 repo 同步</b>
      <details><summary>看狀態輸出</summary><pre class="steps">${escapeHtml(s.output || '')}</pre></details>
    </div>`;
}

/* ---------------- graph legend ---------------- */

/* Colour and dash pattern must match `#graph .edge.*` in style.css exactly,
   or the legend lies about what is on screen. */
const EDGE_STYLE = {
  references: ['var(--line)', '', '這個檔案裡寫到了另一個檔案（progressive disclosure 的參考檔）'],
  invokes: ['var(--accent)', '', 'A 明確叫 agent 去用 B'],
  mirror: ['var(--ok)', '4 3', '你在 governance.json 宣告這兩個必須位元組相同，是刻意的'],
  generated_from: ['var(--vendor)', '2 3', '右邊那個是由左邊產生出來的，不要直接改產生出來的那個'],
  duplicate: ['var(--bad)', '1 3', '內容一樣但你沒宣告過 —— 會各自漂移，要處理'],
  provides: ['var(--line)', '', 'plugin 帶進來的 skill'],
  collision: ['var(--warn)', '5 2', '兩個東西搶同一個名字，只有一個會生效'],
};

function legendHtml(kinds, presentEdgeKinds, nodeColors, edgeLabels) {
  const present = new Set(presentEdgeKinds || []);
  return `
    <div class="lg-group">
      <div class="lg-title">圓點的顏色 ＝ 這是什麼東西</div>
      <div class="lg-items">${(kinds || [])
        .map((k) => `<span><i style="background:${(nodeColors || {})[k] || '#888'}"></i>${escapeHtml(k)}</span>`)
        .join('')}</div>
      <div class="lg-note">圓越大代表份量越重（skill 的內文行數、plugin 的 skill 數）。<b class="ring">紅圈</b>＝這個檔案有阻斷性問題。</div>
    </div>
    <div class="lg-group">
      <div class="lg-title">線的樣式 ＝ 兩個東西是什麼關係</div>
      <div class="lg-lines">${Object.entries(EDGE_STYLE)
        .filter(([k]) => present.has(k))
        .map(
          ([k, [color, dash, why]]) => `<div class="lg-line">
            <svg width="34" height="10" aria-hidden="true"><line x1="1" y1="5" x2="33" y2="5"
              stroke="${color}" stroke-width="1.6" ${dash ? `stroke-dasharray="${dash}"` : ''}/></svg>
            <b>${escapeHtml((edgeLabels || {})[k] || k)}</b><span>${why}</span>
          </div>`,
        )
        .join('')}</div>
    </div>`;
}

/* The overview's four-row breakdown.
 *
 * `counts` from the health report is a partition, so each row is read straight
 * off it. This lived in app.js and derived the minor row as
 * `minor - vendor_owned`, which is only correct when every vendor finding is
 * minor; it disagreed with the findings tab on the real config. It sits here so
 * that arithmetic cannot come back unnoticed.
 */
function breakdownRows(counts) {
  const c = counts || {};
  return [
    {
      k: '需要你處理',
      n: c.blocking || 0,
      why: '你自己的設定裡，會影響 agent 行為的問題。這個數字是唯一的合格判準。',
      cls: (c.blocking || 0) > 0 ? 'critical' : 'ok',
    },
    {
      k: 'vendor（不是你的）',
      n: c.vendor_owned || 0,
      why: 'plugin / 工具組帶進來的。手改會被升級覆蓋，所以不列入判準。',
      cls: 'vendor',
    },
    {
      k: 'minor（可選改善）',
      n: c.minor || 0,
      why: '不影響運作的建議：沒在用的 plugin、殘留備份、reference 檔缺目錄。',
      cls: 'minor',
    },
    {
      k: '已豁免',
      n: c.waived || 0,
      why: '你記錄過理由、決定不修的。豁免是留在紀錄上的決定，不是把它靜音。',
      cls: 'ok',
    },
  ];
}

export {
  breakdownRows,
  catalogueSections,
  rowActionsHtml,
  num,
  shortPath,
  escapeHtml,
  metaBreakdownHtml,
  trendHtml,
  blocks,
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
  displayName,
  EDGE_STYLE,
  HOWTO,
  BUCKET_LABEL,
};
