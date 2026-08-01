"""Checks derived from the official Agent Skills authoring specification."""

from __future__ import annotations

import collections
import itertools
import os
import re

from ..model import Finding, Inventory, Origin, Owner, Severity
from . import (
    DESC_MAX_CHARS,
    DESC_MIN_USEFUL_CHARS,
    NAME_MAX_CHARS,
    REF_TOC_MIN_LINES,
    RESERVED_NAME_WORDS,
    SKILL_BODY_MAX_LINES,
    SPEC_SKILLS,
    Config,
    LazyRegistry,
    make,
    rule,
)

_NAME_RE = re.compile(r"^[a-z0-9-]+$")
#: Signals that a description says *when* to use the skill, not just what it is.
#: Deliberately generous about phrasing. A narrow pattern reported real triggers
#: as missing - "Use agent-browser ONLY when...", "Trigger whenever...", "Use for
#: every TEST task" - and a check that flags correct descriptions gets ignored.
_WHEN_RE = re.compile(
    r"(\buse\b[^.;]{0,60}\bwhen\b"
    r"|\bwhen\s+(the user|you|a |an |the task|working|asked|invoked|running|there)"
    r"|\btrigger(s|ed|ing)?\b[^.;]{0,30}\b(when|whenever|on|if|include)"
    r"|\buse\s+(for|before|after|during|whenever|any time)\b"
    r"|\bfor (any|every|all)\b"
    r"|\b(pre-commit|pre-pr|before claiming|before every|after every)\b"
    r"|使用時機|何時使用|在.{0,30}(時|前|後)使用|當.{0,40}(時|使用)|適用於|用於|需要.{0,20}時)",
    re.I,
)
_FIRST_PERSON_RE = re.compile(r"^\s*(i |i'|we |my |you can use|you should use|this lets you)", re.I)
#: A backslash-separated path. `(?![nrtvfb0'"\\])` rules out C-style escape
#: sequences such as `\n` inside embedded string literals, which are not paths.
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:])[\w.\-]+\\(?![nrtvfb0uUx'\"\\])[\w.\-]+\\?"
)
_DATE_GATE_RE = re.compile(
    r"\b(before|after|until|from|as of|prior to)\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"\d{4}-\d{2}|q[1-4]\s*\d{4}|\d{4})\b",
    re.I,
)


def _auditable(inv: Inventory):
    """Skills that are actually wired into a runtime.

    The ~/.agent/skills library is excluded: nothing loads it, so emitting 800
    per-skill findings for it would bury the config that is live. Rule CB003
    reports the library once, as a whole.
    """
    return [s for s in inv.skills if s.origin is not Origin.ORPHAN_LIBRARY]


def _owner(s, cfg: "Config | None" = None) -> Owner:
    if s.origin is not Origin.LOCAL:
        return Owner.VENDOR
    if cfg is not None and cfg.vendored_reason(s.path):
        return Owner.VENDOR
    return Owner.LOCAL


def _sev_for(s, base: Severity, cfg: "Config | None" = None) -> Severity:
    """Vendor-shipped content is reported, but never blocks.

    A plugin or toolkit replaces its own files on upgrade, so an edit there does
    not persist. The actionable response is to upgrade, remove, or waive - not to
    hand-edit a file that will be overwritten."""
    if _owner(s, cfg) is Owner.LOCAL:
        return base
    return Severity.MINOR


