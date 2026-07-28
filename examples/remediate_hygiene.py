#!/usr/bin/env python3
"""Remaining config hygiene: dead references, stray backups, and runtime parity.

* A workflow step invoked a script that was never written.
* Backup files sat inside the live config trees, making the real configuration
  ambiguous to read.
* An empty `CLAUDE.md.d/` implied a mechanism that was not in use.
* progress-dashboard existed for Claude only, so the same intent routed
  differently depending on which agent was running.
* Two commands shadowed skills of the same name.

`~/.agent/skills` is deliberately left in place: nothing loads it, so it costs no
context today, and relocating 861 files is a bigger intervention than the finding
warrants. It stays reported as a minor advisory.

Run with --apply to write (everything is backed up first).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import patch  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
CODEX = os.path.join(HOME, ".codex")

# 1. A step referencing a script that does not exist. The same document already
#    shows the project-level form, so point at that instead of inventing a script.
DEAD_REF_FIX = {
    "old": "~/.claude/workflows/scripts/approve-baseline.sh HOM-V-008 desktop zh-TW",
    "new": "npm run approve-baseline HOM-V-008 desktop zh-TW",
}
DEAD_REF_FILES = [
    os.path.join(CLAUDE, "workflows", "post-dev-regression.md"),
    os.path.join(CODEX, "workflows", "post-dev-regression.md"),
]

# 2. Backups and stray copies inside the live config trees.
STRAY_FILES = [
    os.path.join(CLAUDE, "CLAUDE.md.bak-progress-dashboard-20260518-1755"),
    os.path.join(CLAUDE, "settings.json.bak-2026-07-20T15-39-59-864Z"),
    os.path.join(CODEX, "AGENTS.md.bak-20260408-1600"),
    os.path.join(CODEX, "AGENTS.md.bak-progress-dashboard-20260518-1755"),
]
STRAY_DIRS = [os.path.join(CLAUDE, "skills", "ui-ux-pro-max.backup-20260715-192659")]
EMPTY_DIRS = [os.path.join(CLAUDE, "CLAUDE.md.d")]

# 3. Claude-only pieces that the Codex runtime also needs for parity.
PARITY_FILES = [
    (
        os.path.join(CLAUDE, "workflows", "progress-dashboard.md"),
        os.path.join(CODEX, "workflows", "progress-dashboard.md"),
    ),
    (
        os.path.join(CLAUDE, "skills", "progress-dashboard", "SKILL.md"),
        os.path.join(CODEX, "skills", "progress-dashboard", "SKILL.md"),
    ),
]

# 4. Commands shadowed by a skill of the same name; the skill is the maintained one.
SHADOWED_COMMANDS = [
    os.path.join(CLAUDE, "commands", "review.md"),
    os.path.join(CLAUDE, "commands", "progress-dashboard.md"),
]


def build() -> patch.ChangeSet:
    changes: list[patch.Change] = []

    for path in DEAD_REF_FILES:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if DEAD_REF_FIX["old"] not in text:
            continue
        changes.append(
            patch.Change(
                path=path,
                new_text=text.replace(DEAD_REF_FIX["old"], DEAD_REF_FIX["new"]),
                reason="referenced a script that does not exist",
            )
        )

    for path in STRAY_FILES:
        if os.path.isfile(path):
            changes.append(
                patch.Change(
                    path=path,
                    new_text="",
                    action="delete",
                    reason="backup file inside the live config tree",
                )
            )

    for src, dest in PARITY_FILES:
        if not os.path.isfile(src):
            continue
        with open(src, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        changes.append(
            patch.Change(
                path=dest,
                new_text=text,
                action="modify" if os.path.exists(dest) else "create",
                reason="runtime parity: mirror of the Claude copy",
            )
        )

    for path in SHADOWED_COMMANDS:
        if os.path.isfile(path):
            changes.append(
                patch.Change(
                    path=path,
                    new_text="",
                    action="delete",
                    reason="a skill of the same name already owns this",
                )
            )

    return patch.ChangeSet(
        name="fix-hygiene",
        description="Repair a dead reference, remove stray backups, mirror "
        "progress-dashboard to Codex, and drop commands shadowed by skills.",
        changes=changes,
    )


def move_dirs(apply: bool) -> list[str]:
    """Relocate stray directories and drop empty scaffolding directories."""
    actions: list[str] = []
    archive = os.path.join(REPO_ROOT, "var", "backups", "imported-stray-dirs")
    for path in STRAY_DIRS:
        if not os.path.isdir(path):
            continue
        dest = os.path.join(archive, os.path.basename(path))
        actions.append(f"move {path} -> {dest}")
        if apply:
            os.makedirs(archive, exist_ok=True)
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.move(path, dest)
    for path in EMPTY_DIRS:
        if os.path.isdir(path) and not os.listdir(path):
            actions.append(f"rmdir {path}")
            if apply:
                os.rmdir(path)
    return actions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cs = build()
    effective = cs.effective()
    dir_actions = move_dirs(apply=False)

    if not effective and not dir_actions:
        print("nothing to do")
        return 0

    for st in cs.manifest()["changes"]:
        print(f"  {st['action']:<7} +{st['added']:<5} -{st['removed']:<5} {st['path']}")
    for a in dir_actions:
        print(f"  {a}")

    if effective:
        diff_path, _ = patch.save(cs, REPO_ROOT)
        print(f"\ndiff -> {diff_path}")

    if args.apply:
        if effective:
            res = patch.apply(cs, REPO_ROOT)
            print(f"applied {res['applied']} file change(s); backup -> {res['backup']}")
        moved = move_dirs(apply=True)
        for a in moved:
            print(f"done: {a}")
    else:
        print("not applied (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
