/* Tests for the dashboard's pure HTML builders.
 *
 * Plain Node, no test framework, no npm - the same constraint as the rest of
 * the project. Run directly, or through `python3 -m unittest`, which shells out
 * to this file and skips it when node is unavailable.
 *
 * Every case here corresponds to something a reader actually got wrong when
 * looking at the page, or to a claim the page makes that must not become a lie.
 */

import assert from 'node:assert/strict';
import {
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
  syncTargetSummary,
  specsHtml,
  specReviewHtml,
  scheduleHtml,
  escapeHtml,
  EDGE_STYLE,
} from '../web/render.js';

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push(`${name}\n    ${err.message.split('\n')[0]}`);
  }
}

/* ---------------- escaping ---------------- */

test('escapeHtml neutralises markup from scanned files', () => {
  // Descriptions come from files on disk. They are data, never markup.
  const out = escapeHtml('<img src=x onerror="alert(1)">');
  assert.ok(!out.includes('<img'), 'tag survived escaping');
  assert.ok(out.includes('&lt;img'));
});

test('a skill description cannot inject markup into its card', () => {
  const html = catalogueCard('skills', {
    name: 'x',
    origin: 'local',
    runtime: 'claude',
    body_lines: 10,
    path: '/p/SKILL.md',
    description: '</div><script>alert(1)</script>',
  });
  assert.ok(!html.includes('<script>'), 'script tag reached the page');
});

/* ---------------- preloaded metadata ---------------- */

const METRICS = {
  total_est_tokens: 18986,
  total_bytes: 75944,
  avoidable_est_tokens: 0,
  by_bucket: {
    plugin: { skills: 100, bytes: 30000 },
    'local:codex': { skills: 62, bytes: 20000 },
    toolkit: { skills: 38, bytes: 15944 },
    'local:claude': { skills: 19, bytes: 10000 },
  },
};

test('cost is shown per runtime, never as a sum nobody pays', () => {
  // Plugins and toolkits install under ~/.claude, so Codex cannot load them.
  // A single combined figure is a number no session ever pays.
  const html = metaBreakdownHtml({
    ...METRICS,
    per_runtime: { claude: { est_tokens: 10664, skills: 174 }, codex: { est_tokens: 4896, skills: 62 } },
  }, 1097);
  assert.ok(html.includes('10,664'), 'Claude cost not shown');
  assert.ok(html.includes('4,896'), 'Codex cost not shown');
  assert.ok(html.includes('沒有任何一次對話會付'), 'did not warn that the total is not a session cost');
});

test('the skill count beside the token figure is what the figure counts', () => {
  // The bug: this showed the inventory total (1,098), which includes an
  // unreferenced library that is never loaded and costs nothing. The number
  // next to a cost must be the number that cost was computed from.
  const html = metaBreakdownHtml(METRICS, 1098);
  assert.ok(html.includes('219'), 'did not show the summed bucket count');
  assert.ok(!/＝ 1,098 個/.test(html), 'showed the inventory total as the cost basis');
});

test('skills that never load are named as free, not hidden', () => {
  const html = metaBreakdownHtml(METRICS, 1098);
  assert.ok(html.includes('879'), 'did not account for the skills excluded from the cost');
});

test('no phantom count when everything on disk is loadable', () => {
  const html = metaBreakdownHtml(METRICS, 219);
  assert.ok(!html.includes('讀不到的目錄'), 'claimed unloadable skills that do not exist');
});

test('zero avoidable waste reads as a bill, not as a problem', () => {
  const html = metaBreakdownHtml(METRICS, 219);
  assert.ok(html.includes('沒有可避免的浪費'));
  assert.ok(!html.includes('tag important'), 'flagged a problem when there is none');
});

test('avoidable waste is surfaced with the rule to look at', () => {
  const html = metaBreakdownHtml({ ...METRICS, avoidable_est_tokens: 2100 }, 219);
  assert.ok(html.includes('2,100'));
  assert.ok(html.includes('CB001'), 'did not say which rule explains it');
});

/* ---------------- trend ---------------- */

const HISTORY = [
  { generated_at: '2026-07-25T10:00:00+00:00', verdict: 'FAIL', counts: { blocking: 99 } },
  { generated_at: '2026-07-26T10:00:00+00:00', verdict: 'FAIL', counts: { blocking: 40 } },
  { generated_at: '2026-07-27T10:00:00+00:00', verdict: 'PASS', counts: { blocking: 0 } },
];

