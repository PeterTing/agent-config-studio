#!/usr/bin/env python3
"""Rewrite the Claude Code workflow hooks so they are correct and low-noise.

Three problems are fixed:

1. ``echo "$DIFF" | grep -qv`` fires when ``$DIFF`` is empty, because echo of an
   empty string still emits one line and that line fails the pattern, so
   ``grep -v`` selects it. Every hook now computes its file list with the filter
   inline and exits early when the list is empty.
2. The injected text gave orders ("reject this operation and run /qa-only").
   Injected context is meant to state facts; the agent decides what to do.
3. A UserPromptSubmit hook re-injected the intent-routing rules on every prompt,
   duplicating content the instruction file already carries once per session.
   It is removed.

Run with --apply to write (a backup is taken first); without it, prints the diff.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import patch  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.expanduser("~/.claude/settings.json")

DOC_FILTER = r"\.(md|txt)$|(^|/)\.gitignore$|^docs/"


def _hook(list_cmd: str, message: str) -> str:
    """Build a self-guarding hook command.

    ``list_cmd`` writes candidate paths to stdout. The pipeline then drops blank
    lines *before* the docs filter: a blank line does not match the docs pattern,
    so a ``grep -v`` filter would otherwise select it and the hook would fire on
    no real change. That is the same defect the old hooks had via ``echo "$VAR"``.
    An empty result exits 0 silently, so the hook cannot fire spuriously.
    """
    return (
        f"FILES=$({{ {list_cmd} ; }} 2>/dev/null "
        f"| grep -E '\\S' | grep -vE '{DOC_FILTER}' | sort -u || true); "
        'if [ -z "$FILES" ]; then exit 0; fi; '
        'N=$(printf \'%s\\n\' "$FILES" | grep -c . || true); '
        f'jq -n --arg n "$N" \'{{hookSpecificOutput:{{hookEventName:"PreToolUse",'
        f'additionalContext:("{message}" + $n + "。")}}}}\''
    )


#: Files that would be pushed. With an upstream, that is upstream..HEAD. Without
#: one the branch has never been pushed, so every file it ever touched is new;
#: `git log --name-only` reports that without depending on history depth (the old
#: `HEAD~5..HEAD` fallback silently reported nothing in a repo with < 6 commits).
_PUSH_SCOPE = (
    "UP=$(git rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true); "
    'if [ -n "$UP" ]; then git diff --name-only "$UP..HEAD"; '
    "else git log --name-only --pretty=format: HEAD; fi"
)

#: Files in the PR range. Falls back to the push scope when the base ref cannot
#: be resolved, so a missing remote reports rather than silently passing.
_PR_SCOPE = (
    "BASE=$(gh pr view --json baseRefName -q .baseRefName 2>/dev/null "
    "|| gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || true); "
    'if [ -n "$BASE" ] && git rev-parse --verify --quiet "origin/$BASE" >/dev/null; '
    'then git diff --name-only "origin/$BASE...HEAD"; '
    f"else {_PUSH_SCOPE}; fi"
)

NEW_PRETOOLUSE = [
    {
        "type": "command",
        "command": _hook(
            "git diff --cached --name-only --diff-filter=ACMR",
            "Commit 前流程：CLAUDE.md 對非文件 commit 要求先跑 /codex:review。目前 staged 的非文件檔案數為 ",
        ),
        "if": "Bash(git commit:*)",
        "statusMessage": "檢查 staged 的非文件變更…",
    },
    {
        "type": "command",
        "command": _hook(
            _PUSH_SCOPE,
            "Push 前流程：CLAUDE.md 對非文件變更要求先跑 /qa-only。本次要推送的非文件檔案數為 ",
        ),
        "if": "Bash(git push:*)",
        "statusMessage": "檢查待推送的非文件變更…",
    },
    {
        "type": "command",
        "command": _hook(
            _PR_SCOPE,
            "建 PR 前流程：CLAUDE.md 要求 /codex:review 與 /qa-only 都跑過。本 PR 的非文件檔案數為 ",
        ),
        "if": "Bash(gh pr create:*)",
        "statusMessage": "檢查 PR 範圍內的非文件變更…",
    },
]


def build_settings() -> str:
    with open(SETTINGS, encoding="utf-8") as fh:
        data = json.load(fh)

    hooks = data.get("hooks") or {}
    hooks["PreToolUse"] = [{"matcher": "Bash", "hooks": NEW_PRETOOLUSE}]
    # The instruction file states the routing rules once per session; repeating
    # them at every prompt boundary is the duplication Claude 5 guidance removes.
    hooks.pop("UserPromptSubmit", None)
    data["hooks"] = hooks
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cs = patch.ChangeSet(
        name="fix-hooks",
        description="Guard hook conditions against empty input, make injected "
        "context factual, and drop the per-prompt rule re-injection.",
        changes=[
            patch.Change(
                path=SETTINGS,
                new_text=build_settings(),
                reason="hook correctness and context-cost fixes",
            )
        ],
    )
    if not cs.effective():
        print("settings.json already matches the target hook configuration")
        return 0

    diff_path, _ = patch.save(cs, REPO_ROOT)
    print(cs.diff())
    print(f"\ndiff -> {diff_path}")
    if args.apply:
        result = patch.apply(cs, REPO_ROOT)
        print(f"applied; backup -> {result['backup']}")
    else:
        print("not applied (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
