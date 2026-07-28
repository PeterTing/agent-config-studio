"""Change sets: propose as a diff, apply only on request, always with a backup.

The scanner and health checker never write. Every mutation goes through here, and
every mutation is preceded by a timestamped backup of the exact bytes replaced,
so any apply can be undone with ``studio rollback``.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

PATCH_DIR = "var/patches"
BACKUP_DIR = "var/backups"


@dataclass
class Change:
    path: str
    new_text: str
    reason: str = ""
    #: "modify" | "create" | "delete"
    action: str = "modify"

    def old_text(self) -> str:
        if not os.path.isfile(self.path):
            return ""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def is_noop(self) -> bool:
        if self.action == "delete":
            return not os.path.exists(self.path)
        return self.old_text() == self.new_text

    def diff(self) -> str:
        old = self.old_text().split("\n")
        new = [] if self.action == "delete" else self.new_text.split("\n")
        label = self.path
        return "\n".join(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a{label}",
                tofile=f"b{label}",
                lineterm="",
                n=3,
            )
        )

    def stat(self) -> dict:
        old = self.old_text().split("\n")
        new = [] if self.action == "delete" else self.new_text.split("\n")
        sm = difflib.SequenceMatcher(a=old, b=new)
        added = removed = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
        return {
            "path": self.path,
            "action": self.action,
            "old_lines": len(old) if self.old_text() else 0,
            "new_lines": len(new),
            "added": added,
            "removed": removed,
            "reason": self.reason,
        }


@dataclass
class ChangeSet:
    name: str
    changes: list[Change] = field(default_factory=list)
    created_at: str = ""
    description: str = ""
    #: Directories to remove. Only ever empty ones, so there is nothing to back
    #: up beyond the fact that they existed; rollback recreates them.
    remove_dirs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def effective(self) -> list[Change]:
        return [c for c in self.changes if not c.is_noop()]

    def has_work(self) -> bool:
        return bool(self.effective() or [d for d in self.remove_dirs if os.path.isdir(d)])

    def diff(self) -> str:
        return "\n".join(c.diff() for c in self.effective())

    def manifest(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "description": self.description,
            "changes": [c.stat() for c in self.effective()],
        }


def save(cs: ChangeSet, repo_root: str) -> tuple[str, str]:
    """Write the diff and manifest for review. Returns (diff path, manifest path)."""
    out = os.path.join(repo_root, PATCH_DIR)
    os.makedirs(out, exist_ok=True)
    stamp = cs.created_at.replace(":", "").replace("-", "")
    base = os.path.join(out, f"{stamp}-{cs.name}")
    diff_path, man_path, payload_path = f"{base}.diff", f"{base}.json", f"{base}.payload.json"

    with open(diff_path, "w", encoding="utf-8") as fh:
        fh.write(cs.diff() + "\n")
    with open(man_path, "w", encoding="utf-8") as fh:
        json.dump(cs.manifest(), fh, indent=2, ensure_ascii=False)
    # Full new content, so `studio apply` reproduces exactly what was reviewed
    # instead of re-deriving it from a possibly-changed source.
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "name": cs.name,
                "created_at": cs.created_at,
                "description": cs.description,
                "changes": [asdict(c) for c in cs.effective()],
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )
    return diff_path, man_path


def load(payload_path: str) -> ChangeSet:
    with open(payload_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return ChangeSet(
        name=data["name"],
        created_at=data.get("created_at", ""),
        description=data.get("description", ""),
        changes=[Change(**c) for c in data.get("changes", [])],
    )


def _write(path: str, text: str) -> None:
    """Write `text` to `path`, preserving what the path already is.

    Two properties the plain atomic-replace lost:

    * **Symlinks.** ``os.replace`` swaps the *link* for a regular file, which
      severs the connection to whatever manages it - a toolkit that installs its
      skills as symlinks, or a dotfile manager. The backup stores dereferenced
      content, so rollback could not put the link back either. Writing through
      the link keeps it intact; atomicity is given up only in that case, which is
      the right trade against permanently disconnecting managed configuration.
    * **Permissions.** A fresh temporary file is created with the process umask,
      so replacing a 0600 file published it and replacing an executable made it
      non-executable. The original mode is copied onto the replacement first.
    """
    if os.path.islink(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return

    mode = None
    if os.path.exists(path):
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = None

    tmp = path + ".studio-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def _backup_slot(repo_root: str, name: str) -> str:
    """Claim a fresh backup directory, never an existing one.

    Second-precision timestamps collide: two fixes applied from the dashboard
    within the same second produced the same directory, and reusing it let the
    second operation overwrite the first one's saved bytes. The backup would
    still be listed, and restoring it would put back the wrong content - worse
    than having no backup, because it looks like one.
    """
    base = os.path.join(repo_root, BACKUP_DIR)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    slot = os.path.join(base, f"{stamp}-{name}")
    suffix = 0
    while True:
        try:
            os.makedirs(slot)  # exclusive: fails if it already exists
            return slot
        except FileExistsError:
            suffix += 1
            slot = os.path.join(base, f"{stamp}-{name}-{suffix}")


def _mirror_path(slot: str, path: str) -> str:
    rel = path.lstrip("/")
    dest = os.path.join(slot, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest


def apply(cs: ChangeSet, repo_root: str, *, dry_run: bool = False) -> dict:
    """Apply a change set. Backs up every touched path first."""
    effective = cs.effective()
    dirs = [d for d in cs.remove_dirs if os.path.isdir(d)]
    if not effective and not dirs:
        return {"applied": 0, "backup": None, "skipped": len(cs.changes), "changes": []}

    if dry_run:
        return {
            "applied": 0,
            "dry_run": True,
            "backup": None,
            "changes": [c.stat() for c in effective],
            "removed_dirs": dirs,
        }

    slot = _backup_slot(repo_root, cs.name)
    record: list[dict] = []
    removed: list[str] = []
    failure: Exception | None = None

    def write_manifest() -> None:
        """Record what was actually done, whether or not everything succeeded.

        Written in a finally block because the failure case is the one that
        matters: an apply that dies partway has already modified files, and a
        backup directory with no manifest is skipped by `list_backups`. The
        result was a partially-changed configuration with no visible way back -
        the exact failure the backup mechanism exists to prevent.
        """
        with open(os.path.join(slot, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "change_set": cs.name,
                    "description": cs.description,
                    "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "changes": record,
                    "removed_dirs": removed,
                    "complete": failure is None,
                    "failed_with": None if failure is None else f"{type(failure).__name__}: {failure}",
                    "planned": len(effective),
                },
                fh,
                indent=2,
                ensure_ascii=False,
            )

    backed_up: set[str] = set()
    try:
        for c in effective:
            existed = os.path.isfile(c.path)
            # Captured before the operation: a delete removes the link, after
            # which islink() is always false and the manifest would record a
            # plain file - so rollback recreated a regular file and the toolkit
            # or dotfile manager that owned the link lost it for good.
            was_link = os.path.islink(c.path)
            link_target = os.readlink(c.path) if was_link else ""
            # Back a path up once only. Two changes can target the same file -
            # overlapping mirror and generated declarations do exactly that - and
            # copying again after the first write saved the *intermediate*
            # content over the original, leaving nothing to roll back to.
            if existed and c.path not in backed_up:
                shutil.copy2(c.path, _mirror_path(slot, c.path))
                backed_up.add(c.path)
            if c.action == "delete":
                if existed:
                    os.remove(c.path)
            else:
                os.makedirs(os.path.dirname(c.path), exist_ok=True)
                _write(c.path, c.new_text)
            record.append(
                {
                    **c.stat(),
                    "existed_before": existed,
                    "was_symlink": was_link,
                    "symlink_target": link_target,
                }
            )

        for d in dirs:
            try:
                os.rmdir(d)  # only ever empty; a non-empty one raises and is skipped
                removed.append(d)
            except OSError:
                continue
    except Exception as exc:  # noqa: BLE001 - re-raised after the manifest lands
        failure = exc
        raise
    finally:
        write_manifest()
    return {
        "applied": len(record) + len(removed),
        "backup": slot,
        "changes": record,
        "removed_dirs": removed,
    }


def list_backups(repo_root: str) -> list[dict]:
    root = os.path.join(repo_root, BACKUP_DIR)
    if not os.path.isdir(root):
        return []
    out: list[dict] = []
    for entry in sorted(os.listdir(root), reverse=True):
        man = os.path.join(root, entry, "manifest.json")
        if not os.path.isfile(man):
            continue
        try:
            with open(man, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": entry, "path": os.path.join(root, entry), **data})
    return out


def rollback(repo_root: str, backup_id: str) -> dict:
    """Restore every file captured in a backup slot to its pre-apply bytes."""
    slot = os.path.join(repo_root, BACKUP_DIR, backup_id)
    man = os.path.join(slot, "manifest.json")
    if not os.path.isfile(man):
        raise FileNotFoundError(f"no backup manifest at {man}")
    with open(man, encoding="utf-8") as fh:
        data = json.load(fh)

    restored: list[str] = []
    removed: list[str] = []
    recreated: list[str] = []
    for d in data.get("removed_dirs", []):
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            recreated.append(d)
    for c in data.get("changes", []):
        path = c["path"]
        saved = os.path.join(slot, path.lstrip("/"))
        if c.get("was_symlink"):
            # Two different states hide behind one flag. A *deleted* link needs
            # recreating; a *modified* one still exists, and `_write` changed the
            # bytes of whatever it points at - so recreating the link alone left
            # the modified content in place while reporting it restored.
            if not os.path.lexists(path) and c.get("symlink_target"):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.symlink(c["symlink_target"], path)
            if os.path.isfile(saved):
                # Follows the link, putting the target's original bytes back.
                shutil.copy2(saved, path)
            restored.append(path)
            continue
        if os.path.isfile(saved):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(saved, path)
            restored.append(path)
        elif not c.get("existed_before", True) and os.path.isfile(path):
            # The apply created this file; rolling back means removing it again.
            os.remove(path)
            removed.append(path)
    return {"backup": backup_id, "restored": restored, "removed": removed, "recreated_dirs": recreated}
