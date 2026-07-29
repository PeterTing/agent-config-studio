"""Command-line entry point.

    studio scan                 inventory every local agent-config root
    studio health               run all rules, save a report, print the verdict
    studio graph                emit the relationship graph
    studio updates              check cloud plugins for available updates
    studio usage                build the invocation index from local history
    studio fix                  apply the fixes that have one correct answer
    studio update               run each package's own updater
    studio consolidate          AI-planned consolidation, validated before applying
    studio specs                check whether the cited guidance has changed
    studio sync                 render canonical -> CLAUDE.md / AGENTS.md / mirrors
    studio apply <payload>      apply a reviewed change set (backs up first)
    studio backups              list restore points
    studio rollback <id>        restore a backup
    studio serve                start the local dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import (
    ai as ai_mod,
    canonical,
    consolidate as consolidate_mod,
    fixes as fixes_mod,
    graph as graph_mod,
    health,
    patch,
    scan,
    specs as specs_mod,
    updates as updates_mod,
    upgrade as upgrade_mod,
    usage,
)
from .model import to_json
from .rules import Config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _usage_cache() -> str:
    """Where the per-file usage cache lives."""
    root = REPO_ROOT if "REPO_ROOT" in globals() else Handler.repo_root
    return os.path.join(root, "var", "usage-cache.json")


def _cfg() -> Config:
    return Config.load(REPO_ROOT)


def _out_path(name: str) -> str:
    d = os.path.join(REPO_ROOT, "var")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


# --------------------------------------------------------------------------- #


def cmd_scan(args) -> int:
    inv = scan.scan()
    path = _out_path("inventory.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_json(inv))
    print(f"inventory -> {path}")
    for k, v in inv.counts().items():
        print(f"  {k:14s} {v}")
    if inv.scan_errors:
        print(f"  scan errors: {len(inv.scan_errors)}")
        for e in inv.scan_errors[:10]:
            print(f"    {e}")
    return 0


def cmd_usage(args) -> int:
    inv = scan.scan()
    idx = usage.build(cache_path=_usage_cache())
    counts = usage.plugin_usage(idx, inv)
    print(json.dumps(idx.summary(), indent=2, ensure_ascii=False))
    print("\ntop invoked tokens:")
    for token, n in sorted(idx.tokens.items(), key=lambda kv: -kv[1])[:25]:
        print(f"  {n:6d}  {token}")
    print("\nplugin invocation counts (only plugins with >0 shown):")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {name}")
    path = _out_path("usage.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"summary": idx.summary(), "tokens": idx.tokens, "plugins": counts}, fh, indent=2, ensure_ascii=False)
    print(f"\nusage -> {path}")
    return 0


def cmd_updates(args) -> int:
    inv = scan.scan()
    rep = updates_mod.check(inv, allow_network=not args.offline)
    print(json.dumps(rep.summary(), indent=2, ensure_ascii=False))
    if rep.updates:
        print("\nupdates available:")
        for u in rep.updates:
            print(f"  {u['plugin']:<48} {u['local'][:12]} -> {(u['remote'] or '')[:12]}")
    if rep.toolkits:
        print("\nskill toolkits:")
        for tk in rep.toolkits:
            flag = (
                "UPDATE AVAILABLE"
                if tk["update_available"]
                else ("up to date" if tk["update_available"] is False else "unknown")
            )
            print(
                f"  {tk['name']:<16} {flag:<17} local={tk['local_version'] or tk['commit'][:8]:<12} "
                f"remote={tk['remote_version'] or '?':<12} manages {tk['manages_count']} skill(s)"
            )
            if tk["note"]:
                print(f"      {tk['note']}")
    if rep.unknown:
        print(f"\nunknown ({len(rep.unknown)}): could not be compared")
        for u in rep.unknown[:15]:
            print(f"  {u['plugin']:<48} {u['reason']}")
    path = _out_path("updates.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "summary": rep.summary(),
                "updates": rep.updates,
                "up_to_date": rep.up_to_date,
                "unknown": rep.unknown,
                "errors": rep.errors,
                "toolkits": rep.toolkits,
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\nupdates -> {path}")
    return 0


def cmd_graph(args) -> int:
    inv = scan.scan()
    cfg = _cfg()
    g = graph_mod.build(inv, cfg, include_plugin_skills=args.expand_plugins)
    path = _out_path("graph.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(g, fh, indent=2, ensure_ascii=False)
    print(f"graph -> {path}")
    print(f"  nodes {g['stats']['node_count']}  edges {g['stats']['edge_count']}")
    kinds: dict[str, int] = {}
    for e in g["edges"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    for k, v in sorted(kinds.items()):
        print(f"  {k:16s} {v}")
    return 0


def cmd_health(args) -> int:
    inv = scan.scan()
    cfg = _cfg()

    usage_summary: dict = {}
    if not args.no_usage:
        idx = usage.build(cache_path=_usage_cache())
        cfg.plugin_usage = usage.plugin_usage(idx, inv)
        cfg.skill_usage = dict(idx.tokens)
        cfg.usage_available = idx.available
        _s = idx.summary()
        cfg.usage_complete = bool(
            idx.available
            and not _s.get('truncated')
            and not _s.get('files_skipped')
            and (_s.get('file_coverage_pct') or 0) >= 99.9
        )
        usage_summary = idx.summary()

    update_summary: dict = {}
    if args.with_updates:
        update_summary = updates_mod.check(inv, allow_network=not args.offline).summary()

    spec_summary: dict = {}
    if getattr(args, "with_specs", False) and not args.offline:
        from . import specs as specs_mod

        states = specs_mod.check(REPO_ROOT, allow_network=True)
        spec_summary = {
            "checked": len(states),
            "changed": [s.url for s in states if s.status == "changed"],
            "new": [s.url for s in states if s.status == "new"],
            "unreachable": [s.url for s in states if s.status == "unreachable"],
        }

    report = health.run(
        inv, cfg, usage=usage_summary, updates=update_summary, specs=spec_summary
    )
    saved = health.save(report, REPO_ROOT)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(health.format_text(report, show_waived=args.show_waived))
        print(f"report -> {saved}")
    return 0 if report.verdict == "PASS" else 1


def cmd_consolidate(args) -> int:
    """Ask for a consolidation plan, validate it, then show or apply it."""
    if not ai_mod.available():
        print("找不到 claude CLI。整合規劃需要它，其餘功能不受影響。")
        return 1

    inv = scan.scan()
    cfg = _cfg()
    idx = usage.build(cache_path=_usage_cache())
    cfg.plugin_usage = usage.plugin_usage(idx, inv)
    cfg.skill_usage = dict(idx.tokens)
    cfg.usage_available = idx.available
    _s = idx.summary()
    cfg.usage_complete = bool(
        idx.available
        and not _s.get('truncated')
        and not _s.get('files_skipped')
        and (_s.get('file_coverage_pct') or 0) >= 99.9
    )
    report = health.run(inv, cfg, usage=idx.summary())

    targets = [
        f
        for f in report.findings
        if not f.waived and f.owner.value == "local" and consolidate_mod.can_propose(f.rule)
    ]
    if args.only:
        targets = [f for f in targets if args.only in f.path or args.only == f.rule]
    if args.limit:
        targets = targets[: args.limit]

    if not targets:
        print("沒有需要 AI 規劃的整合項目")
        return 0

    print(f"{len(targets)} 項可規劃（{', '.join(sorted({f.rule for f in targets}))}）\n")
    total_cost = 0.0
    accepted = []
    for f in targets:
        short = f.path.replace(os.path.expanduser("~"), "~")
        print(f"[{f.rule}] {short}")
        p = consolidate_mod.propose(f, REPO_ROOT, model=args.model)
        total_cost += p.cost_usd
        if not p.ok:
            print(f"   方案被拒：{'；'.join(p.rejected_because)}\n")
            continue
        print(f"   {p.summary}")
        if p.rationale:
            print(f"   理由：{p.rationale}")
        for st in p.change_set.manifest()["changes"]:
            print(f"     {st['action']:<7} +{st['added']:<5} -{st['removed']:<5} {st['path']}")
        accepted.append(p)
        print()

    print(f"通過驗證 {len(accepted)}／{len(targets)}，AI 成本 ${total_cost:.3f}")
    if not accepted:
        return 0

    if args.apply:
        for p in accepted:
            patch.save(p.change_set, REPO_ROOT)
            result = patch.apply(p.change_set, REPO_ROOT)
            print(f"  已套用 {p.rule} {p.path}：{result['applied']} 檔，備份 {os.path.basename(result['backup'] or '')}")
    else:
        for p in accepted:
            patch.save(p.change_set, REPO_ROOT)
        print("未套用（加 --apply 才會寫入）。diff 已寫到 var/patches/")
    return 0


def cmd_specs(args) -> int:
    """Check whether the guidance the rules cite has moved."""
    states = specs_mod.check(REPO_ROOT, allow_network=not args.offline)
    changed = [s for s in states if s.status == "changed"]
    new = [s for s in states if s.status == "new"]

    for s in states:
        mark = {"unchanged": "  ok    ", "changed": "  CHANGED", "new": "  new    ",
                "unreachable": "  ???   ", "unknown": "  ?     "}[s.status]
        print(f"{mark} {s.url}")
        print(f"          規則 {', '.join(s.rules)}" + (f" — {s.note}" if s.note else ""))

    print(f"\n{len(changed)} 份有變動、{len(new)} 份尚無基準、共 {len(states)} 份")

    if changed and args.review:
        if not ai_mod.available():
            print("\n找不到 claude CLI，跳過變動分析。")
        else:
            for s in changed:
                print(f"\n--- 分析 {s.url} ---")
                r = specs_mod.review_change(s, REPO_ROOT, model=args.model)
                if not r.get("ok"):
                    print(f"  失敗：{r.get('error')}")
                    continue
                print(f"  摘要：{r['summary']}")
                for item in r.get("reviews", []):
                    print(f"  [{item.get('rule')}] {item.get('verdict')}")
                    if item.get("what_changed"):
                        print(f"      變動：{item['what_changed']}")
                    if item.get("suggested_change"):
                        print(f"      建議：{item['suggested_change']}")

    if args.accept:
        path = specs_mod.record(REPO_ROOT, states)
        print(f"\n已把目前內容記為新基準：{path}")
        print("這代表你已經看過變動並確認規則仍然正確。")
    elif changed:
        print("\n看過並確認規則仍適用後，用 --accept 更新基準。")
    return 0


def cmd_update(args) -> int:
    """Run each package's own updater. Never reimplements one."""
    inv = scan.scan()
    updates_mod.check(inv, allow_network=True)
    todo = upgrade_mod.plan(inv)
    raw_todo = list(todo)
    if args.only:
        from .server import select_update_targets

        # The same selection the dashboard uses. A bare name previously upgraded
        # every checkout sharing it, while the root-qualified form the dashboard
        # sends selected none.
        todo = select_update_targets(todo, args.only)
        if not todo:
            todo = [t for t in raw_todo if t["target"].startswith(args.only + "@")]

    if not todo:
        print("沒有偵測到可用更新")
        return 0

    print(f"{len(todo)} 項可更新：")
    for item in todo:
        auto = "可自動執行" if item["automatic"] else "需手動"
        print(f"  [{item['kind']:<7}] {item['target']:<44} {item['from']} → {item['to']}  ({auto})")
        print(f"            {item['method']}")

    if not args.apply:
        print("\n未執行（加 --apply 才會真的更新）")
        return 0

    results = []
    for item in todo:
        if not item["automatic"]:
            print(f"\n跳過 {item['target']}：沒有自動路徑，請手動執行上面的指令")
            continue
        print(f"\n更新 {item['target']} …")
        if item["kind"] == "plugin":
            r = upgrade_mod.update_plugin(item["target"])
        else:
            r = upgrade_mod.update_toolkit(item["root"], item["target"])
        results.append(r)
        print(f"  {'OK  ' if r.ok else 'FAIL'} {r.message}")
        if r.restore_hint:
            print(f"  回退：{r.restore_hint}")

    if any(r.needs_restart for r in results):
        print("\n有 plugin 更新完成，Claude Code 需要重啟才會套用。")
    return 0 if all(r.ok for r in results) else 1


