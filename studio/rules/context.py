"""Context-budget and hygiene checks.

"The context window is a public good": every skill's name and description is
preloaded at startup whether or not the skill is ever used, so an over-installed
plugin set is a permanent tax on every session.
"""

from __future__ import annotations

import os

from ..model import Inventory, Origin, Severity
from . import (
    AVOIDABLE_METADATA_TOKEN_BUDGET,
    BYTES_PER_TOKEN,
    SPEC_CTX5,
    SPEC_SKILLS,
    Config,
    LazyRegistry,
    make,
    rule,
)

REG = LazyRegistry()

#: Directory entries that indicate a leftover backup rather than live config.
_STRAY_MARKERS = (".bak", ".backup", ".old", ".orig", "-bak-", ".save")


def _metadata_by_runtime(inv: Inventory) -> dict[str, dict]:
    """Preloaded metadata per runtime.

    Each runtime only preloads its own skills, so a single combined figure would
    overstate what either session actually pays. Plugin skills are attributed to
    the Claude Code runtime, which is what installs them.
    """
    buckets: dict[str, dict] = {}
    for s in inv.skills:
        if s.origin is Origin.ORPHAN_LIBRARY:
            continue  # not wired into any runtime, so not preloaded
        runtime = "claude" if s.origin is Origin.PLUGIN else s.runtime.value
        b = buckets.setdefault(runtime, {"bytes": 0, "count": 0, "by_origin": {}})
        n = len(s.name.encode("utf-8")) + len(s.description.encode("utf-8"))
        b["bytes"] += n
        b["count"] += 1
        b["by_origin"][s.origin.value] = b["by_origin"].get(s.origin.value, 0) + n
    return buckets


@rule(
    "CB001",
    "Avoidable preloaded skill metadata from unused plugins",
    Severity.IMPORTANT,
    SPEC_SKILLS,
    "context",
)
def cb001(inv: Inventory, cfg: Config):
    """Grade only the avoidable share of preloaded metadata.

    Total preloaded metadata is partly irreducible - your own skills and the
    plugins your config depends on have to be there - so penalising the total
    would make the check permanently unsatisfiable. `studio.plugins` decides what
    counts as avoidable, and the remediation script uses the same function, so
    the rule can never ask for something the fix refuses to do.

    Silent without a usage index: "unused" is not assertable without evidence.
    """
    if not cfg.usage_available or not cfg.usage_complete:
        # Same bar as CB002/CB007/CB008. It feeds the bulk-disable path, so
        # concluding "unused" from partial history here is the most costly
        # version of that mistake.
        return

    from ..plugins import avoidable, classify, reference_corpus

    corpus = reference_corpus((os.path.join(cfg.repo_root, "canonical", "*.md"),))
    rows = classify(inv, cfg.plugin_usage, corpus)
    total_bytes, skills, disable = avoidable(rows)
    est = total_bytes // BYTES_PER_TOKEN
    if est <= AVOIDABLE_METADATA_TOKEN_BUDGET:
        return

    worst = sorted(disable, key=lambda r: -r["metadata_bytes"])[:8]
    yield make(
        REG["CB001"],
        f"{skills} skills from {len(disable)} plugin(s) with no recorded usage and no "
        f"mention in your configuration contribute {total_bytes:,} bytes (~{est:,} tokens) "
        f"to every session, against a {AVOIDABLE_METADATA_TOKEN_BUDGET:,}-token guardrail. "
        "Largest: "
        + ", ".join(f"{r['name']} ({r['skills']} skills, {r['metadata_bytes']:,}B)" for r in worst)
        + ".",
        path=os.path.join(inv.roots.get("claude", ""), "settings.json"),
        evidence={
            "avoidable_skills": skills,
            "avoidable_bytes": total_bytes,
            "avoidable_est_tokens": est,
            "budget_tokens": AVOIDABLE_METADATA_TOKEN_BUDGET,
            "plugins": [
                {k: r[k] for k in ("key", "skills", "metadata_bytes", "reason")} for r in disable
            ],
        },
        remedy="Run scripts/remediate_plugins.py --apply; it disables exactly these and "
        "backs up settings.json first.",
    )


@rule(
    "CB002",
    "Enabled plugin shows no recorded usage",
    Severity.MINOR,
    SPEC_CTX5,
    "context",
)
def cb002(inv: Inventory, cfg: Config):
    """Reports only what the usage index can support.

    When no usage index is available the rule stays silent rather than guessing:
    calling a plugin unused on absent evidence would be exactly the kind of
    unverified claim this tool exists to prevent.
    """
    if not cfg.usage_available or not cfg.usage_complete:
        return  # calling something unused needs complete history, not merely some
    for p in inv.plugins:
        if not p.enabled:
            continue
        if p.runtime.value != "claude":
            # The registered fixer edits ~/.claude/settings.json, so a button on a
            # Codex plugin would appear to work and change nothing. Reporting a
            # remedy that cannot be carried out is worse than not offering one.
            continue
        plugin_name = p.key.split("@")[0]
        hits = cfg.plugin_usage.get(plugin_name, 0)
        if hits > 0:
            continue
        yield make(
            REG["CB002"],
            f"{p.key} is enabled and contributes {p.skill_count} skill(s), but the "
            "local usage index records zero invocations of its skills or commands.",
            path=os.path.join(inv.roots.get("claude", ""), "settings.json"),
            evidence={
                "plugin": p.key,
                "runtime": p.runtime.value,
                "skill_count": p.skill_count,
                "recorded_invocations": 0,
            },
            remedy=f"Disable {p.key} unless you need it; re-enabling is one setting flip.",
        )


