"""Behavioural tests for the workflow hook commands in examples/remediate_hooks.py.

These run the hook shell commands for real against throwaway git repositories.
The bug they exist to prevent is a condition that fires when there is nothing to
report, which taxes every unrelated tool call with an irrelevant context
injection - so "does not fire" is asserted as carefully as "does fire".
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_remediate_hooks():
    path = os.path.join(REPO_ROOT, "examples", "remediate_hooks.py")
    spec = importlib.util.spec_from_file_location("remediate_hooks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RH = _load_remediate_hooks()
HOOKS = {h["if"]: h for h in RH.NEW_PRETOOLUSE}
COMMIT = HOOKS["Bash(git commit:*)"]["command"]
PUSH = HOOKS["Bash(git push:*)"]["command"]
PR = HOOKS["Bash(gh pr create:*)"]["command"]


def run_hook(command: str, cwd: str) -> tuple[bool, str | None, int]:
    """Execute a hook command. Returns (fired, injected message, exit code)."""
    proc = subprocess.run(
        ["bash", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    out = proc.stdout.strip()
    if not out:
        return False, None, proc.returncode
    payload = json.loads(out)
    return True, payload["hookSpecificOutput"]["additionalContext"], proc.returncode


def git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def init_repo(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "test")
    git(path, "config", "commit.gpgsign", "false")
    return path


def write(path: str, name: str, body: str = "x\n") -> None:
    full = os.path.join(path, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(body)


class HookQuietWhenNothingToReport(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_silent_outside_a_git_repository(self):
        """The original defect: hooks fired on every Bash call in a non-repo."""
        for name, cmd in (("commit", COMMIT), ("push", PUSH), ("pr", PR)):
            with self.subTest(hook=name):
                fired, msg, rc = run_hook(cmd, self.tmp)
                self.assertFalse(fired, f"{name} hook fired outside a git repo: {msg}")
                self.assertEqual(rc, 0)

    def test_silent_in_a_repository_with_no_commits(self):
        repo = init_repo(os.path.join(self.tmp, "fresh"))
        for name, cmd in (("commit", COMMIT), ("push", PUSH), ("pr", PR)):
            with self.subTest(hook=name):
                fired, msg, _ = run_hook(cmd, repo)
                self.assertFalse(fired, f"{name} hook fired with no commits: {msg}")

    def test_commit_hook_silent_for_docs_only_staging(self):
        repo = init_repo(os.path.join(self.tmp, "docs"))
        write(repo, "README.md")
        write(repo, "docs/guide.md")
        write(repo, "notes.txt")
        write(repo, ".gitignore")
        git(repo, "add", "-A")
        fired, msg, _ = run_hook(COMMIT, repo)
        self.assertFalse(fired, f"fired for a docs-only commit: {msg}")


class HookFiresWhenThereIsSomethingToReport(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_commit_hook_counts_only_non_docs_files(self):
        repo = init_repo(os.path.join(self.tmp, "mixed"))
        write(repo, "README.md")
        write(repo, "app.py")
        write(repo, "lib/util.ts")
        git(repo, "add", "-A")
        fired, msg, _ = run_hook(COMMIT, repo)
        self.assertTrue(fired)
        self.assertIn("2", msg, f"expected a count of 2 non-docs files, got: {msg}")

    def test_push_hook_reports_when_branch_has_no_upstream(self):
        """Regression: the old HEAD~5..HEAD fallback reported nothing in a repo
        with fewer than six commits, silently skipping the check."""
        repo = init_repo(os.path.join(self.tmp, "noupstream"))
        write(repo, "app.py")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "first")
        write(repo, "second.py")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "second")
        fired, msg, _ = run_hook(PUSH, repo)
        self.assertTrue(fired, "push hook stayed silent with 2 unpushed code commits")
        self.assertIn("2", msg, f"expected both files counted, got: {msg}")

    def test_push_hook_silent_when_only_docs_are_unpushed(self):
        repo = init_repo(os.path.join(self.tmp, "docsonly"))
        write(repo, "README.md")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "docs")
        fired, msg, _ = run_hook(PUSH, repo)
        self.assertFalse(fired, f"fired for docs-only history: {msg}")

    def test_push_hook_uses_upstream_range_when_present(self):
        origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True, capture_output=True)
        repo = init_repo(os.path.join(self.tmp, "withupstream"))
        write(repo, "base.py")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        git(repo, "remote", "add", "origin", origin)
        git(repo, "push", "-q", "-u", "origin", "HEAD")

        # Nothing new since the upstream: must stay silent.
        fired, msg, _ = run_hook(PUSH, repo)
        self.assertFalse(fired, f"fired with nothing ahead of upstream: {msg}")

        # One new code file ahead of upstream: must report exactly that one.
        write(repo, "new.py")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "ahead")
        fired, msg, _ = run_hook(PUSH, repo)
        self.assertTrue(fired, "stayed silent while one code commit was ahead")
        self.assertIn("1", msg, f"expected a count of 1, got: {msg}")

    def test_pr_hook_falls_back_when_base_ref_is_unresolvable(self):
        """With no reachable base ref the hook reports rather than passing silently."""
        repo = init_repo(os.path.join(self.tmp, "prfallback"))
        write(repo, "app.py")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "code")
        fired, msg, _ = run_hook(PR, repo)
        self.assertTrue(fired, "PR hook stayed silent with unpushed code and no base ref")
        self.assertIn("1", msg)


class InjectedTextIsFactual(unittest.TestCase):
    """Injected context should state environment facts, not issue orders."""

    FORBIDDEN = ("請拒絕", "你必須", "you must", "please reject", "refuse")

    def test_no_imperative_phrasing(self):
        for h in RH.NEW_PRETOOLUSE:
            for phrase in self.FORBIDDEN:
                self.assertNotIn(
                    phrase.lower(),
                    h["command"].lower(),
                    f"hook {h['if']} injects an order containing {phrase!r}",
                )

    def test_every_tool_hook_is_scoped(self):
        for h in RH.NEW_PRETOOLUSE:
            self.assertTrue(h.get("if"), f"hook {h} has no `if` scope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
