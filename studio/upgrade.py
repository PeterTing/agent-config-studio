"""Run updates by driving each package's own updater.

The rule here is: never reimplement package management, but do invoke the
official one. Those are different things, and an earlier version of this tool
conflated them and refused to update anything at all.

* **Plugins** have a real CLI - ``claude plugin update <plugin>`` - which runs
  Claude Code's own install logic. That is what gets called.
* **Skill toolkits** are git checkouts. gstack documents its own git-install
  upgrade as stash, fetch, reset to origin, then run the repo's ``setup``
  script. That sequence is executed, with the pre-upgrade commit recorded first
  so it can be put back.

Anything that does not match one of those shapes is reported as "no automatic
path", with the command to run by hand. Guessing at a third mechanism is how you
end up with an updater that drifts from the real one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .versions import version_key

#: Plugin updates hit the network and can rebuild a checkout.
PLUGIN_TIMEOUT = 300
#: Toolkit upgrades additionally run the project's own setup script.
TOOLKIT_TIMEOUT = 900


@dataclass
class UpgradeResult:
    target: str
    kind: str  # "plugin" | "toolkit"
    ok: bool
    message: str
    steps: list[dict] = field(default_factory=list)
    #: How to undo it, when an undo exists.
    restore_hint: str = ""
    needs_restart: bool = False

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "kind": self.kind,
            "ok": self.ok,
            "message": self.message,
            "steps": self.steps,
            "restore_hint": self.restore_hint,
            "needs_restart": self.needs_restart,
        }


def _run(cmd: list[str], cwd: str | None = None, timeout: int = PLUGIN_TIMEOUT) -> dict:
    """Run a command, capturing everything. Never raises."""
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return {
            "cmd": " ".join(cmd),
            "cwd": cwd or "",
            "rc": p.returncode,
            "stdout": (p.stdout or "")[-4000:],
            "stderr": (p.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd), "cwd": cwd or "", "rc": -1, "stdout": "", "stderr": "timed out"}
    except OSError as exc:
        return {"cmd": " ".join(cmd), "cwd": cwd or "", "rc": -1, "stdout": "", "stderr": str(exc)}


def plugin_cli_available() -> bool:
    return shutil.which("claude") is not None


def update_plugin(key: str, scope: str = "user") -> UpgradeResult:
    """Update one plugin through Claude Code's own CLI."""
    if not plugin_cli_available():
        return UpgradeResult(
            target=key,
            kind="plugin",
            ok=False,
            message="找不到 claude CLI，無法呼叫官方更新指令。",
        )
    step = _run(["claude", "plugin", "update", key, "--scope", scope])
    ok = step["rc"] == 0
    return UpgradeResult(
        target=key,
        kind="plugin",
        ok=ok,
        message=(
            "已更新（Claude Code 需重啟才會套用）"
            if ok
            else f"更新失敗 (rc={step['rc']})：{(step['stderr'] or step['stdout'])[:200]}"
        ),
        steps=[step],
        restore_hint=f"claude plugin install {key} --scope {scope}  # 指定舊版本可回退",
        needs_restart=ok,
    )


def _git(root: str, *args: str, timeout: int = TOOLKIT_TIMEOUT) -> dict:
    return _run(["git", *args], cwd=root, timeout=timeout)


def _read_version(root: str) -> str:
    path = os.path.join(root, "VERSION")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip().split("\n")[0]
    except OSError:
        return ""


def _migration_scripts(root: str, name: str) -> list[str]:
    """Version-migration scripts shipped by the toolkit, oldest first.

    Read from the checkout *after* the upgrade, because migrations for the new
    version ship with the new version. Two layouts are supported: a top-level
    `migrations/`, and the `<name>-upgrade/migrations/` layout gstack uses.
    """
    found: list[str] = []
    for rel in (os.path.join(f"{name}-upgrade", "migrations"), "migrations"):
        d = os.path.join(root, rel)
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for f in entries:
            if f.startswith("v") and f.endswith(".sh") and os.path.isfile(os.path.join(d, f)):
                found.append(os.path.join(d, f))
    return sorted(found, key=lambda p: version_key(os.path.basename(p)[1:-3]))


def _run_migrations(root: str, name: str, old_version: str, steps: list[dict]) -> list[str]:
    """Run migrations newer than `old_version`. Failures are non-fatal, as the
    toolkit's own flow specifies, but they are surfaced rather than swallowed."""
    if not old_version:
        return []
    ran: list[str] = []
    for script in _migration_scripts(root, name):
        m_ver = os.path.basename(script)[1:-3]
        if version_key(m_ver) <= version_key(old_version):
            continue
        result = _run(["bash", script], cwd=root, timeout=TOOLKIT_TIMEOUT)
        result["migration"] = m_ver
        steps.append(result)
        ran.append(m_ver + ("" if result["rc"] == 0 else " (有錯誤，非致命)"))
    return ran