@rule(
    "CB003",
    "Unreferenced skill library present on disk",
    Severity.MINOR,
    SPEC_CTX5,
    "context",
)
def cb003(inv: Inventory, cfg: Config):
    orphans = [s for s in inv.skills if s.origin is Origin.ORPHAN_LIBRARY]
    if not orphans:
        return
    root = os.path.join(inv.roots.get("agent_library", ""), "skills")
    total = sum(len(s.name) + len(s.description) for s in orphans)
    yield make(
        REG["CB003"],
        f"{len(orphans)} SKILL.md files under {root} are not referenced by any "
        "instruction, settings file, or config. They cost nothing today, but any "
        f"harness that discovers this directory would preload ~{total // BYTES_PER_TOKEN:,} "
        "tokens of metadata.",
        path=root,
        evidence={"skill_count": len(orphans), "metadata_bytes": total, "root": root},
        remedy="Move the library outside the home-level agent directories, or delete it.",
    )


@rule(
    "CB004",
    "Backup or stray file left inside a live config directory",
    Severity.MINOR,
    SPEC_CTX5,
    "context",
)
def cb004(inv: Inventory, cfg: Config):
    for label, root in inv.roots.items():
        if not root or not os.path.isdir(root):
            continue
        checked = [root, os.path.join(root, "skills")]
        for base in checked:
            if not os.path.isdir(base):
                continue
            try:
                entries = sorted(os.listdir(base))
            except OSError:
                continue
            for entry in entries:
                low = entry.lower()
                if not any(m in low for m in _STRAY_MARKERS):
                    continue
                full = os.path.join(base, entry)
                yield make(
                    REG["CB004"],
                    f"{entry} sits inside the live {label} config tree. It is not loaded, "
                    "but it makes the real configuration ambiguous to read and to audit.",
                    path=full,
                    evidence={"root": label, "entry": entry, "is_dir": os.path.isdir(full)},
                    remedy="Move backups outside the config tree (this repo keeps them under var/backups/).",
                )


@rule(
    "CB005",
    "Empty scaffolding directory in a config tree",
    Severity.MINOR,
    SPEC_CTX5,
    "context",
)
def cb005(inv: Inventory, cfg: Config):
    candidates = [
        os.path.join(inv.roots.get("claude", ""), "CLAUDE.md.d"),
        os.path.join(inv.roots.get("claude", ""), "rules"),
    ]
    for path in candidates:
        if not path or not os.path.isdir(path):
            continue
        try:
            if any(os.listdir(path)):
                continue
        except OSError:
            continue
        yield make(
            REG["CB005"],
            f"{os.path.basename(path)}/ exists but is empty, implying a mechanism that "
            "is not actually in use.",
            path=path,
            evidence={"path": path},
            remedy="Remove it, or start using it for path-scoped rules.",
        )


@rule(
    "CB006",
    "The same plugin is enabled from two marketplaces",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "context",
)
def cb006(inv: Inventory, cfg: Config):
    """Two installs of one plugin means two copies of its metadata preloaded and
    two candidates for every skill name it ships."""
    by_name: dict[str, list] = {}
    for p in inv.plugins:
        if not p.enabled:
            continue
        by_name.setdefault((p.runtime.value, p.key.split("@")[0]), []).append(p)
    for (runtime, name), group in sorted(by_name.items()):
        if len(group) < 2:
            continue
        markets = [p.marketplace for p in group]
        yield make(
            REG["CB006"],
            f"{name!r} is enabled {len(group)} times in the {runtime} runtime, from "
            f"marketplaces {markets}. Both installs preload their metadata and both "
            "claim the same skill names.",
            path=os.path.join(inv.roots.get("claude", ""), "settings.json"),
            evidence={"plugin": name, "runtime": runtime, "marketplaces": markets,
                      "keys": [p.key for p in group]},
            remedy="Disable all but one of them.",
        )


#: A cold skill is only worth reporting once its cost is worth an action.
COLD_SKILL_TOKEN_FLOOR = 300