test('trend labels both ends of the time axis', () => {
  // Without dates the chart was unreadable: bars with no stated direction.
  const { chart } = trendHtml(HISTORY);
  assert.ok(chart.includes('2026-07-25'), 'oldest date missing');
  assert.ok(chart.includes('2026-07-27'), 'newest date missing');
  assert.ok(chart.includes('（最舊）') && chart.includes('（最新）'), 'direction not stated');
});

test('every bar carries its date and count on hover', () => {
  const { chart } = trendHtml(HISTORY);
  assert.equal((chart.match(/title="/g) || []).length, 3);
  assert.ok(chart.includes('99 項待處理'));
});

test('a passing run draws no bar, so height always means outstanding work', () => {
  const { chart } = trendHtml(HISTORY);
  assert.ok(chart.includes('class="fill pass" style="height:0%'), 'a clean run drew a bar');
});

test('the date axis is actually visible, not merely present', () => {
  // A string test can be fooled by markup that renders nothing. The axis was
  // the fix for "I cannot read this chart", so it has to be on screen.
  const { chart } = trendHtml(HISTORY);
  const axis = chart.slice(chart.indexOf('trend-axis'));
  assert.ok(!/^[^>]*\bhidden\b/.test(axis), 'axis element is hidden');
  assert.ok(!/display:\s*none/.test(axis), 'axis is display:none');
});

test('a run marked PASS never draws a bar, even on inconsistent data', () => {
  // Defensive: verdict and count come from the same report, so a PASS with a
  // nonzero count should not happen. If it ever does, the verdict wins - a
  // green run must never render as outstanding work.
  const odd = [{ generated_at: '2026-07-27T10:00:00+00:00', verdict: 'PASS', counts: { blocking: 7 } }];
  const { chart } = trendHtml(odd);
  assert.ok(chart.includes('class="fill pass" style="height:0%'), 'a passing run drew a bar');
});

test('the y axis states the scale', () => {
  const { chart } = trendHtml(HISTORY);
  assert.ok(chart.includes('<span>99</span>'), 'peak value not labelled');
});

test('empty history says so instead of drawing an empty box', () => {
  const { chart, caption } = trendHtml([]);
  assert.ok(chart.includes('尚無歷史紀錄'));
  assert.equal(caption, '');
});

test('caption counts failures rather than leaving red bars unexplained', () => {
  assert.ok(trendHtml(HISTORY).caption.includes('<b>2</b> 次未通過'));
  assert.ok(trendHtml([HISTORY[2]]).caption.includes('全部通過'));
});

/* ---------------- findings ---------------- */

const VENDOR_IMPORTANT = { rule: 'SK013', severity: 'important', owner: 'vendor', waived: false };
const LOCAL_IMPORTANT = { rule: 'WF006', severity: 'important', owner: 'local', waived: false };
const LOCAL_MINOR = { rule: 'CB002', severity: 'minor', owner: 'local', waived: false };
const WAIVED = { rule: 'SK007', severity: 'important', owner: 'local', waived: true };

test('"important" on a vendor file is labelled as not yours to fix', () => {
  // This is the exact confusion reported: an important badge with a zero
  // blocking count reads as a contradiction.
  assert.ok(actionLabelHtml(VENDOR_IMPORTANT).includes('不用，不是你的'));
});

test('an important finding you own is labelled as needing action', () => {
  assert.ok(actionLabelHtml(LOCAL_IMPORTANT).includes('要你處理'));
});

test('minor and waived findings are never labelled as needing action', () => {
  assert.ok(actionLabelHtml(LOCAL_MINOR).includes('可選'));
  assert.ok(actionLabelHtml(WAIVED).includes('已豁免'));
  assert.ok(!actionLabelHtml(WAIVED).includes('要你處理'));
});

test('a vendor-owned important finding explains itself in the row', () => {
  const html = whyNotBlockingHtml(VENDOR_IMPORTANT);
  assert.ok(html.includes('為什麼標 important 卻不用你處理'));
  assert.ok(html.includes('升級'), 'did not say what can actually be done');
});

test('no explanation is added where there is no contradiction', () => {
  assert.equal(whyNotBlockingHtml(LOCAL_IMPORTANT), '');
  assert.equal(whyNotBlockingHtml({ ...VENDOR_IMPORTANT, severity: 'minor' }), '');
});

/* ---------------- catalogue ---------------- */

const USABLE = {
  name: 'agent-browser',
  origin: 'local',
  runtime: 'claude',
  body_lines: 234,
  path: '/Users/me/.claude/skills/agent-browser/SKILL.md',
  description: 'Fallback browser tool. Use when the built-in browser cannot connect.',
  refs: ['reference/a.md'],
};

test('a usable skill tells you how to invoke it', () => {
  const html = catalogueCard('skills', USABLE);
  assert.ok(html.includes('用 agent-browser skill'));
  assert.ok(html.includes('Use when the built-in browser'), 'trigger condition not shown');
});

test('a skill that cannot load is never given invocation instructions', () => {
  // 861 skills sit in a directory no runtime reads. Telling the reader to
  // invoke one would be a lie: calling it does nothing at all.
  const html = catalogueCard('skills', { ...USABLE, origin: 'orphan-library' });
  assert.ok(!html.includes('要它做事'), 'offered a way to invoke an unloadable skill');
  assert.ok(html.includes('這個載入不到'));
  assert.ok(html.includes('cat-off'), 'not visually de-emphasised');
});

test('a skill with no description says why that matters', () => {
  const html = catalogueCard('skills', { ...USABLE, description: '' });
  assert.ok(html.includes('不會被自動觸發'));
});

test('an oversized skill body is marked', () => {
  assert.ok(catalogueCard('skills', { ...USABLE, body_lines: 900 }).includes('⚠'));
  assert.ok(!catalogueCard('skills', USABLE).includes('⚠'));
});

test('home directories are shortened rather than leaking a username', () => {
  assert.ok(catalogueCard('skills', USABLE).includes('~/.claude/skills/agent-browser/SKILL.md'));
  assert.ok(!catalogueCard('skills', USABLE).includes('/Users/me'));
});

test('commands are shown with the slash you actually type', () => {
  const html = catalogueCard('commands', {
    name: 'tdd',
    runtime: 'claude',
    lines: 40,
    path: '/p/tdd.md',
    description: '測試驅動開發流程',
  });
  assert.ok(html.includes('/tdd'));
  assert.ok(html.includes('測試驅動開發流程'), 'purpose not shown');
});

test('a workflow says how it gets used, since it never self-triggers', () => {
  const html = catalogueCard('workflows', {
    path: '/p/build.md',
    name: 'build.md',
    runtime: 'claude',
    lines: 60,
    description: 'BUILD 流程（開發實作）',
  });
  assert.ok(html.includes('照 build workflow 做'));
  assert.ok(html.includes('BUILD 流程'));
});

test('a hook shows when it fires and what it injects', () => {
  const html = catalogueCard('hooks', {
    event: 'PreToolUse',
    matcher: 'Bash(git commit:*)',
    if_rule: 'git diff --cached',
    injects: 'Commit 前流程：先跑 /codex:review',
  });
  assert.ok(html.includes('何時開火'));
  assert.ok(html.includes('塞給 agent'));
  assert.ok(html.includes('Commit 前流程'));
});

test('every catalogue kind explains its trigger mechanism', () => {
  for (const kind of ['skills', 'instructions', 'workflows', 'commands', 'agents', 'hooks']) {
    const html = howtoHtml(kind);
    assert.ok(html.includes('什麼時候被觸發'), `${kind} missing trigger explanation`);
    assert.ok(html.includes('你怎麼取用'), `${kind} missing invocation explanation`);
  }
});

/* ---------------- update flow ---------------- */

test('confirmation warns that the update is slow before it starts', () => {
  const html = updateConfirmHtml('gstack');
  assert.ok(html.includes('數十秒到數分鐘'), 'no duration warning');
  assert.ok(html.includes('data-go="gstack"') && html.includes('data-cancel="gstack"'));
});

test('progress shows elapsed time, so a slow update is not mistaken for a hang', () => {
  // The reported symptom was "I clicked update and nothing happened". The
  // update was running; the page just never said so.
  const html = updateRunningHtml('gstack', 42);
  assert.ok(html.includes('42s'));
  assert.ok(html.includes('spin'), 'no activity indicator');
});

test('a successful update reports duration, restart need and how to undo', () => {
  const html = updateResultHtml(
    {
      ok: true,
      message: '已從 0.15.16 升級到 1.60.1',
      needs_restart: true,
      restore_hint: 'git -C /x reset --hard abc123',
      steps: [{ cmd: 'git fetch origin', rc: 0, stderr: '' }],
    },
    73,
  );
  assert.ok(html.includes('✓ 更新完成'));
  assert.ok(html.includes('73s'));
  assert.ok(html.includes('重開 Claude Code'), 'did not say a restart is required');
  assert.ok(html.includes('reset --hard abc123'), 'no way back');
  assert.ok(html.includes('1 步'), 'did not expose what it ran');
});

test('a failed update says so and still offers a way back', () => {
  const html = updateResultHtml(
    { ok: false, message: 'setup 失敗 (rc=1)', restore_hint: 'git reset --hard abc' },
    12,
  );
  assert.ok(html.includes('✕ 更新失敗'));
  assert.ok(html.includes('result bad'));
  assert.ok(html.includes('git reset --hard abc'));
});

test('a result with no message never renders as blank', () => {
  assert.ok(updateResultHtml({ ok: true }, 3).includes('沒有回傳訊息'));
  assert.ok(updateResultHtml(null, 3).includes('沒有回傳訊息'));
});

/* ---------------- legend ---------------- */

test('legend explains every edge kind actually on screen', () => {
  const html = legendHtml(
    ['skill', 'workflow'],
    ['duplicate', 'mirror'],
    { skill: '#00f', workflow: '#0f0' },
    { duplicate: '未宣告的重複', mirror: '宣告的鏡像' },
  );
  assert.ok(html.includes('未宣告的重複'));
  assert.ok(html.includes('宣告的鏡像'));
  assert.ok(html.includes('會各自漂移'), 'label without an explanation of what it means');
});

test('legend omits edge kinds that are not on screen', () => {
  const html = legendHtml(['skill'], ['mirror'], {}, { mirror: '宣告的鏡像' });
  assert.ok(!html.includes('命名衝突'), 'explained a relationship not present');
});

test('legend draws a real sample of each line style', () => {
  // The reported problem was not knowing what a dashed line meant. A colour
  // swatch cannot answer that; a drawn sample can.
  const html = legendHtml(['skill'], ['duplicate'], {}, { duplicate: 'x' });
  assert.ok(html.includes('<svg'), 'no drawn sample');
  assert.ok(html.includes('stroke-dasharray="1 3"'), 'sample does not use the real dash pattern');
});

test('solid relationships are drawn without a dash pattern', () => {
  const html = legendHtml(['skill'], ['invokes'], {}, { invokes: '呼叫' });
  assert.ok(html.includes('<svg') && !html.includes('stroke-dasharray'));
});

test('legend node colours come from the graph, never invented', () => {
  const html = legendHtml(['skill'], [], { skill: '#3d6fd6' }, {});
  assert.ok(html.includes('#3d6fd6'));
});

/* ---------------- spec freshness ---------------- */

test('unchanged specs read as a clean result, not a to-do', () => {
  const html = specsHtml({ specs: [{ url: 'https://x/a', status: 'unchanged', rules: ['SK001'] }], changed: [], new: [], unreachable: [] });
  assert.ok(html.includes('都跟基準一致'));
  assert.ok(html.includes('result ok'));
});

test('a changed spec says which rules now need re-reading', () => {
  const html = specsHtml({
    specs: [{ url: 'https://x/hooks', status: 'changed', rules: ['HK001', 'HK004'], note: 'since 2026-07-27' }],
    changed: ['https://x/hooks'], new: [], unreachable: [],
  });
  assert.ok(html.includes('1 份規範有變動'));
  assert.ok(html.includes('HK001') && html.includes('HK004'), 'dependent rules not named');
});

test('an unreachable spec is never reported as still valid', () => {
  // Absence of evidence must not read as evidence of compliance - the whole
  // discipline this tool is built on.
  const html = specsHtml({
    specs: [{ url: 'https://x/a', status: 'unreachable', rules: ['SK001'], note: 'timeout' }],
    changed: [], new: [], unreachable: ['https://x/a'],
  });
  assert.ok(html.includes('抓不到'));
  assert.ok(html.includes('不能當成'), 'did not warn that the run does not cover it');
  assert.ok(!html.includes('都跟基準一致'), 'claimed everything matched while a fetch failed');
});

test('an AI review is labelled as an opinion, never an action', () => {
  const html = specReviewHtml({
    ok: true, url: 'https://x/hooks', summary: 'restructured',
    reviews: [{ rule: 'HK004', verdict: 'needs-update', what_changed: 'new event', suggested_change: 'add it' }],
  });
  assert.ok(html.includes('HK004') && html.includes('needs-update'));
  assert.ok(html.includes('永遠不會被自動修改'), 'did not state that rules are never auto-edited');
});

test('a failed review says so instead of rendering blank', () => {
  assert.ok(specReviewHtml({ ok: false, error: 'no CLI' }).includes('分析失敗'));
  assert.ok(specReviewHtml(null).includes('分析失敗'));
});

/* ---------------- schedule ---------------- */

test('an uninstalled schedule shows the command that installs it', () => {
  const html = scheduleHtml({ available: true, installed: false, install_command: 'scripts/install-launchd.sh install' });
  assert.ok(html.includes('尚未安裝'));
  assert.ok(html.includes('install-launchd.sh install'), 'did not say how to install it');
});

test('an installed schedule can be inspected', () => {
  const html = scheduleHtml({ available: true, installed: true, output: 'loaded: yes' });
  assert.ok(html.includes('已安裝'));
  assert.ok(html.includes('loaded: yes'));
});

test('a drifted scheduled copy is called out, not buried', () => {
  // The scheduled job runs from a copy. After an edit it keeps checking with old
  // code - it reported five fixed findings for two days - and the only warning
  // was a line inside a collapsed section.
  const html = scheduleHtml({ available: true, installed: true, drifted: true, output: 'copy: DRIFTED', install_command: 'scripts/install-launchd.sh install' });
  assert.ok(html.includes('舊版程式碼'), 'drift is not stated');
  assert.ok(html.includes('result bad'), 'drift is not visually flagged');
  assert.ok(html.includes('install-launchd.sh install'), 'does not say how to fix it');
});

test('an in-sync schedule says so explicitly', () => {
  const html = scheduleHtml({ available: true, installed: true, drifted: false, output: 'copy: in sync' });
  assert.ok(html.includes('與 repo 同步'));
  assert.ok(html.includes('result ok'));
});

test('no installer available says so rather than claiming not installed', () => {
  assert.ok(scheduleHtml({ available: false, reason: 'installer not present' }).includes('沒有排程安裝程式'));
});

/* ---------------- sync coverage ---------------- */

test('sync coverage is described from the targets actually rendered', () => {
  // It was hardcoded as "CLAUDE.md and AGENTS.md" while six files were checked,
  // so a reader could hand-edit a generated skill believing sync missed it.
  const out = syncTargetSummary([
    'CLAUDE.md', 'AGENTS.md', 'SKILL.md', 'SKILL.md', 'SKILL.md', 'SKILL.md',
  ]);
  assert.ok(out.includes('4 個 SKILL.md'), `did not account for the skills: ${out}`);
  assert.ok(out.includes('CLAUDE.md') && out.includes('AGENTS.md'));
});

test('two targets read naturally', () => {
  assert.equal(syncTargetSummary(['CLAUDE.md', 'AGENTS.md']), 'CLAUDE.md 與 AGENTS.md');
});

test('a single target needs no conjunction', () => {
  assert.equal(syncTargetSummary(['CLAUDE.md']), 'CLAUDE.md');
});

test('no targets never claims specific files', () => {
  assert.equal(syncTargetSummary([]), '產生出來的檔案');
  assert.equal(syncTargetSummary(undefined), '產生出來的檔案');
});

/* ---------------- report ---------------- */

if (failures.length) {
  console.error(`\n${failures.length} UI test(s) failed:\n`);
  for (const f of failures) console.error(`  ✕ ${f}\n`);
  console.error(`${passed} passed, ${failures.length} failed`);
  process.exit(1);
}
console.log(`${passed} UI tests passed`);
