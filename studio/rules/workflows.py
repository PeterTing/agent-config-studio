"""Checks for workflow files and command/skill name collisions."""

from __future__ import annotations

import os
import re

from .. import safeio
from ..model import Inventory, Origin, Severity
from . import SPEC_CTX5, SPEC_MEMORY, SPEC_SKILLS, Config, LazyRegistry, make, rule

REG = LazyRegistry()


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _norm(line: str) -> str:
    s = line.strip().lower()
    s = re.sub(r"^[-*+\d.)#>\s]+", "", s)
    s = re.sub(r"[`*_~\[\]()]", "", s)
    s = re.sub(r"[\s、，,。.:：;；!！?？/]+", "", s)
    return s


@rule(
    "WF001",
    "Workflow file is never referenced by any instruction",
    Severity.MINOR,
    SPEC_MEMORY,
    "workflows",
)
def wf001(inv: Inventory, cfg: Config):
    corpus = "\n".join(_read(i.path) for i in inv.instructions)
    for wf in inv.workflows:
        base = os.path.basename(wf.path)
        stem = os.path.splitext(base)[0]
        # Instructions may list workflows by bare stem (`ideate`) rather than by
        # filename, so a stem match counts as reachable.
        if base in corpus or wf.path in corpus:
            continue
        if re.search(rf"(?<![\w-]){re.escape(stem)}(?![\w-])", corpus):
            continue
        yield make(
            REG["WF001"],
            f"{base} is not reachable from any instruction file, so nothing will ever "
            "route to it.",
            path=wf.path,
            evidence={"basename": base, "lines": wf.lines},
            remedy="Add it to the routing table, or delete it.",
        )


@rule(
    "WF002",
    "Workflow references a file that does not exist",
    Severity.IMPORTANT,
    SPEC_MEMORY,
    "workflows",
)
def wf002(inv: Inventory, cfg: Config):
    for wf in inv.workflows:
        text = _read(wf.path)
        for ref in wf.refs:
            if os.path.exists(ref):
                continue
            needle = ref.replace(os.path.expanduser("~") + "/", "~/")
            line = None
            for i, ln in enumerate(text.split("\n"), start=1):
                if needle in ln or ref in ln:
                    line = i
                    break
            yield make(
                REG["WF002"],
                f"references {needle}, which does not exist.",
                path=wf.path,
                line=line,
                evidence={"missing": ref},
                remedy="Fix the path, ship the missing file, or drop the step.",
            )


@rule(
    "WF003",
    "Workflow duplicates the content of a skill it also invokes",
    Severity.MINOR,
    SPEC_CTX5,
    "workflows",
)
def wf003(inv: Inventory, cfg: Config):
    local = {s.name: s for s in inv.skills if s.origin is Origin.LOCAL and s.name}
    for wf in inv.workflows:
        text = _read(wf.path)
        wf_keys = {_norm(ln) for ln in text.split("\n") if len(_norm(ln)) >= 16}
        if not wf_keys:
            continue
        for name, s in local.items():
            if name not in text:
                continue
            skill_keys = {_norm(ln) for ln in _read(s.path).split("\n") if len(_norm(ln)) >= 16}
            overlap = wf_keys & skill_keys
            if len(overlap) < 4:
                continue
            yield make(
                REG["WF003"],
                f"{len(overlap)} lines are duplicated from the {name!r} skill, which "
                "this workflow already tells the agent to read.",
                path=wf.path,
                evidence={"skill": name, "duplicate_line_count": len(overlap)},
                remedy="Keep the pointer, drop the copied body.",
            )


@rule(
    "WF004",
    "Workflow exists for one runtime only",
    Severity.MINOR,
    SPEC_MEMORY,
    "workflows",
)
def wf004(inv: Inventory, cfg: Config):
    """The dual-harness setup is only dual if both sides can route the same intent."""
    claude = {os.path.basename(w.path) for w in inv.workflows if w.runtime.value == "claude"}
    codex = {os.path.basename(w.path) for w in inv.workflows if w.runtime.value == "codex"}
    ignore = {"README.md"}
    for base in sorted((claude ^ codex) - ignore):
        present = "claude" if base in claude else "codex"
        missing = "codex" if base in claude else "claude"
        src = next(
            w for w in inv.workflows if os.path.basename(w.path) == base and w.runtime.value == present
        )
        yield make(
            REG["WF004"],
            f"{base} exists for {present} but not {missing}, so the same intent routes "
            "differently depending on which agent you run.",
            path=src.path,
            evidence={"present_in": present, "missing_from": missing},
            remedy=f"Mirror it into the {missing} workflows directory, or drop it from both.",
        )


