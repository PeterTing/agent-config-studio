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


@rule(
    "AG001",
    "Subagent file has no frontmatter, so it never loads",
    Severity.CRITICAL,
    SPEC_AGENTS,
    "agents",
)
def ag001(inv: Inventory, cfg: Config):
    """A subagent's identity comes only from its ``name`` frontmatter field.

    Without a frontmatter block there is no name, so Claude Code cannot register
    or invoke the file no matter what the body says. The file looks like working
    configuration in a directory listing, which is what makes this worth
    reporting loudly: someone routes work to a reviewer that does not exist.
    """
    for a in inv.agents:
        if a.frontmatter_present:
            continue
        yield make(
            REG["AG001"],
            f"{os.path.basename(a.path)} has no YAML frontmatter, so it declares no "
            f"`name` and Claude Code never registers it. Its {a.lines} lines are "
            "unreachable; nothing can delegate to it.",
            path=a.path,
            line=1,
            evidence={"lines": a.lines, "filename_stem": a.name},
            remedy=(
                "Add a frontmatter block with `name` (lowercase-hyphen) and "
                "`description` (when to delegate), or delete the file if it is no "
                "longer wanted."
            ),
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