def update_toolkit(root: str, name: str) -> UpgradeResult:
    """Upgrade a git-checkout skill toolkit in place.

    Follows the sequence the toolkit documents for a git install. The current
    commit is captured first: a failed ``setup`` leaves the checkout on the new
    code, and without the old ref there is nothing to go back to.
    """
    steps: list[dict] = []
    if not os.path.isdir(os.path.join(root, ".git")):
        return UpgradeResult(
            target=name, kind="toolkit", ok=False, message="不是 git checkout，沒有自動升級路徑。"
        )

    before = _git(root, "rev-parse", "HEAD")
    steps.append(before)
    old_commit = (before["stdout"] or "").strip()
    old_version = _read_version(root)
    if before["rc"] != 0 or not old_commit:
        return UpgradeResult(
            target=name, kind="toolkit", ok=False, message="讀不到目前的 commit，中止。", steps=steps
        )

    # Local edits are stashed rather than discarded; the caller is told they exist.
    # A failed stash must stop the upgrade here. `reset --hard` two steps later
    # destroys anything the stash did not take, so continuing on a nonzero exit -
    # an unmerged index, a locked repo - silently deletes the user's work. The
    # verdict comes from the return code, not from matching git's stdout wording.
    stash = _git(root, "stash", "--include-untracked")
    steps.append(stash)
    if stash["rc"] != 0:
        return UpgradeResult(
            target=name,
            kind="toolkit",
            ok=False,
            message=(
                "git stash 失敗，已中止升級（沒有動到任何檔案）。"
                f"原因：{(stash['stderr'] or stash['stdout'] or '')[:200]}"
            ),
            steps=steps,
        )
    stashed = "Saved working directory" in (stash["stdout"] or "")

    fetch = _git(root, "fetch", "origin")
    steps.append(fetch)
    if fetch["rc"] != 0:
        return UpgradeResult(
            target=name,
            kind="toolkit",
            ok=False,
            message=f"git fetch 失敗：{(fetch['stderr'] or '')[:200]}",
            steps=steps,
            restore_hint=f"git -C {root} stash pop" if stashed else "",
        )

    head = _git(root, "rev-parse", "--abbrev-ref", "origin/HEAD")
    branch = (head["stdout"] or "").strip() or "origin/main"
    steps.append(head)

    reset = _git(root, "reset", "--hard", branch)
    steps.append(reset)
    if reset["rc"] != 0:
        return UpgradeResult(
            target=name,
            kind="toolkit",
            ok=False,
            message=f"git reset 失敗：{(reset['stderr'] or '')[:200]}",
            steps=steps,
            restore_hint=f"git -C {root} reset --hard {old_commit}",
        )

    # The toolkit's own installer, when it ships one.
    setup = os.path.join(root, "setup")
    if os.path.isfile(setup) and os.access(setup, os.X_OK):
        ran = _run([setup], cwd=root, timeout=TOOLKIT_TIMEOUT)
        steps.append(ran)
        if ran["rc"] != 0:
            return UpgradeResult(
                target=name,
                kind="toolkit",
                ok=False,
                message=f"setup 失敗 (rc={ran['rc']})，程式碼已在新版但安裝未完成。",
                steps=steps,
                restore_hint=f"git -C {root} reset --hard {old_commit} && {setup}",
            )

    # Version migrations, which the toolkit's own upgrade flow runs after setup.
    # Skipping them is how an updater silently diverges from the real one: setup
    # alone does not cover stale config or moved files.
    migrated = _run_migrations(root, name, old_version, steps)

    after = _git(root, "rev-parse", "HEAD")
    steps.append(after)
    notes = []
    if stashed:
        notes.append(f"本地修改已 stash，用 `git -C {root} stash pop` 取回")
    if migrated:
        notes.append("已跑遷移：" + "、".join(migrated))
    new_version = _read_version(root)

    return UpgradeResult(
        target=name,
        kind="toolkit",
        ok=True,
        message=(
            f"已從 {old_version or old_commit[:8]} 升級到 {new_version or (after['stdout'] or '').strip()[:8]}。"
            + ("" if not notes else " " + "；".join(notes) + "。")
        ),
        steps=steps,
        restore_hint=f"git -C {root} reset --hard {old_commit}",
    )


def plan(inventory) -> list[dict]:
    """What could be updated, and how each one would be done."""
    out: list[dict] = []
    for p in inventory.plugins:
        if p.enabled and p.update_available:
            out.append(
                {
                    "kind": "plugin",
                    "target": p.key,
                    "from": p.version or p.commit[:8],
                    "to": p.remote_revision,
                    "method": f"claude plugin update {p.key} --scope user",
                    "automatic": plugin_cli_available(),
                }
            )
    for kit in inventory.toolkits:
        if not kit.get("update_available"):
            continue
        is_git = os.path.isdir(os.path.join(kit.get("root", ""), ".git"))
        out.append(
            {
                "kind": "toolkit",
                "target": kit.get("name", ""),
                "root": kit.get("root", ""),
                "from": kit.get("local_version", ""),
                "to": kit.get("remote_version", ""),
                "method": "git stash → fetch → reset --hard origin → ./setup",
                "automatic": is_git,
                "manages": kit.get("manages_count", 0),
            }
        )
    return out
