"""Checks for subagent definitions.

Subagents were the one configured thing this tool never looked at. That gap was
not academic: the setup it was built against had eleven definition files, 3,500
lines, untouched for six months, none of which Claude Code could load - and
nothing reported it, because no rule existed to look.

The requirements come from the official subagent documentation:

* files live under ``.claude/agents/`` or ``~/.claude/agents/``, scanned
  **recursively** - a subfolder is fine and does not change identity;
* ``name`` and ``description`` are the only required frontmatter fields;
* ``name`` is a unique lowercase-hyphen identifier and may not contain ``:``,
  which is reserved for plugin-scoped identifiers;
* ``description`` states *when Claude should delegate* to the subagent.

Identity comes only from ``name``. A file without frontmatter therefore has no
identity at all, which is why that case is critical rather than cosmetic.
"""

from __future__ import annotations

import collections
import os
import re

from ..model import Inventory, Severity
from . import SPEC_AGENTS, Config, LazyRegistry, make, rule

REG = LazyRegistry()

_NAME_RE = re.compile(r"^[a-z0-9-]+$")
#: Signals a description says *when to delegate*, not just what the agent knows.
_WHEN_RE = re.compile(
    r"(\buse\b[^.;]{0,60}\b(when|for|after|before)\b"
    r"|\bwhen\s+(the user|you|a |an |the task|working|asked|reviewing|writing)"
    r"|\bfor (any|every|all)\b"
    r"|\b(proactively|delegate)\b"
    r"|使用時機|何時使用|當.{0,40}(時|使用)|適用於|用於)",
    re.I,
)


#: A path-shaped token in prose: a run of path characters ending in ``.md``.
#: Bounded so that ``security.md.bak`` does not read as a reference to
#: ``security.md`` - a substring test called an orphaned backup a live reader and
#: downgraded the finding that would have got it cleaned up.
_PATH_TOKEN_RE = re.compile(r"[~\w./\\-]*\.md\b(?!\.)")


def _reference_index(inv: Inventory) -> dict[str, list[str]]:
    """Map every resolved ``.md`` path mentioned anywhere to the files mentioning it.

    Built once per run. Asking the question per agent instead re-read the whole
    corpus each time - 1,135 files x 11 agents, 66 seconds - for an answer that
    does not change between agents.

    References are resolved rather than compared as text, because the same file
    is written three ways: ``~/.claude/agents/x.md``, the expanded absolute path,
    and ``../agents/x.md`` relative to the referring file. Matching only the
    literal spelling reports the other two as unreferenced.
    """
    from .. import safeio

    index: dict[str, list[str]] = {}
    sources = (
        list(inv.commands)
        + list(inv.workflows)
        + list(inv.instructions)
        + list(inv.skills)
        + list(inv.agents)
    )
    for src in sources:
        text = safeio.read_text(src.path) or ""
        if ".md" not in text:
            continue
        base = os.path.dirname(src.path)
        here = os.path.realpath(src.path)
        for token in set(_PATH_TOKEN_RE.findall(text)):
            expanded = os.path.expanduser(token)
            candidate = expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
            resolved = os.path.realpath(candidate)
            if resolved == here:
                continue  # a file does not reference itself
            readers = index.setdefault(resolved, [])
            if src.path not in readers:
                readers.append(src.path)
    return index


@rule(
    "AG001",
    "Subagent file has no frontmatter, so it never loads",
    Severity.CRITICAL,
    SPEC_AGENTS,
    "agents",
)
def ag001(inv: Inventory, cfg: Config):
    """A subagent's identity comes only from its ``name`` frontmatter field.

    Without a frontmatter block there is no name, so Claude Code never registers
    the file and nothing can *delegate* to it. Whether anything *reads* it is a
    separate question, and the finding says which, because the two call for
    opposite actions.
    """
    index = _reference_index(inv)
    for a in inv.agents:
        if a.frontmatter_present:
            continue
        readers = index.get(os.path.realpath(a.path), [])
        if readers:
            names = ", ".join(sorted({os.path.basename(r) for r in readers}))
            detail = (
                f"{os.path.basename(a.path)} has no YAML frontmatter, so Claude Code "
                f"never registers it as a subagent and nothing can delegate to it. "
                f"It is not unused, though: {names} reference it by path and read it "
                "as prompt content. Deleting it would break them."
            )
            remedy = (
                "Add `name` and `description` frontmatter so it can also be delegated "
                f"to, or leave it as a prompt file that {names} read. Do not delete it."
            )
        else:
            detail = (
                f"{os.path.basename(a.path)} has no YAML frontmatter, so it declares no "
                f"`name` and Claude Code never registers it. Its {a.lines} lines are "
                "unreachable, and nothing else references the path either."
            )
            remedy = (
                "Add a frontmatter block with `name` (lowercase-hyphen) and "
                "`description` (when to delegate), or remove the file."
            )
        yield make(
            REG["AG001"],
            detail,
            path=a.path,
            line=1,
            evidence={
                "lines": a.lines,
                "filename_stem": a.name,
                "referenced_by": sorted(readers),
            },
            remedy=remedy,
            severity=Severity.IMPORTANT if readers else Severity.CRITICAL,
        )


