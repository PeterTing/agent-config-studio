"""Detect whether cloud-installed plugins have updates available.

Read-only network access only: ``git ls-remote`` reads the remote ref and writes
nothing, locally or remotely. A plugin whose remote cannot be reached is reported
as unknown rather than up to date, so a network failure never looks like a clean
bill of health.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from .versions import version_key

LS_REMOTE_TIMEOUT = 20


@dataclass
class UpdateReport:
    #: Externally-managed skill toolkits (see studio.toolkits), annotated in place.
    toolkits: list[dict] = field(default_factory=list)
    checked: int = 0
    updates: list[dict] = field(default_factory=list)
    up_to_date: list[dict] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    offline: bool = False

    def summary(self) -> dict:
        return {
            "checked": self.checked,
            "updates_available": len(self.updates),
            "up_to_date": len(self.up_to_date),
            "unknown": len(self.unknown),
            "offline": self.offline,
            "toolkits_checked": len(self.toolkits),
            "toolkit_updates_available": sum(
                1 for t in self.toolkits if t.get("update_available")
            ),
        }


def _ls_remote(url: str) -> tuple[str | None, str | None]:
    """Return (sha, error). Never raises."""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=LS_REMOTE_TIMEOUT,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip().splitlines()[-1:][0] if (
            proc.stderr or proc.stdout
        ).strip() else f"git exited {proc.returncode}"
    out = proc.stdout.strip()
    if not out:
        return None, "empty ls-remote output"
    return out.split()[0], None


def _remote_manifest(repo: str) -> dict | None:
    """Fetch a marketplace's manifest from GitHub, or None when unreachable."""
    import json as _json
    import urllib.error
    import urllib.request

    for branch in ("HEAD", "main", "master"):
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/.claude-plugin/marketplace.json"
        try:
            with urllib.request.urlopen(url, timeout=LS_REMOTE_TIMEOUT) as resp:  # noqa: S310
                if resp.status != 200:
                    continue
                return _json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def _local_marketplace_state(path: str) -> tuple[str, str]:
    """Fingerprint a locally-sourced marketplace by its newest file mtime."""
    newest = 0.0
    count = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "__pycache__"}]
        for f in filenames:
            try:
                m = os.stat(os.path.join(dirpath, f)).st_mtime
            except OSError:
                continue
            newest = max(newest, m)
            count += 1
    return (f"{newest:.0f}", f"{count} files")


def check(inventory, *, allow_network: bool = True) -> UpdateReport:
    """Annotate ``inventory.plugins`` in place and return a summary report.

    Covers both update channels: marketplace plugins, and skill toolkits that are
    git checkouts inside a skills directory (gstack). A toolkit can be many
    versions behind while every plugin is current, so reporting only plugins
    would give a misleadingly clean answer.
    """
    report = UpdateReport()

    from . import toolkits as toolkits_mod

    kits = toolkits_mod.discover(
        [
            os.path.expanduser("~/.claude/skills"),
            os.path.expanduser("~/.codex/skills"),
        ]
    )
    toolkits_mod.check_updates(kits, allow_network=allow_network)
    report.toolkits = [k.to_dict() for k in kits]
    inventory.toolkits = report.toolkits
    seen: dict[str, tuple[str | None, str | None]] = {}

    manifests: dict[str, dict | None] = {}

    for p in inventory.plugins:
        if not p.enabled:
            continue
        if not p.marketplace_repo:
            p.update_available = None
            p.update_note = f"marketplace {p.marketplace!r} is not a GitHub source"
            report.unknown.append({"plugin": p.key, "reason": p.update_note})
            continue
        if not allow_network:
            p.update_available = None
            p.update_note = "network checks disabled"
            report.offline = True
            report.unknown.append({"plugin": p.key, "reason": p.update_note})
            continue

        if p.marketplace_repo not in manifests:
            manifests[p.marketplace_repo] = _remote_manifest(p.marketplace_repo)
        manifest = manifests[p.marketplace_repo]
        report.checked += 1
        if manifest is None:
            p.update_available = None
            p.update_note = f"could not fetch the manifest for {p.marketplace_repo}"
            report.unknown.append({"plugin": p.key, "reason": p.update_note})
            report.errors.append(f"{p.key}: manifest unreachable")
            continue

        name = p.key.split("@")[0]
        entry = next(
            (e for e in (manifest.get("plugins") or []) if isinstance(e, dict) and e.get("name") == name),
            None,
        )
        if entry is None:
            p.update_available = None
            p.update_note = "no longer listed in the remote marketplace manifest"
            report.unknown.append({"plugin": p.key, "reason": p.update_note})
            continue

        remote_version = str(entry.get("version") or "")
        remote_sha = ""
        src = entry.get("source")
        if isinstance(src, dict):
            remote_sha = str(src.get("sha") or "")

        if remote_version and p.version:
            p.remote_revision = remote_version
            newer = version_key(remote_version) > version_key(p.version)
            p.update_available = newer
            p.update_note = (
                f"local {p.version} -> remote {remote_version}" if newer else f"up to date at {p.version}"
            )
        elif remote_sha and p.commit:
            p.remote_revision = remote_sha
            width = min(len(remote_sha), len(p.commit))
            same = width >= 7 and remote_sha[:width] == p.commit[:width]
            p.update_available = not same
            p.update_note = (
                f"local {p.commit[:12]} -> remote {remote_sha[:12]}" if not same
                else f"up to date at {p.commit[:12]}"
            )
        else:
            p.update_available = None
            p.update_note = "remote manifest records no comparable version or sha"
            report.unknown.append({"plugin": p.key, "reason": p.update_note})
            continue

        target = report.updates if p.update_available else report.up_to_date
        target.append(
            {
                "plugin": p.key,
                "marketplace": p.marketplace,
                "runtime": p.runtime.value,
                "local": p.version or p.commit,
                "remote": p.remote_revision,
                "source": p.source,
            }
        )

    return report