def cmd_fix(args) -> int:
    """Apply the fixes whose remedy is mechanical, and say why the rest are not."""
    inv = scan.scan()
    cfg = _cfg()
    idx = usage.build(cache_path=_usage_cache())
    cfg.plugin_usage = usage.plugin_usage(idx, inv)
    cfg.skill_usage = dict(idx.tokens)
    cfg.usage_available = idx.available
    _s = idx.summary()
    cfg.usage_complete = bool(
        idx.available
        and not _s.get('truncated')
        and not _s.get('files_skipped')
        and (_s.get('file_coverage_pct') or 0) >= 99.9
    )
    report = health.run(inv, cfg, usage=idx.summary())

    info = fixes_mod.available(report.findings)
    auto = fixes_mod.bulk_keys(info)
    individual = [k for k, v in info.items() if v.get("fixable") and not v.get("bulk")]
    manual = [(k, v) for k, v in info.items() if not v.get("fixable")]

    if args.list:
        print(f"一鍵可修復 ({len(auto)}):")
        for k in auto:
            v = info[k]
            print(f"  {v['rule']:<7} {v['label']:<14} {v.get('subject') or v['path'].replace(os.path.expanduser('~'), '~')}")
        print(f"\n只能單獨決定 ({len(individual)}):")
        for k in individual[:8]:
            v = info[k]
            print(f"  {v['rule']:<7} {v['label']:<14} {v.get('subject','')}")
        if len(individual) > 8:
            print(f"  … 另外 {len(individual) - 8} 個")
        print(f"\n需要你決定 ({len(manual)}):")
        seen = set()
        for _k, v in manual:
            if v["rule"] in seen:
                continue
            seen.add(v["rule"])
            print(f"  {v['rule']:<7} {v['why']}")
        return 0

    if not auto:
        print("沒有可自動修復的項目")
        return 0

    cs, applied, skipped = fixes_mod.build_change_set(auto, report.findings, inv, cfg, REPO_ROOT)
    if not cs.has_work():
        print("沒有可套用的變更")
        return 0

    for st in cs.manifest()["changes"]:
        print(f"  {st['action']:<7} +{st['added']:<5} -{st['removed']:<5} {st['path']}")
    for d in cs.remove_dirs:
        print(f"  rmdir                 {d}")
    diff_path, _ = patch.save(cs, REPO_ROOT)
    print(f"\n{len(applied)} 項可修復，{len(skipped)} 項跳過")
    print(f"diff -> {diff_path}")

    if args.apply:
        result = patch.apply(cs, REPO_ROOT)
        print(f"applied {result['applied']}; backup -> {result['backup']}")
    else:
        print("未套用（加 --apply 才會寫入）")
    return 0


