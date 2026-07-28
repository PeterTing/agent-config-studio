"""Decide which enabled plugins are avoidable context cost.

This is shared by the health rule (CB001) and the remediation script on purpose.
When a rule grades one definition of "avoidable" and the fix applies a different
one, the rule can demand something the fix deliberately refuses to do and the
check never passes. One implementation, one definition.

A plugin is avoidable only when **both** hold:

* no recorded usage anywhere in local history - Claude transcripts, Codex
  rollouts, typed slash commands, MCP tool calls, subagent spawns; and
* your own configuration never names it.

The second condition matters: a plugin your instructions route work to is a
deliberate cost, not waste, even with no invocation recorded yet. Only the plugin
*name* counts as a reference. Matching on the names of the skills it ships pulls
in coincidences - `access`, `release`, `blueprint`, `framework` all appear in
unrelated prose - and each coincidence silently keeps a plugin nobody asked for.

But a mention is weak evidence, and on its own it hid a real cost: `figma` was
named in one instruction line, had **zero** recorded usage of its skills *and*
zero of its MCP tools, and still went on charging for every session. Treating
"mentioned" as a silent keep means the most expensive dead plugin is the one that
never gets reported. So a mentioned-but-never-used plugin gets its own verdict,
``review``: not disabled automatically, because the reference may be intentional,
but surfaced with the cost attached so the choice is made knowingly.
"""

from __future__ import annotations

import glob
import os
import re

from .model import Inventory, Origin

REFERENCE_GLOBS = (
    "~/.claude/CLAUDE.md",
    "~/.claude/rules/*.md",
    "~/.codex/AGENTS.md",
    "~/.claude/workflows/*.md",
    "~/.codex/workflows/*.md",
    "~/.claude/commands/*.md",
    "~/.codex/commands/*.md",
    "~/.claude/skills/*/SKILL.md",
    "~/.codex/skills/*/SKILL.md",
)

#: Shorter than this, a plugin name is too generic to treat a hit as intentional.
MIN_REFERENCE_NAME_LEN = 4


def reference_corpus(extra_globs: tuple[str, ...] = ()) -> str:
    """Concatenate everything the user hand-maintains."""
    parts: list[str] = []
    for pat in (*REFERENCE_GLOBS, *extra_globs):
        for path in glob.glob(os.path.expanduser(pat)):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    parts.append(fh.read())
            except OSError:
                continue
    return "\n".join(parts)


def mentions(corpus: str, token: str) -> bool:
    if len(token) < MIN_REFERENCE_NAME_LEN:
        return False
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", corpus) is not None


def classify(
    inv: Inventory, usage_counts: dict[str, int], corpus: str, *, runtime: str = "claude"
) -> list[dict]:
    """Return one row per enabled plugin in ``runtime`` with a keep/disable verdict."""
    skills_by_plugin: dict[str, list] = {}
    bytes_by_plugin: dict[str, int] = {}
    for s in inv.skills:
        if s.origin is not Origin.PLUGIN or not s.plugin:
            continue
        skills_by_plugin.setdefault(s.plugin, []).append(s)
        bytes_by_plugin[s.plugin] = (
            bytes_by_plugin.get(s.plugin, 0)
            + len(s.name.encode("utf-8"))
            + len(s.description.encode("utf-8"))
        )

    rows: list[dict] = []
    for p in inv.plugins:
        if not p.enabled or p.runtime.value != runtime:
            continue
        name = p.key.split("@")[0]
        hits = usage_counts.get(name, 0)
        skills = skills_by_plugin.get(name, [])

        if hits > 0:
            verdict, reason = "keep", f"{hits} recorded invocation(s)"
        elif not skills:
            # No skills means no always-on cost, so there is nothing to reclaim.
            verdict, reason = "keep", "ships no skills, so disabling saves no context"
        elif mentions(corpus, name):
            verdict, reason = (
                "review",
                "named in your config but never actually used - confirm the cost is intended",
            )
        else:
            verdict, reason = "disable", "zero recorded usage and never named in your config"

        rows.append(
            {
                "key": p.key,
                "name": name,
                "marketplace": p.marketplace,
                "verdict": verdict,
                "reason": reason,
                "invocations": hits,
                "skills": len(skills),
                "metadata_bytes": bytes_by_plugin.get(name, 0),
            }
        )
    rows.sort(key=lambda r: (r["verdict"] != "disable", -r["metadata_bytes"]))
    return rows


def avoidable(rows: list[dict]) -> tuple[int, int, list[dict]]:
    """(bytes, skill count, rows) for the plugins classified as disable."""
    disable = [r for r in rows if r["verdict"] == "disable"]
    return (
        sum(r["metadata_bytes"] for r in disable),
        sum(r["skills"] for r in disable),
        disable,
    )
