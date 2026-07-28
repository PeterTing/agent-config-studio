#!/usr/bin/env python3
"""Disable Claude Code plugins that are provably unused, and only those.

Selection requires two independent signals, because either one alone gets it
wrong:

1. **Zero recorded usage** across the whole local history - Claude transcripts,
   Codex rollouts, typed slash commands, MCP tool calls and subagent spawns.
   Coverage is reported; anything short of full coverage aborts.
2. **Not referenced by your own configuration** - a plugin named in an
   instruction file, workflow or hand-authored skill is kept even with zero
   recorded calls, because the config expects it to be there. `figma` is the
   clear case: no skill invocation on record, but the instructions route design
   work to Figma MCP.

Codex-side plugins are deliberately left alone: they contribute nothing to the
Claude Code startup context, so disabling them buys no measurable saving.

Run with --apply to write (settings.json is backed up first).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import patch, plugins as plugins_mod, scan, usage  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.expanduser("~/.claude/settings.json")


def decide() -> dict:
    inv = scan.scan()
    idx = usage.build()
    counts = usage.plugin_usage(idx, inv)

    summary = idx.summary()
    if summary.get("truncated") or (summary.get("file_coverage_pct") or 0) < 99.9:
        raise SystemExit(
            "usage index coverage is incomplete "
            f"({summary.get('file_coverage_pct')}% of {summary.get('transcripts_total')} "
            "transcripts). Refusing to call a plugin unused on partial evidence."
        )

    corpus = plugins_mod.reference_corpus((os.path.join(REPO_ROOT, "canonical", "*.md"),))
    return {"rows": plugins_mod.classify(inv, counts, corpus), "coverage": summary}


def build_settings(disable_keys: set[str]) -> str:
    with open(SETTINGS, encoding="utf-8") as fh:
        data = json.load(fh)
    plugins = data.get("enabledPlugins") or {}
    for key in disable_keys:
        if key in plugins:
            plugins[key] = False
    data["enabledPlugins"] = plugins
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    result = decide()
    rows = result["rows"]
    cov = result["coverage"]
    print(
        f"usage coverage: {cov['file_coverage_pct']}% of {cov['transcripts_total']} transcripts "
        f"({cov['bytes_read'] / 1e9:.1f} GB); {cov['total_invocations']} skill calls, "
        f"{cov['mcp_tool_calls']} MCP calls, {cov['agent_spawns']} agent spawns, "
        f"{cov['codex_tool_calls']} Codex tool calls\n"
    )

    to_disable = [r for r in rows if r["verdict"] == "disable"]
    keeps = [r for r in rows if r["verdict"] == "keep"]

    print(f"{'plugin':46s} {'skills':>6s} {'bytes':>8s}  reason")
    print("-" * 100)
    for r in to_disable:
        print(f"DISABLE {r['key']:38s} {r['skills']:6d} {r['metadata_bytes']:8,d}  {r['reason']}")
    print()
    for r in keeps:
        print(f"keep    {r['key']:38s} {r['skills']:6d} {r['metadata_bytes']:8,d}  {r['reason']}")

    saved = sum(r["metadata_bytes"] for r in to_disable)
    n_skills = sum(r["skills"] for r in to_disable)
    print(
        f"\n{len(to_disable)} plugin(s) to disable: {n_skills} skills, "
        f"{saved:,} bytes ≈ {saved // 4:,} tokens off every future session."
    )
    print(f"{len(keeps)} kept.")

    if not to_disable:
        return 0

    cs = patch.ChangeSet(
        name="disable-unused-plugins",
        description=f"Disable {len(to_disable)} Claude Code plugin(s) with zero recorded "
        "usage and no reference from your own configuration.",
        changes=[
            patch.Change(
                path=SETTINGS,
                new_text=build_settings({r["key"] for r in to_disable}),
                reason="context budget: unused plugin metadata",
            )
        ],
    )
    if not cs.effective():
        print("\nsettings.json already reflects this decision")
        return 0

    diff_path, _ = patch.save(cs, REPO_ROOT)
    print(f"\ndiff -> {diff_path}")
    if args.apply:
        res = patch.apply(cs, REPO_ROOT)
        print(f"applied; backup -> {res['backup']}")
        print("restore with: python3 -m studio.cli rollback " + os.path.basename(res["backup"]))
    else:
        print("not applied (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