@rule(
    "WF005",
    "A command and a skill share the same name",
    Severity.MINOR,
    SPEC_SKILLS,
    "workflows",
)
def wf005(inv: Inventory, cfg: Config):
    # A command and a skill only collide when the same runtime loads both; a
    # Claude command and a Codex skill sharing a name is not a conflict.
    skills_by_runtime: dict[str, dict[str, object]] = {}
    for s in inv.skills:
        if s.origin is Origin.ORPHAN_LIBRARY or not s.name:
            continue
        skills_by_runtime.setdefault(s.runtime.value, {}).setdefault(s.name, s)
    for c in inv.commands:
        match = skills_by_runtime.get(c.runtime.value, {}).get(c.name)
        if match is not None:
            yield make(
                REG["WF005"],
                f"/{c.name} resolves to both a command and a skill; the two can drift "
                "and it is not obvious which one runs.",
                path=c.path,
                evidence={"name": c.name, "skill_path": match.path},
                remedy="Keep one. If the command is a thin wrapper, delete it and let "
                "the skill own the name.",
            )


#: Names shorter than this match unrelated prose too often to be evidence.
_ROUTE_MIN_LEN = 6
#: A line that actually dispatches work somewhere. Either a table row - routing
#: tables are how both runtimes' instructions are written - or a verb that hands
#: the task off. Everything else may use backticks for values, keys and flags.
#: A heading or table-key column that marks a mapping from something no longer
#: available to what replaces it. Column one of such a table is a key, not a
#: route.
_TRANSLATION_RE = re.compile(
    r"舊命令|舊指令|對應做法|命令對應|指令對應"
    r"|\bold\b|\bformer\b|\blegacy\b|\bdeprecated\b|\bwas\b|\bmigrat",
    re.I,
)

_ROUTING_LINE = re.compile(
    r"^\s*\||\b(use|uses|run|runs|invoke|call|read|see|route[sd]?|delegate|apply)\b"
    r"|走|用|跑|讀|見|改用|呼叫|套用|加上",
    re.I,
)
#: Backticked spans that are plainly not a skill/command/workflow name. The
#: leading-dash case matters most: instructions quote CLI flags constantly
#: (`--session-name`, `--profile`), and every one of them looked like a dead
#: route until this excluded them.
_NOT_A_ROUTE = re.compile(
    r"^(-|\$|@|#|mcp__|https?:|~/|\.{1,2}/|/)|[./\\ ()$]|^[a-z]+$",
)


def _plugin_command_names(inv: Inventory) -> set[str]:
    """Commands shipped by installed plugins.

    The inventory only scans commands you wrote, so without this every reference
    to a plugin command - `/codex:adversarial-review` and friends - reads as a
    dead route. A rule that flags working configuration gets muted, and then it
    protects nothing.

    The root comes from the scanned inventory rather than a hardcoded ``~``, so
    the rule reads only what the scan covered and stays isolable in tests.
    """
    import glob

    claude_root = inv.roots.get("claude")
    if not claude_root:
        return set()
    root = os.path.join(claude_root, "plugins")
    if not os.path.isdir(root):
        return set()
    return {
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(root, "**", "commands", "**", "*.md"), recursive=True)
    }


