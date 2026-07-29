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
      所以這 <b>${num(metrics.total_est_tokens)} tokens ＝ ${num(counted)} 個「真的會被載入」的 skill
      名稱＋描述總和</b>，不是它們的完整內容。${
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
      <div class="ylab"><span>${max}</span><span>0</span></div>
      <div class="bars">${hist
        .map((p) => {
          const b = (p.counts || {}).blocking || 0;
          const pass = p.verdict === 'PASS';
          const pct = b === 0 ? 0 : Math.max(8, Math.round((b / max) * 100));
          const when = (p.generated_at || '').replace('T', ' ').slice(0, 16);
          return `<div class="slot" title="${escapeHtml(when)} ／ ${pass ? '通過' : '未通過'}：${b} 項待處理">
              <div class="fill ${pass ? 'pass' : 'fail'}" style="height:${pass ? 0 : pct}%"></div>
            </div>`;
        })
        .join('')}</div>
    </div>
    <div class="trend-axis"><span>${escapeHtml((hist[0].generated_at || '').slice(0, 10))}（最舊）</span><span>${escapeHtml(
      (last.generated_at || '').slice(0, 10),
    )}（最新）</span></div>`;

  const caption = `
    一根 = 一次健檢，左舊右新，共 ${hist.length} 次。<b>柱子越高代表當時待處理的問題越多</b>；
    貼齊底線的綠色細線代表那次是 0 問題、通過。滑鼠移上去看日期與數量。
    目前 ${fails > 0 ? `有 <b>${fails}</b> 次未通過（都在早期，是這個工具剛開始幫你清理時的狀態）` : '全部通過'}。`;

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
      <div class="cat-f"><span>打 <code class="mono">/${escapeHtml(name)}</code> 執行</span>
        <span class="mono muted">${escapeHtml(shortPath(r.path))}</span></div>
    </article>`;
  }
  if (kind === 'agents') {
    return `<article class="cat">
      <div class="cat-h"><b class="mono">${escapeHtml(name)}</b><span class="grow"></span>
        <span class="muted mono" style="font-size:12px">${r.lines} 行</span></div>
      <div class="cat-d">${escapeHtml(r.description || '（沒有描述）')}</div>
      <div class="cat-f"><span>要它做事：<code class="mono">用 ${escapeHtml(name)} agent</code></span>
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
    ${kind === 'workflows' ? `<div class="cat-f"><span>要用它：<code class="mono">照 ${escapeHtml(stem)} workflow 做</code></span></div>` : ''}
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

export {
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
  syncTargetSummary,
  displayName,
  EDGE_STYLE,
  HOWTO,
  BUCKET_LABEL,
};
