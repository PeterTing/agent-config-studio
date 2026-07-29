"""Discover externally-managed skill toolkits and whether they have updates.

Some skill collections are not plugins: they are git repositories cloned into a
skills directory, which then install their sibling skills alongside themselves.
gstack works this way. Those skills sit at a path that looks hand-authored, but
editing them is undone by the toolkit's next upgrade, so they must be classified
by provenance rather than by location.

Provenance is read from the toolkit itself - its git remote, its VERSION file,
and the set of skill directories it ships - so the classification stays correct
when the toolkit changes, with nothing hardcoded about any particular toolkit.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

FETCH_TIMEOUT = 15

#: Remote VERSION URL patterns for common git hosts, tried in order.
_RAW_URL_BUILDERS = (
    lambda owner, repo, branch: f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/VERSION",
)
_DEFAULT_BRANCHES = ("main", "master")

from .versions import version_key

@dataclass
class Toolkit:
    name: str
    root: str
    #: Directory the toolkit installs its skills into.
    install_dir: str
    remote: str = ""
    local_version: str = ""
    remote_version: str = ""
    commit: str = ""
    #: Skill directory names this toolkit ships.
    manages: list[str] = field(default_factory=list)
    update_available: bool | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "root": self.root,
            "install_dir": self.install_dir,
            "remote": self.remote,
            "local_version": self.local_version,
            "remote_version": self.remote_version,
            "commit": self.commit,
            "manages_count": len(self.manages),
            "manages": sorted(self.manages),
            "update_available": self.update_available,
            "note": self.note,
        }


def _git(root: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _parse_remote(remote: str) -> tuple[str, str] | None:
    m = re.search(r"[:/]([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$", remote)
    if not m:
        return None
    return m.group(1), m.group(2)


def _fetch(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 - fixed https hosts
            if resp.status != 200:
                return None
            return resp.read(4096).decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _read_version(root: str) -> str:
    path = os.path.join(root, "VERSION")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip().split("\n")[0]
    except OSError:
        return ""


def discover(skills_dirs: list[str]) -> list[Toolkit]:
    """Find toolkits: a git repo sitting inside a skills directory."""
    out: list[Toolkit] = []
    for skills_dir in skills_dirs:
        if not os.path.isdir(skills_dir):
            continue
        try:
            entries = sorted(os.listdir(skills_dir))
        except OSError:
            continue
        for entry in entries:
            root = os.path.join(skills_dir, entry)
            if not os.path.isdir(os.path.join(root, ".git")):
                continue
            if not os.path.isfile(os.path.join(root, "SKILL.md")):
                continue
            remote = _git(root, "config", "--get", "remote.origin.url") or ""
            manages: list[str] = []
            try:
                for sub in sorted(os.listdir(root)):
                    if os.path.isfile(os.path.join(root, sub, "SKILL.md")):
                        manages.append(sub)
            except OSError:
                pass
            # The toolkit's own directory is part of what it installs, whether or
            # not it also ships nested skills. Gating this on `manages` meant a
            # checkout whose only skill is its root SKILL.md owned nothing, so
            # every symlink pointing at that root was classified LOCAL - and one
            # gstack release turned that into five blocking findings on a file
            # the user does not own.
            if manages or os.path.isfile(os.path.join(root, "SKILL.md")):
                manages.append(entry)
            out.append(
                Toolkit(
                    name=entry,
                    root=root,
                    install_dir=skills_dir,
                    remote=remote,
                    local_version=_read_version(root),
                    commit=_git(root, "rev-parse", "--short", "HEAD") or "",
                    manages=sorted(set(manages)),
                )
            )
    return out


def check_updates(toolkits: list[Toolkit], *, allow_network: bool = True) -> list[Toolkit]:
    """Annotate each toolkit with remote version / update availability."""
    for tk in toolkits:
        if not allow_network:
            tk.update_available = None
            tk.note = "network checks disabled"
            continue
        parsed = _parse_remote(tk.remote) if tk.remote else None
        if not parsed:
            tk.update_available = None
            tk.note = f"unrecognised remote: {tk.remote[:60] or '(none)'}"
            continue
        owner, repo = parsed

        remote_version = None
        if tk.local_version:
            for build in _RAW_URL_BUILDERS:
                for branch in _DEFAULT_BRANCHES:
                    remote_version = _fetch(build(owner, repo, branch))
                    if remote_version:
                        break
                if remote_version:
                    break

        if remote_version:
            tk.remote_version = remote_version
            lv, rv = version_key(tk.local_version), version_key(remote_version)
            tk.update_available = rv > lv
            tk.note = (
                f"local {tk.local_version} vs remote {remote_version}"
                if tk.update_available
                else f"up to date at {tk.local_version}"
            )
            continue

        # No VERSION file to compare: fall back to comparing commits.
        head = _git(tk.root, "ls-remote", tk.remote, "HEAD")
        if not head:
            tk.update_available = None
            tk.note = "could not reach remote"
            continue
        remote_sha = head.split()[0]
        local_sha = _git(tk.root, "rev-parse", "HEAD") or ""
        if local_sha:
            tk.update_available = not remote_sha.startswith(local_sha[:7])
            tk.note = f"local {local_sha[:8]} vs remote {remote_sha[:8]}"
        else:
            tk.update_available = None
            tk.note = "no local commit"
    return toolkits


def managed_paths(toolkits: list[Toolkit]) -> set[str]:
    """Absolute SKILL.md paths owned by any discovered toolkit.

    Resolved through symlinks as well as by name: a toolkit installs its skills
    as links from the skills directory into its own checkout, and a link whose
    directory name does not match any managed entry - gstack's
    `_gstack-command` -> `gstack/SKILL.md` - would otherwise read as content the
    user wrote.
    """
    out: set[str] = set()
    roots: list[str] = []
    for tk in toolkits:
        for name in tk.manages:
            out.add(os.path.join(tk.install_dir, name, "SKILL.md"))
        if tk.root:
            roots.append(os.path.realpath(tk.root))
        out.add(os.path.join(tk.root, "SKILL.md"))

    for tk in toolkits:
        try:
            entries = os.listdir(tk.install_dir)
        except OSError:
            continue
        for entry in entries:
            candidate = os.path.join(tk.install_dir, entry, "SKILL.md")
            if not os.path.islink(candidate) and not os.path.isfile(candidate):
                continue
            target = os.path.realpath(candidate)
            if any(target == os.path.join(r, "SKILL.md") or target.startswith(r + os.sep) for r in roots):
                out.add(candidate)
    return out