def _routable_names(inv: Inventory, runtime: str | None = None) -> set[str]:
    """Everything a name could resolve to *for one runtime*.

    Resolving against a global set hid a real class of broken route: a Claude
    instruction naming a skill that exists only under ~/.codex reads as valid,
    while Claude cannot invoke it. Each runtime loads only its own, so that is
    the set the check has to use.

    Plugin skills and plugin commands are Claude-side - they are installed under
    ~/.claude/plugins - and are treated as such.
    """
    def keep(item_runtime) -> bool:
        return runtime is None or getattr(item_runtime, "value", item_runtime) == runtime

    names: set[str] = set()
    for s in inv.skills:
        if s.origin is Origin.ORPHAN_LIBRARY:
            continue
        if keep("claude" if s.origin is Origin.PLUGIN else s.runtime):
            names.add(s.name)
    names |= {c.name for c in inv.commands if keep(c.runtime)}
    names |= {
        os.path.splitext(os.path.basename(w.path))[0] for w in inv.workflows if keep(w.runtime)
    }
    names |= {a.name for a in inv.agents if keep(a.runtime)}
    if keep("claude"):
        names |= _plugin_command_names(inv)
    return names


@rule(
    "WF006",
    "Instruction routes to a skill or command that does not exist",
    Severity.IMPORTANT,
    SPEC_MEMORY,
    "workflows",
)
def wf006(inv: Inventory, cfg: Config):
    """A routing table is only worth its weakest entry.

    Instructions and workflows route work by naming things - "security review:
    `/cso`". When a name no longer resolves, nothing errors: the agent reads a
    confident instruction, cannot act on it, and silently does something else.
    That is worse than having no routing line, because the line reads as coverage.

    Only backticked, hyphenated, sufficiently long names count. Bare prose words
    match unrelated text constantly, and a check that cries wolf gets ignored -
    which would cost more than the entries it finds.
    """
    if not _routable_names(inv):
        return  # nothing scanned: knowing of nothing is not evidence of absence
    known_by_runtime: dict[str, set[str]] = {}

    # Grouped by the missing name rather than by file: the same routing table is
    # usually mirrored into both runtimes, and reporting one broken entry twice
    # doubles the noise without adding anything to act on.
    missing: dict[str, list[str]] = {}
    for source in list(inv.instructions) + list(inv.workflows):
        text = safeio.read_text(source.path)
        if not text:
            continue
        rt = getattr(source.runtime, "value", source.runtime)
        if rt not in known_by_runtime:
            known_by_runtime[rt] = _routable_names(inv, rt)
        known = known_by_runtime[rt]
        heading = ""
        translating = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                translating = bool(_TRANSLATION_RE.search(heading))
            elif stripped.startswith("|") and not translating:
                first = stripped.strip("|").split("|")[0].strip()
                if _TRANSLATION_RE.search(first):
                    translating = True
            elif not stripped:
                pass
            if not _ROUTING_LINE.search(line):
                # Backticks mark all sorts of things - status values
                # (`not-covered`), CI trigger names (`pre-pr`), config keys. Only
                # a line that actually routes work can contain a dead route, and
                # without this the rule reported two of those for every real one.
                continue
            searchable = line
            if line.lstrip().startswith("|"):
                cells = line.strip().strip("|").split("|")
                if len(cells) > 1 and translating:
                    # Only in a translation table. Its key column names a command
                    # precisely because it is *not* available here - "old
                    # `/qa-only` -> do this in Codex instead" - and reading those
                    # keys as routes flagged fourteen correct rows for every real
                    # one. Elsewhere the first column is often the target itself
                    # (`| agent-browser | Chrome via CDP | ... |`), so suppressing
                    # it everywhere would silently miss a route that was removed.
                    searchable = "|".join(cells[1:])
            for token in re.findall(r"`([^`\n]{1,80})`", searchable):
                name = token.strip().lstrip("/").split(":")[-1]
                if len(name) < _ROUTE_MIN_LEN or "-" not in name:
                    continue
                if _NOT_A_ROUTE.search(name) or name in known:
                    continue
                sources = missing.setdefault(name, [])
                if source.path not in sources:
                    sources.append(source.path)

    for name, sources in sorted(missing.items()):
            yield make(
                REG["WF006"],
                f"{', '.join(sorted({os.path.basename(p) for p in sources}))} routes work to "
                f"{name!r}, but no skill, command, workflow or subagent of that name exists in "
                "either runtime. The instruction reads as coverage while resolving to nothing.",
                path=sorted(sources)[0],
                evidence={"name": name, "sources": sorted(sources)},
                remedy=(
                    f"Create {name!r}, point the line at whatever replaced it, or delete the "
                    "line. A routing entry that resolves to nothing is worse than no entry."
                ),
            )
