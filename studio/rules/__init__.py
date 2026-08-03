"""Rule registry.

Every check maps to a specific published requirement. The ``spec`` URL on each
rule is what makes a "compliant" verdict auditable rather than asserted: you can
open the link and confirm the threshold the checker enforces.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from ..model import Finding, Inventory, Owner, Severity

# --------------------------------------------------------------------------- #
# Official specification sources
# --------------------------------------------------------------------------- #

SPEC_SKILLS = "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices"
SPEC_OPUS5 = "https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5"
SPEC_CTX5 = "https://claude.com/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models"
SPEC_MEMORY = "https://code.claude.com/docs/en/memory"
SPEC_HOOKS = "https://code.claude.com/docs/en/hooks"
SPEC_AGENTS = "https://code.claude.com/docs/en/sub-agents"

# --------------------------------------------------------------------------- #
# Thresholds, each traceable to a published number
# --------------------------------------------------------------------------- #

#: "Keep SKILL.md body under 500 lines for optimal performance" - SPEC_SKILLS
SKILL_BODY_MAX_LINES = 500
#: "description: Maximum 1,024 characters" - SPEC_SKILLS
DESC_MAX_CHARS = 1024
#: "name: Maximum 64 characters" - SPEC_SKILLS
NAME_MAX_CHARS = 64
#: "For reference files longer than 100 lines, include a table of contents" - SPEC_SKILLS
REF_TOC_MIN_LINES = 100
#: "target under 200 lines per CLAUDE.md file" - SPEC_MEMORY
INSTRUCTION_MAX_LINES = 200
#: A description shorter than this cannot carry both "what" and "when".
DESC_MIN_USEFUL_CHARS = 60
#: Budget for the *avoidable* share of preloaded skill metadata, in tokens:
#: metadata contributed by plugins with no recorded usage. Not a published
#: number. Chosen as a local guardrail so a small tail of unused plugins is
#: tolerated while a large one is not. Total preloaded metadata is reported as a
#: metric instead of a limit, because your own skills are not reducible on demand.
AVOIDABLE_METADATA_TOKEN_BUDGET = 2000
#: Rough bytes-per-token used for metadata estimates. Mixed CJK/ASCII config
#: text sits near 4 bytes/token in practice; the checker reports bytes too so
#: the estimate is never the only evidence.
BYTES_PER_TOKEN = 4

#: Reserved words forbidden in a skill name - SPEC_SKILLS
RESERVED_NAME_WORDS = ("anthropic", "claude")


@dataclass
class Waiver:
    rule: str
    path_glob: str
    reason: str
    #: Resolved at load time for a ``toolkit:<name>`` scope. See :meth:`matches`.
    scope_paths: frozenset[str] = frozenset()

    def matches(self, finding: Finding) -> bool:
        if self.rule not in ("*", finding.rule):
            return False
        # A toolkit installs its skills *beside* the ones you wrote, in the same
        # directory, so no path pattern separates them: a glob wide enough to
        # cover gstack also silences your own files, which is the one thing a
        # waiver must never do. Scoping by toolkit asks the toolkit what it
        # manages instead, and covers exactly that.
        if self.path_glob.startswith("toolkit:"):
            return os.path.realpath(finding.path) in self.scope_paths
        if self.path_glob in ("*", ""):
            return True
        # Findings carry absolute paths while people write `~/.claude/...` -
        # the form used in this repo's own governance example. Without expanding
        # it, every waiver written the documented way silently failed to match
        # and the finding kept blocking.
        return fnmatch.fnmatch(finding.path, os.path.expanduser(self.path_glob))


@dataclass
class Config:
    """Runtime configuration for a health run."""

    repo_root: str
    #: Declared byte-identical mirror groups, e.g. the same skill in
    #: ~/.claude and ~/.codex plus a canonical company copy.
    mirrors: list[dict] = field(default_factory=list)
    #: Files generated from canonical/ that must not drift.
    generated: list[dict] = field(default_factory=list)
    waivers: list[Waiver] = field(default_factory=list)
    #: Path globs for content installed from elsewhere. Distinct from a waiver:
    #: a waiver says "acknowledged, not fixing"; this says "not mine to fix,
    #: because an upgrade would overwrite the edit".
    vendored: list[dict] = field(default_factory=list)
    #: Plugin keys observed in real usage; used by the context-budget rules.
    plugin_usage: dict[str, int] = field(default_factory=dict)
    #: skill name -> recorded invocations. Separate from plugin_usage because a
    #: plugin can be busy through its MCP tools while every skill it ships stays
    #: cold, and that distinction is the whole point of the cold-skill rule.
    skill_usage: dict[str, int] = field(default_factory=dict)
    #: Whether a usage index was actually built. Tracked separately from the
    #: counts because "every plugin has zero invocations" produces an empty dict,
    #: and reading that as "no evidence" silently skipped the usage rules in
    #: exactly the case they exist for - an entirely unused plugin set.
    usage_available: bool = False
    #: Whether that index covered *everything*. `available` only means at least
    #: one transcript was read, so a truncated or partially-skipped scan could
    #: still be used to call a plugin unused - and the CB002 fixer would then
    #: offer to disable something whose only use was in the omitted history.
    usage_complete: bool = False
    #: Why governance.json could not be read, when it exists but failed to parse.
    #: Never silently empty: dropping the declarations would remove every mirror,
    #: generated-file, vendored and waiver check at once, and the run would report
    #: PASS while checking none of what the user declared.
    governance_error: str = ""

    def vendored_reason(self, path: str) -> str | None:
        """Return the declared reason when `path` is externally managed."""
        for entry in self.vendored:
            glob = os.path.expanduser(entry.get("path", ""))
            if glob and fnmatch.fnmatch(path, glob):
                return entry.get("reason", "declared externally managed")
        return None

    @property
    def canonical_dir(self) -> str:
        return os.path.join(self.repo_root, "canonical")

    @classmethod
    def load(cls, repo_root: str) -> "Config":
        cfg = cls(repo_root=repo_root)
        path = os.path.join(repo_root, "canonical", "governance.json")
        if not os.path.isfile(path):
            return cfg
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            cfg.governance_error = f"{type(exc).__name__}: {exc}"
            return cfg
        if not isinstance(data, dict):
            cfg.governance_error = f"expected a JSON object, found {type(data).__name__}"
            return cfg
        cfg.mirrors = data.get("mirrors", []) or []
        cfg.generated = data.get("generated", []) or []
        cfg.vendored = data.get("vendored", []) or []
        bad = [w for w in (data.get("waivers", []) or []) if not isinstance(w, dict)]
        if bad:
            # Crashing here takes down health and the dashboard, instead of
            # producing the incomplete-audit finding MR004 exists to give.
            cfg.governance_error = (
                f"{len(bad)} waiver entr(y/ies) are not objects: {bad[:3]!r}"
            )
            return cfg
        cfg.waivers = [
            Waiver(
                rule=w.get("rule", "*"),
                path_glob=w.get("path", "*"),
                reason=w.get("reason", ""),
                scope_paths=_toolkit_scope(w.get("path", "")),
            )
            for w in (data.get("waivers", []) or [])
        ]
        return cfg


def _toolkit_scope(path_glob: str) -> frozenset[str]:
    """Resolve ``toolkit:<name>`` to the set of files that toolkit manages.

    Read once at load rather than per finding: discovery shells out to git, and
    a rule that fires a hundred times would otherwise pay for it a hundred
    times. An unknown name resolves to the empty set, so a typo waives nothing
    rather than everything.
    """
    if not path_glob.startswith("toolkit:"):
        return frozenset()
    wanted = path_glob[len("toolkit:") :].strip()
    from .. import toolkits as toolkits_mod

    dirs = [
        os.path.join(os.path.expanduser("~"), d, "skills")
        for d in (".claude", ".codex")
    ]
    kits = [k for k in toolkits_mod.discover(dirs) if k.name == wanted]
    return frozenset(
        os.path.realpath(p) for p in toolkits_mod.managed_paths(kits)
    )


RuleFn = Callable[[Inventory, Config], Iterator[Finding]]


@dataclass
class Rule:
    code: str
    title: str
    severity: Severity
    spec: str
    fn: RuleFn
    category: str


REGISTRY: list[Rule] = []


def rule(code: str, title: str, severity: Severity, spec: str, category: str):
    """Register a check function under ``code``."""

    def deco(fn: RuleFn) -> RuleFn:
        if any(r.code == code for r in REGISTRY):
            raise ValueError(f"duplicate rule code {code}")
        REGISTRY.append(
            Rule(code=code, title=title, severity=severity, spec=spec, fn=fn, category=category)
        )
        return fn

    return deco


def find(code: str) -> Rule:
    """Look up a registered rule by code."""
    for r in REGISTRY:
        if r.code == code:
            return r
    raise KeyError(f"no rule registered as {code!r}")


class LazyRegistry:
    """``REG["SK001"]`` resolved at call time, so import order cannot matter."""

    def __getitem__(self, code: str) -> Rule:
        return find(code)


def make(
    r: Rule,
    detail: str,
    *,
    path: str = "",
    line: int | None = None,
    evidence: dict | None = None,
    remedy: str = "",
    owner: Owner = Owner.LOCAL,
    severity: Severity | None = None,
) -> Finding:
    """Build a Finding pre-filled from its Rule."""
    return Finding(
        rule=r.code,
        severity=severity or r.severity,
        title=r.title,
        detail=detail,
        path=path,
        line=line,
        evidence=evidence or {},
        remedy=remedy,
        spec=r.spec,
        owner=owner,
    )


def ensure_loaded() -> None:
    """Import the rule modules so REGISTRY is populated.

    Registration happens as an import side effect, so anything that reads
    REGISTRY - including the dashboard's rule listing - has to trigger it first.
    Imports live in a function rather than at module scope because each rule
    module imports names from this one.
    """
    from . import agents, context, hooks, instructions, mirrors, skills, workflows  # noqa: F401


def run_all(inv: Inventory, cfg: Config) -> list[Finding]:
    """Execute every registered rule and apply waivers."""
    ensure_loaded()

    findings: list[Finding] = []
    for r in REGISTRY:
        try:
            findings.extend(r.fn(inv, cfg))
        except Exception as exc:  # a broken check must not hide the other checks
            findings.append(
                Finding(
                    rule=r.code,
                    severity=Severity.IMPORTANT,
                    title=f"checker error in {r.code}",
                    detail=f"{type(exc).__name__}: {exc}",
                    spec=r.spec,
                )
            )
    for f in findings:
        for w in cfg.waivers:
            if w.matches(f):
                f.waived = True
                f.waiver_reason = w.reason
                break
    findings.sort(key=lambda f: (f.waived, f.severity.rank, f.rule, f.path))
    return findings
