"""Managed-mirror and canonical-drift checks.

The dual-harness setup needs the same rules available to Claude Code and Codex.
Maintaining two copies by hand guarantees drift; the supported alternative is one
canonical source rendered into both, with a checker that fails when a rendered
file no longer matches what the canonical source would produce.

Two relationships are tracked:

``mirrors``
    Groups of paths that must stay byte-identical (a skill shared verbatim
    across runtimes, plus any company-canonical copy).

``generated``
    A target file produced from canonical sources. Drift means someone edited the
    generated file directly instead of the source.
"""

from __future__ import annotations

import hashlib
import os

from .. import safeio
from ..model import Inventory, Severity
from . import SPEC_CTX5, SPEC_MEMORY, Config, LazyRegistry, make, rule

REG = LazyRegistry()


def _digest_file(path: str) -> str | None:
    """Digest a mirror path, or None when it cannot be read.

    Mirror groups may point outside the agent-config directories - a company
    canonical copy under ~/Documents, for example - and a scheduled run has no
    access there. safeio turns that into None instead of a hung process.
    """
    data = safeio.read_bytes(path)
    if data is None:
        return None
    return hashlib.md5(data).hexdigest()


@rule(
    "MR001",
    "Declared mirror group has drifted",
    Severity.CRITICAL,
    SPEC_CTX5,
    "mirrors",
)
def mr001(inv: Inventory, cfg: Config):
    for group in cfg.mirrors:
        label = group.get("name", "unnamed")
        paths = [os.path.expanduser(p) for p in group.get("paths", [])]
        digests: dict[str, str | None] = {p: _digest_file(p) for p in paths}
        present = {p: d for p, d in digests.items() if d is not None}
        if len(present) < 2:
            continue
        if len(set(present.values())) == 1:
            continue
        yield make(
            REG["MR001"],
            f"mirror group {label!r} is declared byte-identical but the copies differ: "
            + ", ".join(f"{os.path.basename(os.path.dirname(p))}={d[:8]}" for p, d in present.items())
            + ".",
            path=paths[0],
            evidence={"group": label, "digests": {p: d for p, d in present.items()}},
            remedy="Re-sync every declared mirror and generated file: `studio sync --apply`.",
        )


@rule(
    "MR002",
    "Declared mirror path is missing",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "mirrors",
)
def mr002(inv: Inventory, cfg: Config):
    for group in cfg.mirrors:
        label = group.get("name", "unnamed")
        for p in group.get("paths", []):
            full = os.path.expanduser(p)
            present = safeio.exists(full)
            if present:
                continue
            if present is None:
                yield make(
                    REG["MR002"],
                    f"mirror group {label!r} declares {p}, which this process cannot "
                    "read. On macOS a scheduled agent has no access to ~/Documents, so "
                    "the group is reported as unverified rather than assumed in sync.",
                    path=full,
                    evidence={"group": label, "path": p, "reason": "unreadable in this context"},
                    remedy="Verify it from an interactive run, or move the canonical copy "
                    "outside a TCC-protected directory.",
                    severity=Severity.MINOR,
                )
                continue
            yield make(
                REG["MR002"],
                f"mirror group {label!r} declares {p}, which does not exist. The group "
                "cannot be verified as synchronised.",
                path=full,
                evidence={"group": label, "missing": p},
                remedy="Create the mirror, or remove the path from canonical/governance.json.",
            )


@rule(
    "MR003",
    "Generated instruction file has drifted from its canonical source",
    Severity.CRITICAL,
    SPEC_MEMORY,
    "mirrors",
)
def mr003(inv: Inventory, cfg: Config):
    from ..canonical import render_target  # local import: avoids a cycle at import time

    for spec in cfg.generated:
        target = os.path.expanduser(spec.get("target", ""))
        if not target:
            continue
        if not os.path.exists(target):
            yield make(
                REG["MR003"],
                f"declared generated target {spec.get('target')} does not exist.",
                path=target,
                evidence={"spec": spec},
                remedy="Run `studio sync` to produce it.",
            )
            continue
        try:
            expected = render_target(cfg, spec)
        except FileNotFoundError as exc:
            yield make(
                REG["MR003"],
                f"cannot render {spec.get('target')}: {exc}",
                path=target,
                evidence={"spec": spec},
                remedy="Restore the missing canonical source file.",
            )
            continue
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                actual = fh.read()
        except OSError as exc:
            yield make(
                REG["MR003"],
                f"cannot read {target}: {exc}",
                path=target,
                evidence={"spec": spec},
            )
            continue
        if actual == expected:
            continue
        exp_lines = expected.split("\n")
        act_lines = actual.split("\n")
        first_diff = next(
            (
                i + 1
                for i in range(max(len(exp_lines), len(act_lines)))
                if (exp_lines[i] if i < len(exp_lines) else None)
                != (act_lines[i] if i < len(act_lines) else None)
            ),
            None,
        )
        yield make(
            REG["MR003"],
            f"{os.path.basename(target)} no longer matches what canonical/ renders "
            f"(first difference at line {first_diff}; {len(act_lines)} lines on disk vs "
            f"{len(exp_lines)} rendered). Someone edited the generated file instead of "
            "the source, so the two runtimes are diverging.",
            path=target,
            line=first_diff,
            evidence={
                "target": target,
                "sources": spec.get("sources", []),
                "on_disk_lines": len(act_lines),
                "rendered_lines": len(exp_lines),
                "first_diff_line": first_diff,
            },
            remedy="Move the edit into the canonical source, then run `studio sync`.",
        )


@rule(
    "MR004",
    "Governance declarations could not be read",
    Severity.CRITICAL,
    SPEC_MEMORY,
    "mirrors",
)
def mr004(inv: Inventory, cfg: Config):
    """Report an unreadable governance.json instead of proceeding without it.

    A malformed file used to be swallowed and treated as "no declarations". Every
    mirror, generated-file, vendored and waiver check then had nothing to check,
    so they all passed, and the run reported PASS while verifying none of what
    the user had declared. A missing trailing comma could turn the whole audit
    into a green light.
    """
    if not cfg.governance_error:
        return
    yield make(
        REG["MR004"],
        "canonical/governance.json exists but could not be parsed "
        f"({cfg.governance_error}). Every mirror, generated-file, vendored-path "
        "and waiver declaration in it was skipped, so those checks did not run "
        "at all - a verdict from this run does not cover them.",
        path=os.path.join(cfg.repo_root, "canonical", "governance.json"),
        evidence={"error": cfg.governance_error},
        remedy="Fix the JSON syntax and re-run. Until then treat the verdict as incomplete.",
    )


@rule(
    "MR005",
    "A file the audit depends on could not be read",
    Severity.CRITICAL,
    SPEC_MEMORY,
    "mirrors",
)
def mr005(inv: Inventory, cfg: Config):
    """A partial scan must not be able to report PASS.

    The scanner records unreadable paths in `inv.scan_errors` and moves on, which
    keeps one bad file from taking the run down. But the verdict was computed
    only from findings, so a file that was never examined produced no findings
    and therefore looked clean. Absence of evidence was being reported as
    evidence of compliance.
    """
    for err in inv.scan_errors:
        path = str(err).split(":", 1)[0]
        yield make(
            REG["MR005"],
            f"{err}. This path was skipped, so nothing in it was checked and the "
            "verdict does not cover it.",
            path=path,
            evidence={"error": str(err)},
            remedy="Fix the permissions or the file, then re-run so it is actually audited.",
        )