@rule(
    "CB007",
    "Skills you have never used are still charged to every session",
    Severity.MINOR,
    SPEC_CTX5,
    "context",
)
def cb007(inv: Inventory, cfg: Config):
    """Report hand-authored and toolkit skills with no recorded invocation.

    CB001 and CB002 grade *plugins*, which left the larger half unexamined: your
    own skills and a toolkit's are preloaded on exactly the same terms, and
    nothing was checking them. In the setup this was written against that gap hid
    roughly 7,400 tokens across 71 skills.

    Two deliberate limits:

    * **Plugin skills are excluded.** They cannot be removed individually - a
      plugin is all-or-nothing - so listing them here would be noise CB001
      already covers at the level where action is possible.
    * **Never auto-fixable.** A skill fires when its description matches the
      conversation, so "never invoked" means "has not come up yet", not "useless".
      Only a person can tell those apart, which is why this reports and stops.
    """
    if not cfg.usage_available or not cfg.usage_complete:
        # An index recording nothing IS a claim - but only when it read
        # everything. Partial history cannot support "never invoked".
        return

    cold: list = []
    for s in inv.skills:
        if s.origin in (Origin.ORPHAN_LIBRARY, Origin.PLUGIN):
            continue
        if cfg.skill_usage.get(s.name, 0) > 0:
            continue
        cold.append(s)
    if not cold:
        return

    def weight(s):
        return len(s.name.encode("utf-8")) + len(s.description.encode("utf-8"))

    # The floor applies per runtime, because that is the unit being reported:
    # a runtime whose own cold skills are trivial should not be dragged into a
    # finding by the other runtime's total.
    for runtime in sorted({s.runtime.value for s in cold}):
        group = [s for s in cold if s.runtime.value == runtime]
        g_bytes = sum(weight(s) for s in group)
        if g_bytes // BYTES_PER_TOKEN < COLD_SKILL_TOKEN_FLOOR:
            continue
        worst = sorted(group, key=weight, reverse=True)
        yield make(
            REG["CB007"],
            f"{len(group)} skill(s) in the {runtime} runtime have no recorded invocation "
            f"anywhere in your local history, yet their names and descriptions cost "
            f"{g_bytes:,} bytes (~{g_bytes // BYTES_PER_TOKEN:,} tokens) at the start of "
            "every session. Largest: "
            + ", ".join(f"{s.name} (~{weight(s) // BYTES_PER_TOKEN} tok)" for s in worst[:6])
            + ".",
            path=worst[0].path,
            evidence={
                "runtime": runtime,
                "cold_skills": len(group),
                "bytes": g_bytes,
                "est_tokens": g_bytes // BYTES_PER_TOKEN,
                "names": sorted(s.name for s in group),
            },
            remedy=(
                "Review the list and delete the ones you no longer want. A skill with no "
                "invocation is not necessarily dead - it may simply never have matched a "
                "conversation - so decide per skill rather than in bulk."
            ),
            owner=_cold_owner(group),
        )


def _cold_owner(group):
    """Yours unless every one of them came from a toolkit.

    A mixed group still contains files you can delete, so calling the whole
    finding vendor-owned would drop it out of the verdict and hide work you can
    actually do."""
    from ..model import Owner

    return Owner.VENDOR if all(s.origin is Origin.TOOLKIT for s in group) else Owner.LOCAL


@rule(
    "CB008",
    "Plugin named in your config but never actually used",
    Severity.MINOR,
    SPEC_CTX5,
    "context",
)
def cb008(inv: Inventory, cfg: Config):
    """Surface the plugins a mention was quietly absolving.

    The classifier keeps a plugin your configuration names even with no recorded
    usage, because routing work to it is a deliberate choice. That is right, but
    as a *silent* keep it hid the worst case there is: an expensive plugin that
    is mentioned once and never actually invoked, by skill or by MCP tool. This
    reports those with the cost attached; it never disables anything.
    """
    if not cfg.usage_available or not cfg.usage_complete:
        return

    from ..plugins import classify, reference_corpus

    corpus = reference_corpus((os.path.join(cfg.repo_root, "canonical", "*.md"),))
    for r in classify(inv, cfg.plugin_usage, corpus):
        if r["verdict"] != "review" or not r["metadata_bytes"]:
            continue
        est = r["metadata_bytes"] // BYTES_PER_TOKEN
        yield make(
            REG["CB008"],
            f"{r['key']} adds {r['skills']} skill(s) (~{est:,} tokens) to every session, "
            "but your local history records zero invocations of its skills, commands or "
            "MCP tools. It is kept only because your configuration mentions the name.",
            path=os.path.join(inv.roots.get("claude", ""), "settings.json"),
            evidence={
                "plugin": r["name"],
                "key": r["key"],
                "skills": r["skills"],
                "metadata_bytes": r["metadata_bytes"],
                "est_tokens": est,
                "invocations": 0,
            },
            remedy=(
                "Either confirm you want it (the mention is doing its job and it will be "
                f"used), or disable it: claude plugin disable {r['key']} --scope user."
            ),
        )