@rule("SK001", "SKILL.md is missing YAML frontmatter", Severity.CRITICAL, SPEC_SKILLS, "skills")
def sk001(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if not s.frontmatter_present:
            yield make(
                REG["SK001"],
                "SKILL.md has no closed `---` frontmatter block, so its name and "
                "description are never loaded and the skill can never be selected.",
                path=s.path,
                line=1,
                evidence={"dir": s.dir_name},
                remedy="Add a frontmatter block with `name` and `description`.",
                owner=_owner(s, cfg),
                severity=_sev_for(s, Severity.CRITICAL, cfg),
            )


@rule("SK002", "Skill name violates the name format rules", Severity.CRITICAL, SPEC_SKILLS, "skills")
def sk002(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if not s.frontmatter_present:
            continue
        problems: list[str] = []
        if not s.name:
            problems.append("empty")
        else:
            if not _NAME_RE.match(s.name):
                problems.append("must contain only lowercase letters, numbers and hyphens")
            if len(s.name) > NAME_MAX_CHARS:
                problems.append(f"{len(s.name)} chars exceeds the {NAME_MAX_CHARS}-char limit")
            low = s.name.lower()
            for word in RESERVED_NAME_WORDS:
                if word in low:
                    problems.append(f"contains reserved word '{word}'")
        if problems:
            yield make(
                REG["SK002"],
                f"name={s.name!r}: " + "; ".join(problems),
                path=s.path,
                evidence={"name": s.name, "problems": problems},
                remedy="Rename to a lowercase-hyphen slug without reserved words.",
                owner=_owner(s, cfg),
                severity=_sev_for(s, Severity.CRITICAL, cfg),
            )


@rule(
    "SK003",
    "Skill name does not match its directory name",
    Severity.IMPORTANT,
    SPEC_SKILLS,
    "skills",
)
def sk003(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if _owner(s, cfg) is not Owner.LOCAL or not s.name:
            continue
        if s.name != s.dir_name:
            yield make(
                REG["SK003"],
                f"directory is {s.dir_name!r} but frontmatter declares name {s.name!r}. "
                "Two directories declaring one name means only one of them ever loads.",
                path=s.path,
                evidence={"dir": s.dir_name, "name": s.name},
                remedy=f"Rename the directory to {s.name!r}, or change `name:` to {s.dir_name!r}.",
                owner=Owner.LOCAL,
            )


@rule("SK004", "Skill description is missing or over the length limit", Severity.CRITICAL, SPEC_SKILLS, "skills")
def sk004(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if not s.frontmatter_present:
            continue
        n = len(s.description)
        if n == 0:
            yield make(
                REG["SK004"],
                "description is empty, so the skill can never be selected.",
                path=s.path,
                evidence={"len": 0},
                remedy="Write a description covering what it does and when to use it.",
                owner=_owner(s, cfg),
                severity=_sev_for(s, Severity.CRITICAL, cfg),
            )
        elif n > DESC_MAX_CHARS:
            yield make(
                REG["SK004"],
                f"description is {n} chars, over the {DESC_MAX_CHARS}-char limit.",
                path=s.path,
                evidence={"len": n, "limit": DESC_MAX_CHARS},
                remedy="Trim to the trigger conditions; move detail into the body.",
                owner=_owner(s, cfg),
                severity=_sev_for(s, Severity.CRITICAL, cfg),
            )


@rule(
    "SK005",
    "Skill description does not say when to use the skill",
    Severity.IMPORTANT,
    SPEC_SKILLS,
    "skills",
)
def sk005(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if not s.description:
            continue  # SK004 already covers this
        if _WHEN_RE.search(s.description):
            continue
        # No length escape hatch. A long description that never says when to use
        # the skill is not "carrying enough context" - it is a detailed statement
        # of what the thing is, which is exactly the description that never
        # fires. Two 350-character descriptions here read as thorough while
        # naming no trigger at all, and the length rule hid both.
        yield make(
            REG["SK005"],
            f"description is {len(s.description)} chars and carries no trigger phrase "
            f"({s.description[:90]!r}). The description is the only thing loaded at "
            "startup, so without a 'when to use' clause the skill is effectively invisible.",
            path=s.path,
            evidence={"len": len(s.description), "description": s.description},
            remedy="Append a 'Use when ...' clause naming concrete triggers.",
            owner=_owner(s, cfg),
            severity=_sev_for(s, Severity.IMPORTANT, cfg),
        )


@rule("SK006", "Skill description is not written in third person", Severity.MINOR, SPEC_SKILLS, "skills")
def sk006(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if s.description and _FIRST_PERSON_RE.match(s.description):
            yield make(
                REG["SK006"],
                f"description opens in first/second person: {s.description[:70]!r}. "
                "It is injected into the system prompt, where mixed point-of-view "
                "degrades skill selection.",
                path=s.path,
                evidence={"description": s.description[:200]},
                remedy="Rewrite in third person, e.g. 'Processes X. Use when ...'.",
                owner=_owner(s, cfg),
            )


@rule("SK007", "SKILL.md body exceeds the 500-line budget", Severity.IMPORTANT, SPEC_SKILLS, "skills")
def sk007(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if s.body_lines > SKILL_BODY_MAX_LINES:
            yield make(
                REG["SK007"],
                f"body is {s.body_lines} lines, over the {SKILL_BODY_MAX_LINES}-line budget. "
                "Everything in it competes with conversation history once loaded.",
                path=s.path,
                evidence={"body_lines": s.body_lines, "limit": SKILL_BODY_MAX_LINES},
                remedy="Split detail into one-level-deep reference files under the skill directory.",
                owner=_owner(s, cfg),
                severity=_sev_for(s, Severity.IMPORTANT, cfg),
            )


@rule(
    "SK008",
    "Reference file is more than one level deep from SKILL.md",
    Severity.MINOR,
    SPEC_SKILLS,
    "skills",
)
def sk008(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if _owner(s, cfg) is not Owner.LOCAL:
            continue
        skill_dir = os.path.dirname(s.path)
        for ref in s.refs:
            if not ref.endswith(".md") or not ref.startswith(skill_dir):
                continue
            if ref == s.path or not os.path.isfile(ref):
                continue  # a skill citing its own path is not a nested reference
            try:
                with open(ref, encoding="utf-8", errors="replace") as fh:
                    nested = fh.read()
            except OSError:
                continue
            # A reference file linking *back* to its own SKILL.md is a return
            # link, not added depth. The documented hazard is a forward chain
            # (SKILL.md -> a.md -> b.md), where the last hop gets partially read.
            ref_dir = os.path.dirname(ref)
            deeper = []
            for m in re.findall(r"\[[^\]]*\]\(([^)#\s]+\.md)\)", nested):
                if m.startswith(("http://", "https://")):
                    continue
                if os.path.normpath(os.path.join(ref_dir, m)) == s.path:
                    continue
                deeper.append(m)
            if deeper:
                yield make(
                    REG["SK008"],
                    f"{os.path.relpath(ref, skill_dir)} links onward to "
                    f"{deeper[:3]}. Nested references get partially read, so the "
                    "agent may act on a truncated file.",
                    path=ref,
                    evidence={"nested_links": deeper[:10], "skill": s.name},
                    remedy="Link every reference file directly from SKILL.md instead.",
                    owner=Owner.LOCAL,
                )


@rule(
    "SK009",
    "Long reference file has no table of contents",
    Severity.MINOR,
    SPEC_SKILLS,
    "skills",
)
def sk009(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if _owner(s, cfg) is not Owner.LOCAL:
            continue
        skill_dir = os.path.dirname(s.path)
        for ref in s.refs:
            if not ref.endswith(".md") or not ref.startswith(skill_dir) or not os.path.isfile(ref):
                continue
            if ref == s.path:
                continue  # SK007 already budgets SKILL.md itself
            try:
                with open(ref, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            n = len(text.split("\n"))
            if n <= REF_TOC_MIN_LINES:
                continue
            head = "\n".join(text.split("\n")[:40]).lower()
            if "## contents" in head or "## table of contents" in head or "## 目錄" in head:
                continue
            yield make(
                REG["SK009"],
                f"{os.path.relpath(ref, skill_dir)} is {n} lines with no contents "
                "list, so a partial read hides what else the file covers.",
                path=ref,
                evidence={"lines": n, "skill": s.name},
                remedy="Add a '## Contents' list at the top of the file.",
                owner=Owner.LOCAL,
            )


@rule("SK010", "Windows-style backslash path in a skill", Severity.MINOR, SPEC_SKILLS, "skills")
def sk010(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if _owner(s, cfg) is not Owner.LOCAL:
            continue
        try:
            with open(s.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        # Ignore fenced code (shell line-continuations legitimately use \).
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        prose = re.sub(r"`[^`\n]*`", "", prose)
        hits = [h for h in _WINDOWS_PATH_RE.findall(prose) if "/" not in h][:5]
        if hits:
            yield make(
                REG["SK010"],
                f"backslash paths found ({hits}); these break on Unix.",
                path=s.path,
                evidence={"samples": hits},
                remedy="Use forward slashes in all paths.",
                owner=Owner.LOCAL,
            )


@rule("SK011", "Skill contains time-sensitive branching", Severity.MINOR, SPEC_SKILLS, "skills")
def sk011(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        if _owner(s, cfg) is not Owner.LOCAL:
            continue
        try:
            with open(s.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        hits = _DATE_GATE_RE.findall(text)
        if hits:
            joined = ["".join(h) for h in hits][:4]
            yield make(
                REG["SK011"],
                f"date-gated instructions found ({joined}); these silently become "
                "wrong once the date passes.",
                path=s.path,
                evidence={"samples": joined},
                remedy="Describe the current method, and move superseded guidance under an 'Old patterns' section.",
                owner=Owner.LOCAL,
            )


@rule("SK012", "Two skill directories declare the same name", Severity.CRITICAL, SPEC_SKILLS, "skills")
def sk012(inv: Inventory, cfg: Config):
    buckets: dict[tuple[str, str], list] = collections.defaultdict(list)
    for s in _auditable(inv):
        if not s.name:
            continue
        buckets[(s.runtime.value, s.name)].append(s)
    for (runtime, name), group in sorted(buckets.items()):
        if len(group) < 2:
            continue
        paths = [g.path for g in group]
        identical = len({g.content_hash for g in group}) == 1
        yield make(
            REG["SK012"],
            f"{len(group)} directories in the {runtime} runtime declare name {name!r}"
            + (" with byte-identical content" if identical else " with differing content")
            + ". Only one can ever load; the rest are dead weight.",
            path=paths[0],
            evidence={"paths": paths, "identical": identical, "name": name},
            remedy="Delete the redundant directory (or give it a distinct name).",
            owner=Owner.LOCAL if group[0].origin is Origin.LOCAL else Owner.VENDOR,
            severity=Severity.CRITICAL if group[0].origin is Origin.LOCAL else Severity.MINOR,
        )


@rule(
    "SK013",
    "Byte-identical SKILL.md duplicated across directories",
    Severity.IMPORTANT,
    SPEC_SKILLS,
    "skills",
)
def sk013(inv: Inventory, cfg: Config):
    declared: set[frozenset[str]] = set()
    for group in cfg.mirrors:
        declared.add(frozenset(os.path.expanduser(p) for p in group.get("paths", [])))

    buckets: dict[str, list] = collections.defaultdict(list)
    for s in _auditable(inv):
        buckets[s.content_hash].append(s)
    for h, group in sorted(buckets.items()):
        if len(group) < 2:
            continue
        paths = frozenset(g.path for g in group)
        if any(paths <= d for d in declared):
            continue  # an intentional, declared mirror set
        if len({g.runtime for g in group}) > 1:
            continue  # cross-runtime copies are handled by the mirror rules
        yield make(
            REG["SK013"],
            f"identical content (md5 {h[:12]}) at {sorted(paths)}. Undeclared "
            "duplicates drift apart silently.",
            path=sorted(paths)[0],
            evidence={"md5": h, "paths": sorted(paths)},
            remedy="Delete the copy, or declare the pair as a managed mirror in canonical/governance.json.",
            owner=Owner.LOCAL if group[0].origin is Origin.LOCAL else Owner.VENDOR,
        )


@rule("SK014", "Skill frontmatter failed to parse cleanly", Severity.MINOR, SPEC_SKILLS, "skills")
def sk014(inv: Inventory, cfg: Config):
    for s in _auditable(inv):
        for w in s.parse_warnings:
            yield make(
                REG["SK014"],
                w,
                path=s.path,
                evidence={"warning": w},
                remedy="Fix the frontmatter so it is valid YAML with space indentation.",
                owner=_owner(s, cfg),
            )


@rule(
    "SK015",
    "Skill body contradicts its own frontmatter or leading directive",
    Severity.IMPORTANT,
    SPEC_SKILLS,
    "skills",
)
def sk015(inv: Inventory, cfg: Config):
    """Catch the specific self-contradiction shape: frontmatter says a tool is a
    fallback / not the default, while the body still calls it the default."""
    for s in _auditable(inv):
        if _owner(s, cfg) is not Owner.LOCAL or not s.description:
            continue
        desc = s.description.lower()
        claims_fallback = "fallback" in desc or "not the default" in desc
        if not claims_fallback:
            continue
        try:
            with open(s.path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
        except OSError:
            continue
        for i, ln in enumerate(lines, start=1):
            low = ln.lower()
            if "is the default" in low and s.name.lower() in low:
                yield make(
                    REG["SK015"],
                    f"frontmatter calls this a FALLBACK tool, but line {i} states "
                    f"{ln.strip()[:110]!r}. The agent gets opposite instructions from one file.",
                    path=s.path,
                    line=i,
                    evidence={"line": ln.strip()},
                    remedy="Reword the body line to match the fallback-first policy.",
                    owner=Owner.LOCAL,
                )
                break


REG = LazyRegistry()


def _declared_mirror_paths(cfg: Config) -> set[str]:
    """Every path already under a declared management mechanism.

    Both mirrors and generated targets count. A pair rendered from one canonical
    source is exactly the fix this rule asks for, so continuing to report it
    afterwards would mean the finding could never be cleared - the surest way to
    teach someone to ignore a rule.
    """
    out: set[str] = set()
    for group in cfg.mirrors:
        paths = group.get("paths") or [group.get("source"), group.get("target")]
        for p in paths:
            if p:
                out.add(os.path.expanduser(p))
    for spec in cfg.generated:
        target = spec.get("target")
        if target:
            out.add(os.path.expanduser(target))
    return out


@rule(
    "SK016",
    "Same skill hand-maintained in two runtimes and already drifting",
    Severity.IMPORTANT,
    SPEC_SKILLS,
    "skills",
)
def sk016(inv: Inventory, cfg: Config):
    """One skill name, two runtimes, two copies edited by hand.

    This is the gap between the rules that already existed. ``SK013`` only sees
    copies that are still byte-identical, and a declared mirror group is checked
    by ``MR001`` - so a pair that is *supposed* to be one skill, has drifted, and
    was never declared falls between them and is reported by nothing.

    That is not a hypothetical. In the setup this was written against, the two
    most-invoked skills in the whole configuration - 787 and 478 recorded uses -
    were each maintained as two hand-edited copies whose contents had already
    diverged, with no declaration and no check.

    Divergence is often legitimate: each runtime names its own tools, exactly as
    the instruction files do. The problem is not that the copies differ, it is
    that nothing keeps the *shared* part in step. The remedy is the mechanism
    already used for CLAUDE.md and AGENTS.md - one source plus a per-runtime
    delta - or an explicit mirror declaration if they really must stay identical.
    """
    declared = _declared_mirror_paths(cfg)
    by_name: dict[str, list] = collections.defaultdict(list)
    for s in inv.skills:
        if s.origin is Origin.LOCAL:
            by_name[s.name].append(s)

    for name, group in sorted(by_name.items()):
        runtimes = {s.runtime.value for s in group}
        if len(runtimes) < 2:
            continue
        if any(s.path in declared for s in group):
            continue  # MR001 owns declared groups
        if len({s.content_hash for s in group}) == 1:
            continue  # still identical: SK013's territory
        yield make(
            REG["SK016"],
            f"{name!r} exists in {sorted(runtimes)} as separate hand-maintained files whose "
            "contents have already diverged, and the pair is not declared as a mirror. "
            "Every future edit has to be made twice, and nothing checks that it was.",
            path=sorted(s.path for s in group)[0],
            evidence={
                "skill": name,
                "runtimes": sorted(runtimes),
                "paths": sorted(s.path for s in group),
                "hashes": sorted({s.content_hash for s in group}),
            },
            remedy=(
                "If the difference is only runtime-specific tool names, render both from one "
                "source the way canonical/ renders CLAUDE.md and AGENTS.md. If they must stay "
                "identical, declare the pair in canonical/governance.json so MR001 checks it."
            ),
        )


#: Description overlap above which two skills are likely to compete for the same
#: trigger. Tuned against a real config: 0.18 surfaced genuine pairs, and lower
#: values pulled in unrelated skills that merely share domain vocabulary.
OVERLAP_THRESHOLD = 0.18
#: Words too generic to indicate two skills do the same job.
_OVERLAP_STOPWORDS = frozenset(
    """use when the a an and or for to of in on with is are be this that it you your
    i we they how what which any all from by as at if then than into over under can
    will should must may need needs using used skill skills task tasks work working
    help claude codex agent user request requests asks asked run runs make sure also
    file files code project repo repository""".split()
)


def _overlap_bag(s) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9-]{2,}", f"{s.name} {s.description}".lower())
    return {w for w in words if w not in _OVERLAP_STOPWORDS}


@rule(
    "SK017",
    "Two skills describe overlapping jobs and compete for the same trigger",
    Severity.MINOR,
    SPEC_SKILLS,
    "skills",
)
def sk017(inv: Inventory, cfg: Config):
    """Different names, same job.

    A skill is selected by its description, so two descriptions covering the same
    ground do not merely waste the tokens of the loser - they make selection
    ambiguous, and the one that wins is not necessarily the one that works.
    ``SK012`` and ``SK013`` cannot see this: the names differ and the bytes
    differ.

    Deliberately heuristic and deliberately not auto-fixable. Word overlap is
    evidence that two descriptions cover the same ground, not proof that either
    is redundant, and choosing which survives is an editorial decision. Pairs
    that are the same skill in two runtimes are left to ``SK016``.
    """
    # One entry per skill *name* - a skill mirrored into both runtimes would
    # otherwise report every overlap twice - remembering every runtime it loads
    # in. Two skills only compete when they are loaded together: a Claude-only
    # skill and a Codex-only one never see each other, so pairing them would
    # report a conflict that cannot happen.
    mine: list = []
    runtimes: dict[str, set[str]] = {}
    seen_names: set[str] = set()
    for s in sorted(inv.skills, key=lambda s: (s.name, s.runtime.value)):
        if s.origin is not Origin.LOCAL or not s.description:
            continue
        runtimes.setdefault(s.name, set()).add(s.runtime.value)
        if s.name in seen_names:
            continue
        seen_names.add(s.name)
        mine.append(s)
    bags = {id(s): _overlap_bag(s) for s in mine}
    usage = cfg.skill_usage or {}

    for a, b in itertools.combinations(mine, 2):
        if a.name == b.name:
            continue  # cross-runtime pair of one skill: SK016's job
        if not (runtimes.get(a.name, set()) & runtimes.get(b.name, set())):
            continue  # never loaded together, so they cannot compete
        A, B = bags[id(a)], bags[id(b)]
        if not A or not B:
            continue
        score = len(A & B) / len(A | B)
        if score < OVERLAP_THRESHOLD:
            continue
        ha, hb = usage.get(a.name, 0), usage.get(b.name, 0)
        yield make(
            REG["SK017"],
            f"{a.name!r} ({ha} recorded use(s)) and {b.name!r} ({hb}) share "
            f"{score:.0%} of their description vocabulary, so the model has to choose "
            "between them on nearly identical evidence. Shared terms: "
            + ", ".join(sorted(A & B)[:10])
            + ".",
            path=a.path,
            evidence={
                "skills": [a.name, b.name],
                "paths": [a.path, b.path],
                "overlap": round(score, 3),
                "invocations": [ha, hb],
                "shared_terms": sorted(A & B)[:20],
            },
            remedy=(
                "Merge them, or narrow one description so each states a distinct trigger. "
                "If one has never been invoked while the other has, that is a strong hint "
                "which description is actually doing the work."
            ),
        )
