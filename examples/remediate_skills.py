#!/usr/bin/env python3
"""Bring the hand-authored skills into line with the Agent Skills specification.

Three classes of fix:

* **Descriptions** - a description is the only thing preloaded at startup, so a
  skill whose description is just its title can never be selected. Each one is
  rewritten to state what the skill does and when to use it.
* **Size** - SKILL.md bodies over the 500-line budget are split, with long-form
  detail moved into one-level-deep reference files.
* **Self-contradiction** - agent-browser's frontmatter calls it a fallback while
  its body still called it the default.

Run with --apply to write (a backup is taken first); without it, prints the diff.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import patch, refactor  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_SKILLS = os.path.expanduser("~/.claude/skills")
CODEX_SKILLS = os.path.expanduser("~/.codex/skills")

# --------------------------------------------------------------------------- #
# Descriptions: what it does + when to use it, third person.
# --------------------------------------------------------------------------- #

DESCRIPTIONS = {
    f"{CLAUDE_SKILLS}/api-design-reviewer/SKILL.md": (
        "Reviews REST API designs for convention and consistency problems: lints OpenAPI "
        "specs, detects breaking changes between versions, and produces a design scorecard "
        "covering naming, status codes, pagination, versioning and security headers. Use when "
        "reviewing an API design or OpenAPI/Swagger spec, adding or changing an endpoint, "
        "checking whether a change is backwards compatible, or when the user mentions REST "
        "conventions, API contract review, or breaking changes."
    ),
    f"{CLAUDE_SKILLS}/ci-cd-pipeline-builder/SKILL.md": (
        "Generates CI/CD pipelines from the stack signals actually present in a repository, "
        "covering build, test, lint and environment-aware deploy stages. Use when bootstrapping "
        "CI for a repository that has none, adding test or deploy stages to an existing "
        "pipeline, or when the user mentions CI, CD, GitHub Actions, build pipeline, or "
        "deployment stages."
    ),
    f"{CLAUDE_SKILLS}/email-template-builder/SKILL.md": (
        "Builds transactional email systems: React Email templates for welcome, verification, "
        "password reset, invoice, notification and digest mail, provider integration for Resend, "
        "Postmark, SendGrid or AWS SES, plus a preview server, i18n, dark mode, spam-score "
        "optimisation and open/click tracking. Use when creating or changing transactional "
        "email, wiring an email provider, or when the user mentions email templates, React "
        "Email, or email deliverability."
    ),
    f"{CLAUDE_SKILLS}/performance-profiler/SKILL.md": (
        "Profiles Node.js, Python and Go applications to locate CPU, memory and I/O bottlenecks: "
        "generates flamegraphs, captures heap snapshots, detects leaks, analyses bundle size, "
        "inspects slow database queries and runs k6 or Artillery load tests, measuring before "
        "and after. Use when something is slow, memory grows over time, a bundle is too large, "
        "or the user mentions profiling, flamegraph, memory leak, bottleneck, or load testing."
    ),
    f"{CODEX_SKILLS}/gh-address-comments/SKILL.md": (
        "Finds the open GitHub PR for the current branch and works through its review and issue "
        "comments with the gh CLI, verifying gh authentication first and prompting the user to "
        "log in when it is missing. Use when addressing PR feedback, replying to or resolving "
        "review comments, or when the user mentions PR comments, review feedback, or gh pr review."
    ),
    f"{CODEX_SKILLS}/playwright-interactive/SKILL.md": (
        "Drives a persistent Playwright session through js_repl so browser and Electron handles "
        "stay alive across iterations, giving fast functional and visual UI debugging without "
        "restarting the toolchain. Use when iteratively debugging a local web or Electron app, "
        "taking repeated screenshots of the same running app, or checking viewport fit and "
        "layout. Requires js_repl to be enabled."
    ),
}

# --------------------------------------------------------------------------- #
# Splits: keep the decision-making content, move long-form detail out.
# --------------------------------------------------------------------------- #

AGENT_BROWSER_MOVES = [
    {
        "target": "reference/qa-patterns.md",
        "heading": "agent-browser QA patterns",
        "note": "End-to-end recipes. Read the one that matches the task at hand.",
        "sections": [
            "Batch Execution",
            "Local App Verification Pattern",
            "Form QA Pattern",
            "Auth And Session State",
            "Figma Runtime QA Pattern",
            "Admin-To-Frontend QA Pattern",
            "Multi-Session QA",
        ],
    },
    {
        "target": "reference/diagnostics.md",
        "heading": "agent-browser diagnostics and evidence capture",
        "note": "Console, network, viewport, frame, evaluation and tracing commands.",
        "sections": [
            "Console, Errors, Network, And HAR",
            "Viewport, Devices, Media, And Responsive QA",
            "Tabs, Windows, Frames, Dialogs",
            "JavaScript Evaluation",
            "Diff, Trace, Profiling, Video, React, Web Vitals",
        ],
    },
    {
        "target": "reference/troubleshooting.md",
        "heading": "agent-browser troubleshooting and reporting",
        "note": "Read when a command fails, or when writing up the evidence.",
        "sections": ["Troubleshooting", "Evidence Report Template"],
    },
]

SPLITS = [
    {
        "path": f"{CLAUDE_SKILLS}/agent-browser/SKILL.md",
        "moves": AGENT_BROWSER_MOVES,
        "pointer_note": "Detail lives in these files and loads only when the task needs it.",
    },
    {
        "path": f"{CODEX_SKILLS}/agent-browser/SKILL.md",
        "moves": AGENT_BROWSER_MOVES,
        "pointer_note": "Detail lives in these files and loads only when the task needs it.",
    },
    {
        "path": f"{CLAUDE_SKILLS}/senior-secops/SKILL.md",
        "moves": [
            {
                "target": "reference/standards-and-tooling.md",
                "heading": "SecOps standards and tooling reference",
                "note": "Look-up material: tool flags, compliance control mappings, "
                "secure-coding examples and supply-chain commands.",
                "sections": [
                    "Tool Reference",
                    "Compliance Frameworks",
                    "Best Practices",
                    "Secret Scanning Tools",
                    "Supply Chain Security",
                ],
            }
        ],
        "pointer_note": "",
    },
    {
        "path": f"{CODEX_SKILLS}/playwright-interactive/SKILL.md",
        "moves": [
            {
                "target": "reference/screenshot-examples.md",
                "heading": "Playwright screenshot examples",
                "note": "Worked screenshot and visual-QA examples.",
                "sections": ["Screenshot Examples"],
            },
            {
                "target": "reference/bootstrap.md",
                "heading": "Playwright session bootstrap",
                "note": "One-time bootstrap code for a js_repl Playwright session.",
                "sections": ["Bootstrap (Run Once)"],
            },
        ],
        "pointer_note": "",
    },
]

# --------------------------------------------------------------------------- #
# Self-contradiction: body text that contradicts the frontmatter.
# --------------------------------------------------------------------------- #

TEXT_FIXES = [
    {
        "path": f"{CLAUDE_SKILLS}/agent-browser/SKILL.md",
        "old": "`agent-browser` is the default browser automation tool for agent-driven QA in "
        "this environment.",
        "new": "`agent-browser` is a fallback browser automation tool for agent-driven QA, used "
        "when the built-in browser is unavailable in this environment.",
        "reason": "body contradicted the frontmatter's fallback-first policy",
    },
]


def build() -> patch.ChangeSet:
    changes: list[patch.Change] = []

    for path, desc in DESCRIPTIONS.items():
        if not os.path.isfile(path):
            print(f"skip (missing): {path}", file=sys.stderr)
            continue
        changes.append(refactor.set_description(path, desc))

    # Splits produce a modified SKILL.md plus new reference files. Applying the
    # text fixes first would be overwritten by the split's full-file rewrite, so
    # the split runs against text that already includes them.
    pending_text: dict[str, list[dict]] = {}
    for fix in TEXT_FIXES:
        pending_text.setdefault(fix["path"], []).append(fix)

    split_paths = {s["path"] for s in SPLITS}
    for path, fixes in pending_text.items():
        if path in split_paths:
            continue  # handled inside the split below
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for fix in fixes:
            if fix["old"] not in text:
                raise ValueError(f"{path}: text to replace not found: {fix['old'][:60]!r}")
            text = text.replace(fix["old"], fix["new"])
        changes.append(patch.Change(path=path, new_text=text, reason=fixes[0]["reason"]))

    for spec in SPLITS:
        path = spec["path"]
        if not os.path.isfile(path):
            print(f"skip (missing): {path}", file=sys.stderr)
            continue
        produced = refactor.split_skill(
            path, spec["moves"], pointer_note=spec.get("pointer_note", "")
        )
        for change in produced:
            if change.path == path:
                for fix in pending_text.get(path, []):
                    if fix["old"] in change.new_text:
                        change.new_text = change.new_text.replace(fix["old"], fix["new"])
                # Descriptions are rewritten above; re-apply so the split keeps them.
                if path in DESCRIPTIONS:
                    tmp = change.new_text
                    change.new_text = _swap_description(tmp, DESCRIPTIONS[path])
            changes.append(change)

    # A later change to the same path must win, so collapse duplicates keeping
    # the last one queued for each path.
    merged: dict[str, patch.Change] = {}
    for c in changes:
        merged[c.path] = c

    return patch.ChangeSet(
        name="fix-skills",
        description="Rewrite thin descriptions, split oversized SKILL.md bodies into "
        "reference files, and remove a body/frontmatter contradiction.",
        changes=list(merged.values()),
    )


def _swap_description(text: str, description: str) -> str:
    """Apply a description replacement to in-memory text."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        tmp = fh.name
    try:
        return refactor.set_description(tmp, description).new_text
    finally:
        os.unlink(tmp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show-diff", action="store_true")
    args = ap.parse_args()

    cs = build()
    effective = cs.effective()
    if not effective:
        print("skills already match the target state")
        return 0

    diff_path, man_path = patch.save(cs, REPO_ROOT)
    for st in cs.manifest()["changes"]:
        print(f"  {st['action']:<7} +{st['added']:<5} -{st['removed']:<5} {st['path']}")
    if args.show_diff:
        print()
        print(cs.diff())
    print(f"\ndiff -> {diff_path}")

    if args.apply:
        result = patch.apply(cs, REPO_ROOT)
        print(f"applied {result['applied']} file(s); backup -> {result['backup']}")
    else:
        print("not applied (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