def cmd_sync(args) -> int:
    cfg = _cfg()
    changes: list[patch.Change] = []

    for target, text in canonical.targets(cfg):
        changes.append(
            patch.Change(
                path=target,
                new_text=text,
                reason="rendered from canonical sources",
                action="modify" if os.path.exists(target) else "create",
            )
        )

    for name, src, others in canonical.mirror_groups(cfg):
        if not os.path.isfile(src):
            print(f"warning: mirror {name!r} source missing: {src}", file=sys.stderr)
            continue
        with open(src, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for dest in others:
            changes.append(
                patch.Change(
                    path=dest,
                    new_text=text,
                    reason=f"mirror of {src} (group {name})",
                    action="modify" if os.path.exists(dest) else "create",
                )
            )

    cs = patch.ChangeSet(
        name="sync",
        description="Render generated instruction files and re-sync declared mirrors.",
        changes=changes,
    )
    effective = cs.effective()
    if not effective:
        print("already in sync: nothing to change")
        return 0

    diff_path, man_path = patch.save(cs, REPO_ROOT)
    print(f"{len(effective)} file(s) would change")
    for st in cs.manifest()["changes"]:
        print(f"  {st['action']:<7} +{st['added']:<5} -{st['removed']:<5} {st['path']}")
    print(f"\ndiff     -> {diff_path}")
    print(f"manifest -> {man_path}")

    if args.apply:
        result = patch.apply(cs, REPO_ROOT)
        print(f"\napplied {result['applied']} file(s)")
        print(f"backup  -> {result['backup']}")
    else:
        print("\nnot applied. re-run with --apply, or: studio apply "
              f"{diff_path.replace('.diff', '.payload.json')}")
    return 0


def cmd_apply(args) -> int:
    cs = patch.load(args.payload)
    print(f"change set {cs.name!r} ({len(cs.effective())} effective change(s))")
    result = patch.apply(cs, REPO_ROOT, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_backups(args) -> int:
    rows = patch.list_backups(REPO_ROOT)
    if not rows:
        print("no backups")
        return 0
    for b in rows:
        print(f"{b['id']}  {b.get('change_set','?'):<12} {len(b.get('changes',[]))} file(s)")
        for c in b.get("changes", []):
            print(f"    {c['path']}")
    return 0


def cmd_rollback(args) -> int:
    result = patch.rollback(REPO_ROOT, args.backup_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    serve(
        REPO_ROOT,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        allow_actions=args.allow_actions,
    )
    return 0


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studio", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="inventory local agent config").set_defaults(fn=cmd_scan)
    sub.add_parser("usage", help="build the invocation index").set_defaults(fn=cmd_usage)

    u = sub.add_parser("updates", help="check cloud plugins for updates")
    u.add_argument("--offline", action="store_true", help="skip network calls")
    u.set_defaults(fn=cmd_updates)

    g = sub.add_parser("graph", help="emit the relationship graph")
    g.add_argument("--expand-plugins", action="store_true", help="include every plugin skill as its own node")
    g.set_defaults(fn=cmd_graph)

    h = sub.add_parser("health", help="run all compliance rules")
    h.add_argument("--json", action="store_true")
    h.add_argument("--show-waived", action="store_true")
    h.add_argument("--no-usage", action="store_true", help="skip building the usage index")
    h.add_argument("--with-updates", action="store_true", help="also check plugin updates")
    h.add_argument(
        "--with-specs",
        action="store_true",
        help="also check whether the documents the rules cite have changed",
    )
    h.add_argument("--offline", action="store_true")
    h.set_defaults(fn=cmd_health)

    co = sub.add_parser("consolidate", help="AI-planned consolidation, validated before it is applied")
    co.add_argument("--apply", action="store_true", help="write the validated plans")
    co.add_argument("--only", help="restrict to one rule code or path fragment")
    co.add_argument("--limit", type=int, help="plan at most N findings")
    co.add_argument("--model", help="override the model used for planning")
    co.set_defaults(fn=cmd_consolidate)

    sp = sub.add_parser("specs", help="check whether the cited guidance has changed")
    sp.add_argument("--review", action="store_true", help="ask what changed and whether rules still hold")
    sp.add_argument("--accept", action="store_true", help="record current content as the new baseline")
    sp.add_argument("--offline", action="store_true")
    sp.add_argument("--model", help="override the model used for review")
    sp.set_defaults(fn=cmd_specs)

    up = sub.add_parser("update", help="run each package's own updater")
    up.add_argument("--apply", action="store_true", help="actually run the updates")
    up.add_argument("--only", help="restrict to one plugin or toolkit name")
    up.set_defaults(fn=cmd_update)

    fx = sub.add_parser("fix", help="apply the fixes that have one correct answer")
    fx.add_argument("--apply", action="store_true", help="write the changes (backs up first)")
    fx.add_argument("--list", action="store_true", help="list what is and is not auto-fixable")
    fx.set_defaults(fn=cmd_fix)

    s = sub.add_parser("sync", help="render canonical sources into each runtime")
    s.add_argument("--apply", action="store_true", help="write the changes (backs up first)")
    s.set_defaults(fn=cmd_sync)

    a = sub.add_parser("apply", help="apply a saved change set")
    a.add_argument("payload", help="path to a *.payload.json produced by sync")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_apply)

    sub.add_parser("backups", help="list restore points").set_defaults(fn=cmd_backups)

    r = sub.add_parser("rollback", help="restore a backup")
    r.add_argument("backup_id")
    r.set_defaults(fn=cmd_rollback)

    v = sub.add_parser("serve", help="start the local dashboard")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8787)
    v.add_argument("--no-open", action="store_true")
    v.add_argument(
        "--allow-actions",
        action="store_true",
        help="let the dashboard apply syncs and rollbacks (origin- and token-checked)",
    )
    v.set_defaults(fn=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