@rule(
    "AG002",
    "Subagent is missing a required frontmatter field",
    Severity.CRITICAL,
    SPEC_AGENTS,
    "agents",
)
def ag002(inv: Inventory, cfg: Config):
    """``name`` and ``description`` are the only required fields, and both matter:
    without ``name`` there is no identity, without ``description`` Claude has no
    basis on which to delegate."""
    for a in inv.agents:
        if not a.frontmatter_present:
            continue  # AG001 owns that case
        missing = [
            field
            for field, value in (("name", a.declared_name), ("description", a.description))
            if not value
        ]
        if not missing:
            continue
        yield make(
            REG["AG002"],
            f"{os.path.basename(a.path)} has frontmatter but no "
            + " or ".join(f"`{m}`" for m in missing)
            + ". Both are required; identity comes from `name` and delegation from "
            "`description`.",
            path=a.path,
            line=1,
            evidence={"missing": missing},
            remedy="Add the missing field(s) to the frontmatter block.",
        )


@rule(
    "AG003",
    "Subagent name is not a valid identifier",
    Severity.CRITICAL,
    SPEC_AGENTS,
    "agents",
)
def ag003(inv: Inventory, cfg: Config):
    """A name containing ``:`` is not merely discouraged - the documentation says
    Claude Code refuses to load the file and logs an error, because ``:`` is
    reserved for plugin-scoped identifiers."""
    for a in inv.agents:
        if not a.frontmatter_present or not a.declared_name:
            continue
        name = a.declared_name
        if ":" in name:
            yield make(
                REG["AG003"],
                f"name {name!r} contains ':', which is reserved for plugin-scoped "
                "identifiers. Claude Code does not load the file and logs an error.",
                path=a.path,
                evidence={"name": name, "reason": "colon"},
                remedy="Rename to a lowercase-hyphen identifier without ':'.",
            )
        elif not _NAME_RE.match(name):
            yield make(
                REG["AG003"],
                f"name {name!r} is not a lowercase-hyphen identifier.",
                path=a.path,
                evidence={"name": name, "reason": "format"},
                remedy="Use only lowercase letters, digits and hyphens.",
            )


@rule(
    "AG004",
    "Two subagents declare the same name",
    Severity.IMPORTANT,
    SPEC_AGENTS,
    "agents",
)
def ag004(inv: Inventory, cfg: Config):
    """Only one of them loads, and which one is decided by filesystem read order
    rather than any documented precedence - so the winner can change without
    anything being edited."""
    by_name: dict[tuple[str, str], list] = collections.defaultdict(list)
    for a in inv.agents:
        if a.frontmatter_present and a.declared_name:
            by_name[(a.runtime.value, a.declared_name)].append(a)
    for (runtime, name), group in sorted(by_name.items()):
        if len(group) < 2:
            continue
        yield make(
            REG["AG004"],
            f"{len(group)} files in the {runtime} runtime declare name {name!r}. Only "
            "one loads, chosen by filesystem read order, so which one is active can "
            "change without any edit.",
            path=sorted(a.path for a in group)[0],
            evidence={"name": name, "paths": sorted(a.path for a in group)},
            remedy="Rename all but one, or delete the duplicates.",
        )


@rule(
    "AG005",
    "Subagent description does not say when to delegate",
    Severity.IMPORTANT,
    SPEC_AGENTS,
    "agents",
)
def ag005(inv: Inventory, cfg: Config):
    """The description is the whole basis for delegation.

    One that only states expertise - "Python backend expert" - gives Claude
    nothing to match a task against, so the subagent exists but is never chosen.
    """
    for a in inv.agents:
        if not a.frontmatter_present or not a.description:
            continue  # AG001/AG002 own those
        if _WHEN_RE.search(a.description):
            continue
        yield make(
            REG["AG005"],
            f"description of {a.declared_name!r} states what it is but never when to "
            f"delegate to it ({a.description[:90]!r}). Claude selects a subagent by "
            "matching the task against this text, so without a trigger it is "
            "effectively never chosen.",
            path=a.path,
            evidence={"name": a.declared_name, "description": a.description},
            remedy="Add a clause naming concrete situations, e.g. 'Use after writing or modifying code.'",
        )
