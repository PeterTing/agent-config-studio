"""Data model for the local agent-config inventory and health findings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum


class Runtime(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    SHARED = "shared"
    UNKNOWN = "unknown"


class Origin(str, Enum):
    #: Hand-authored under ~/.claude/skills or ~/.codex/skills.
    LOCAL = "local"
    #: Installed by a plugin marketplace (updatable from the cloud).
    PLUGIN = "plugin"
    #: Installed into a local skills directory by an external toolkit such as
    #: gstack. Lives in a "local" path but is overwritten on toolkit upgrade, so
    #: editing it in place does not persist.
    TOOLKIT = "toolkit"
    #: The unreferenced ~/.agent/skills library.
    ORPHAN_LIBRARY = "orphan-library"


class Severity(str, Enum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    MINOR = "minor"

    @property
    def rank(self) -> int:
        return {"critical": 0, "important": 1, "minor": 2}[self.value]


@dataclass
class Skill:
    id: str
    name: str
    dir_name: str
    path: str
    runtime: Runtime
    origin: Origin
    description: str = ""
    body_lines: int = 0
    body_bytes: int = 0
    content_hash: str = ""
    #: Relative paths referenced from SKILL.md (progressive-disclosure targets).
    refs: list[str] = field(default_factory=list)
    #: Skills/commands this skill tells the agent to invoke.
    invokes: list[str] = field(default_factory=list)
    plugin: str | None = None
    frontmatter_present: bool = True
    frontmatter_keys: list[str] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class Instruction:
    """A CLAUDE.md / AGENTS.md style always-loaded instruction file."""

    id: str
    path: str
    runtime: Runtime
    lines: int
    bytes: int
    #: Top-level markdown headings, used for duplicate-section detection.
    sections: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    invokes: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    id: str
    path: str
    runtime: Runtime
    lines: int
    #: First heading or frontmatter description - what this workflow is for.
    description: str = ""
    refs: list[str] = field(default_factory=list)
    invokes: list[str] = field(default_factory=list)


@dataclass
class Command:
    id: str
    name: str
    path: str
    runtime: Runtime
    lines: int
    #: What the command does, from frontmatter or the first heading.
    description: str = ""
    invokes: list[str] = field(default_factory=list)


@dataclass
class AgentDef:
    id: str
    name: str
    path: str
    runtime: Runtime
    lines: int
    description: str = ""
    #: Whether the file has a closed YAML frontmatter block. Without one there is
    #: no `name`, and identity comes only from `name` - so the file never loads.
    frontmatter_present: bool = True
    #: The name as declared in frontmatter, empty when absent. `name` above falls
    #: back to the filename for display, which would otherwise hide the problem.
    declared_name: str = ""


@dataclass
class Hook:
    id: str
    event: str
    matcher: str | None
    index: int
    type: str
    command: str = ""
    if_rule: str | None = None
    status_message: str | None = None
    source: str = ""
    #: Text this hook injects into context, when statically extractable.
    injects: str = ""


@dataclass
class Plugin:
    id: str
    key: str
    marketplace: str
    runtime: Runtime
    enabled: bool
    source_type: str = ""
    source: str = ""
    last_revision: str = ""
    #: Installed version string and commit, from installed_plugins.json.
    version: str = ""
    commit: str = ""
    install_path: str = ""
    #: "owner/name" of the marketplace repository, when it is a GitHub source.
    marketplace_repo: str = ""
    skill_count: int = 0
    #: Filled in by updates.py.
    remote_revision: str = ""
    update_available: bool | None = None
    update_note: str = ""


class Owner(str, Enum):
    #: Hand-authored config the user can and should fix.
    LOCAL = "local"
    #: Shipped by a plugin/marketplace; editing it gets overwritten on upgrade.
    VENDOR = "vendor"


@dataclass
class Finding:
    rule: str
    severity: Severity
    title: str
    detail: str
    path: str = ""
    line: int | None = None
    #: Machine-readable evidence so the report can be re-verified.
    evidence: dict[str, object] = field(default_factory=dict)
    remedy: str = ""
    #: Official documentation URL this rule is derived from.
    spec: str = ""
    owner: Owner = Owner.LOCAL
    #: Set when a waiver in canonical/waivers.json matches this finding.
    waived: bool = False
    waiver_reason: str = ""

    @property
    def location(self) -> str:
        if self.path and self.line:
            return f"{self.path}:{self.line}"
        return self.path

    @property
    def key(self) -> str:
        """Stable identity used for fix selection and trend diffing.

        Rule and path alone are not unique: several findings can share one file -
        every unused plugin is reported against settings.json - and collapsing
        them would make a per-item fix act on whichever one happened to win.
        The discriminator is drawn from the evidence, which is where a rule puts
        the thing the finding is actually about.
        """
        extra = ""
        for field_name in ("plugin", "group", "token", "name", "skill", "entry"):
            value = (self.evidence or {}).get(field_name)
            if isinstance(value, str) and value:
                extra = value
                break
        return "|".join([self.rule, self.path, str(self.line or ""), extra])


@dataclass
class Inventory:
    scanned_at: str = ""
    roots: dict[str, str] = field(default_factory=dict)
    skills: list[Skill] = field(default_factory=list)
    instructions: list[Instruction] = field(default_factory=list)
    workflows: list[Workflow] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    agents: list[AgentDef] = field(default_factory=list)
    hooks: list[Hook] = field(default_factory=list)
    plugins: list[Plugin] = field(default_factory=list)
    #: Externally-managed skill toolkits (see studio.toolkits), as plain dicts so
    #: the inventory stays JSON-round-trippable.
    toolkits: list[dict] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "skills": len(self.skills),
            "instructions": len(self.instructions),
            "workflows": len(self.workflows),
            "commands": len(self.commands),
            "agents": len(self.agents),
            "hooks": len(self.hooks),
            "plugins": len(self.plugins),
            "toolkits": len(self.toolkits),
        }

    def skill_by_name(self, name: str) -> list[Skill]:
        return [s for s in self.skills if s.name == name]


def _enc(obj):
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def to_json(obj, indent: int = 2) -> str:
    """Serialise any dataclass in this module (or list thereof) to JSON."""
    if isinstance(obj, list):
        payload = [asdict(o) for o in obj]
    else:
        payload = asdict(obj)
    return json.dumps(payload, indent=indent, ensure_ascii=False, default=_enc)
