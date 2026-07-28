"""Checks for always-loaded instruction files (CLAUDE.md / AGENTS.md / rules).

These encode the Claude 5 generation context-engineering guidance: remove
duplication, remove over-specification, remove rules repeated at context
boundaries, and remove instructions telling the model to do what it already does.

An important distinction is enforced deliberately:

* **Flagged** - instructions that add a *step*: "include a final verification
  step", "use a subagent to verify", "double-check your answer". The Opus 5 guide
  says to remove these because they compound with behaviour the model already has.
* **Not flagged** - constraints on *output truthfulness*: "do not claim verified
  without evidence", "report Not covered when a flow was not exercised". These
  are not extra steps and no published guidance asks for their removal.

Conflating the two would make the checker demand deleting a safeguard the
official guidance never objected to, so the patterns below are scoped to the
step-adding forms only.
"""

from __future__ import annotations

import os
import re

from ..model import Inventory, Origin, Owner, Severity
from . import (
    INSTRUCTION_MAX_LINES,
    SPEC_CTX5,
    SPEC_MEMORY,
    SPEC_OPUS5,
    Config,
    LazyRegistry,
    make,
    rule,
)

REG = LazyRegistry()

# --- pattern banks --------------------------------------------------------- #

#: Instructions that add a verification step the model already performs.
_EXTRA_VERIFY_RE = re.compile(
    r"("
    r"final verification step|include a verification step|add a verification (step|phase)"
    r"|double[- ]?check (your|the) (answer|work|output)|re-?verify before (responding|answering)"
    r"|verify (your|its) own work"
    r"|再(次|度)(確認|檢查|驗證)一?(遍|次)?"
    r"|完成後(再|還要)(重新|再)(檢查|確認|驗證)"
    r"|自我(複查|複核)"
    r")",
    re.I,
)

#: Instructions that delegate verification or review of the agent's own work.
_SUBAGENT_VERIFY_RE = re.compile(
    r"("
    r"use a subagent to (verify|review|double|check)"
    r"|subagent[^\n]{0,40}(verify|驗證|複查|複核)"
    r"|派\s*[\w-]*\s*subagent[^\n]{0,30}(review|驗證|複查|檢查)"
    r"|每個\s*agent\s*完成後[^\n]{0,40}review"
    r"|兩階段\s*Review"
    r"|(Spec|規格)\s*Review\s*(→|->|、|＋|\+)\s*(Quality|品質)\s*Review"
    r")",
    re.I,
)

#: A clause that *forbids* the pattern instead of prescribing it. A rule saying
#: "do not use a subagent to verify" is compliance, not a violation, so the
#: subagent/verification patterns are skipped when negated.
_NEGATED_RE = re.compile(
    r"(不用|不要|不得|不可|別|避免|禁止|只用於|僅用於|不預設|不委派|不是|"
    r"\bdo not\b|\bdon't\b|\bnever\b|\bavoid\b|\bno longer\b|\bwithout\b|\brather than\b)",
    re.I,
)

#: Forceful override language the Claude 5 guidance says to remove.
_FORCEFUL_RE = re.compile(
    r"(強制|不可違反|絕對不(可|得|能)|一律不得|嚴禁|禁止|必須|鐵律|Iron Law|硬規則"
    r"|MUST NOT|NEVER|ALWAYS|MANDATORY|EXTREMELY[_ ]IMPORTANT|CRITICAL:|不得)",
    re.I,
)

#: A skill statement that retires an artifact, e.g. "`progress.html` 已淘汰".
_DEPRECATION_RE = re.compile(
    r"`([^`\n]{3,60})`[^\n]{0,24}(已淘汰|已廢棄|已棄用|is deprecated|no longer supported)"
    r"|(?:不再產生|不得(?:再)?(?:要求)?(?:產生|使用|引用)|do not (?:use|generate|reference))[^\n]{0,12}`([^`\n]{3,60})`",
    re.I,
)

#: A mandating context around a token in an instruction file.
_MANDATE_WORDS = ("必要", "必須", "強制", "required", "must", "產生", "更新", "generate", "render")


def _norm(line: str) -> str:
    """Normalise a line for duplicate detection: drop markup, numbering, spacing."""
    s = line.strip().lower()
    s = re.sub(r"^[-*+\d.)#>\s]+", "", s)
    s = re.sub(r"[`*_~\[\]()]", "", s)
    s = re.sub(r"[\s、，,。.:：;；!！?？/]+", "", s)
    return s


#: Markdown scaffolding that repeats legitimately and carries no rule content:
#: table separator rows, horizontal rules, and empty table cells.
_STRUCTURAL_RE = re.compile(r"^[\s|:\-=+*_]+$")


def _is_structural(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _STRUCTURAL_RE.match(stripped):
        return True
    # A table row whose cells are all separator dashes.
    if stripped.startswith("|") and set(stripped) <= set("|-: \t"):
        return True
    return False


def _carries_rule_text(line: str) -> bool:
    """True when a line has enough prose to be a rule rather than scaffolding."""
    if _is_structural(line):
        return False
    letters = re.sub(r"[^\w一-鿿]", "", line)
    return len(letters) >= 8


#: A skill name mentioned as a whole token. Without boundaries a 3-character
#: name such as `doc` matches inside an unrelated word like "docs".
_MIN_NAME_LEN_FOR_MATCH = 4


def _names_in(text: str, names) -> list[str]:
    """Skill names appearing in `text` as complete tokens."""
    out = []
    for n in names:
        if len(n) < _MIN_NAME_LEN_FOR_MATCH:
            continue
        if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text):
            out.append(n)
    return out


def _read(path: str) -> str:
    """Read a file with the generated banner removed.

    Every generated file carries the identical provenance banner, so a rule that
    compares content across files would report that boilerplate as duplication.
    It is not content, and no content rule should ever see it.
    """
    from ..canonical import strip_banner

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return strip_banner(fh.read())
    except OSError:
        return ""


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """Return (1-indexed line number, text) skipping fenced code blocks."""
    out: list[tuple[int, str]] = []
    fenced = False
    for i, ln in enumerate(text.split("\n"), start=1):
        if ln.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append((i, ln))
    return out


# --- rules ----------------------------------------------------------------- #


@rule(
    "IN001",
    "Instruction file exceeds the 200-line target",
    Severity.IMPORTANT,
    SPEC_MEMORY,
    "instructions",
)
def in001(inv: Inventory, cfg: Config):
    for ins in inv.instructions:
        if ins.lines > INSTRUCTION_MAX_LINES:
            est = ins.bytes // 4
            yield make(
                REG["IN001"],
                f"{ins.lines} lines / {ins.bytes:,} bytes (~{est:,} tokens) loaded into "
                f"every session, against a {INSTRUCTION_MAX_LINES}-line target. "
                "Longer files consume more context and reduce adherence.",
                path=ins.path,
                evidence={
                    "lines": ins.lines,
                    "limit": INSTRUCTION_MAX_LINES,
                    "bytes": ins.bytes,
                    "est_tokens": est,
                },
                remedy="Move procedures into skills and path-scoped rules; keep only always-true facts.",
            )


@rule(
    "IN002",
    "Instruction adds a verification step the model already performs",
    Severity.CRITICAL,
    SPEC_OPUS5,
    "instructions",
)
def in002(inv: Inventory, cfg: Config):
    for ins in inv.instructions:
        text = _read(ins.path)
        for lineno, ln in _prose_lines(text):
            m = _EXTRA_VERIFY_RE.search(ln)
            if not m or _NEGATED_RE.search(ln):
                continue
            yield make(
                REG["IN002"],
                f"line {lineno} instructs an extra verification pass: {ln.strip()[:130]!r}. "
                "Opus 5 verifies its own work unprompted; these instructions compound "
                "into over-verification and cost tokens with no quality gain.",
                path=ins.path,
                line=lineno,
                evidence={"match": m.group(0), "line": ln.strip()},
                remedy="Delete the extra pass. Keep only the output constraint "
                "(no 'verified' claim without evidence), which is not a step.",
            )


@rule(
    "IN003",
    "Instruction delegates verification or self-review to a subagent",
    Severity.CRITICAL,
    SPEC_OPUS5,
    "instructions",
)
def in003(inv: Inventory, cfg: Config):
    for ins in inv.instructions:
        text = _read(ins.path)
        for lineno, ln in _prose_lines(text):
            m = _SUBAGENT_VERIFY_RE.search(ln)
            if not m or _NEGATED_RE.search(ln):
                continue
            yield make(
                REG["IN003"],
                f"line {lineno} routes review/verification through a subagent: "
                f"{ln.strip()[:130]!r}. The Opus 5 guide says not to use subagents to "
                "verify or double-check the agent's own work, and to delegate only "
                "genuinely independent, sizeable tracks.",
                path=ins.path,
                line=lineno,
                evidence={"match": m.group(0), "line": ln.strip()},
                remedy="Make main-agent-first the default and reserve delegation for "
                "large independent work.",
            )


@rule(
    "IN004",
    "The same rule is stated twice inside one instruction file",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "instructions",
)
def in004(inv: Inventory, cfg: Config):
    for ins in inv.instructions:
        text = _read(ins.path)
        seen: dict[str, int] = {}
        for lineno, ln in _prose_lines(text):
            if not _carries_rule_text(ln):
                continue
            key = _norm(ln)
            if len(key) < 12:
                continue
            if key in seen:
                yield make(
                    REG["IN004"],
                    f"line {lineno} repeats line {seen[key]} verbatim after normalisation: "
                    f"{ln.strip()[:110]!r}.",
                    path=ins.path,
                    line=lineno,
                    evidence={"first_line": seen[key], "duplicate_line": lineno, "text": ln.strip()},
                    remedy="Keep one statement of the rule and delete the other.",
                )
            else:
                seen[key] = lineno


@rule(
    "IN005",
    "Instruction file restates content that lives in a skill",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "instructions",
)
def in005(inv: Inventory, cfg: Config):
    """Flag instruction sections that copy a skill's own rules.

    The skill is the source of truth and loads on demand; copying its rules into
    an always-loaded file pays for them in every session and lets the two drift.
    """
    local_skills = [s for s in inv.skills if s.origin is Origin.LOCAL]
    for ins in inv.instructions:
        text = _read(ins.path)
        ins_lines = {
            _norm(ln): lineno
            for lineno, ln in _prose_lines(text)
            if _carries_rule_text(ln) and len(_norm(ln)) >= 16
        }
        if not ins_lines:
            continue
        for s in local_skills:
            if s.path not in ins.refs and not _names_in(text, [s.name]):
                continue
            body = _read(s.path)
            skill_keys = {
                _norm(ln)
                for _, ln in _prose_lines(body)
                if _carries_rule_text(ln) and len(_norm(ln)) >= 16
            }
            overlap = sorted(set(ins_lines) & skill_keys)
            if len(overlap) < 3:
                continue
            first = min(ins_lines[k] for k in overlap)
            yield make(
                REG["IN005"],
                f"{len(overlap)} distinct lines are duplicated between this file and "
                f"the {s.name!r} skill (first at line {first}). The skill already "
                "carries them and loads on demand.",
                path=ins.path,
                line=first,
                evidence={
                    "skill": s.name,
                    "skill_path": s.path,
                    "duplicate_line_count": len(overlap),
                    "sample_lines": sorted(ins_lines[k] for k in overlap)[:8],
                },
                remedy=f"Replace the copied block with a one-line pointer to the {s.name!r} skill.",
            )


@rule(
    "IN006",
    "Instruction references a file that does not exist",
    Severity.IMPORTANT,
    SPEC_MEMORY,
    "instructions",
)
def in006(inv: Inventory, cfg: Config):
    for ins in inv.instructions:
        text = _read(ins.path)
        for ref in ins.refs:
            if os.path.exists(ref):
                continue
            line = None
            needle = ref.replace(os.path.expanduser("~") + "/", "~/")
            for lineno, ln in _prose_lines(text):
                if needle in ln or ref in ln:
                    line = lineno
                    break
            yield make(
                REG["IN006"],
                f"references {needle}, which does not exist. The agent will follow the "
                "pointer and find nothing.",
                path=ins.path,
                line=line,
                evidence={"missing": ref},
                remedy="Fix the path or remove the reference.",
            )


@rule(
    "IN007",
    "Forceful override language is used at high density",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "instructions",
)
def in007(inv: Inventory, cfg: Config):
    """Claude 5 guidance: remove all-caps emphasis and forceful language that tries
    to override intent; the model resolves conflicting guidance from context."""
    for ins in inv.instructions:
        text = _read(ins.path)
        prose = _prose_lines(text)
        hits: list[tuple[int, str]] = []
        for lineno, ln in prose:
            for m in _FORCEFUL_RE.finditer(ln):
                hits.append((lineno, m.group(0)))
        if not prose:
            continue
        per_100 = len(hits) * 100.0 / max(len(prose), 1)
        # 8 markers per 100 prose lines: below this, emphasis reads as normal
        # technical writing; above it, the file is arguing with the reader.
        if per_100 <= 8.0:
            continue
        yield make(
            REG["IN007"],
            f"{len(hits)} forceful markers across {len(prose)} prose lines "
            f"({per_100:.1f} per 100 lines). Opus 5 follows instructions "
            "consistently across the full context window without emphatic framing, "
            "and over-specification narrows useful judgement.",
            path=ins.path,
            line=hits[0][0] if hits else None,
            evidence={
                "marker_count": len(hits),
                "prose_lines": len(prose),
                "per_100_lines": round(per_100, 1),
                "samples": [f"L{n}:{t}" for n, t in hits[:12]],
            },
            remedy="State rules once, plainly. Reserve emphatic framing for genuine "
            "safety red lines such as never putting secrets in the transcript.",
        )


@rule(
    "IN008",
    "Instruction mandates an artifact a skill has retired",
    Severity.CRITICAL,
    SPEC_MEMORY,
    "instructions",
)
def in008(inv: Inventory, cfg: Config):
    """Contradictory rules are the worst failure mode: when two instructions
    disagree, the model may pick either one arbitrarily."""
    retired: list[tuple[str, str, int]] = []  # (token, skill_path, line)
    for s in inv.skills:
        if s.origin is not Origin.LOCAL:
            continue
        body = _read(s.path)
        for lineno, ln in _prose_lines(body):
            m = _DEPRECATION_RE.search(ln)
            if not m:
                continue
            token = m.group(1) or m.group(3)
            if token:
                retired.append((token.strip(), s.path, lineno))

    # One retired token can be declared retired on several skill lines; report the
    # instruction line once, citing the first declaration.
    reported: set[tuple[str, int, str]] = set()
    for token, skill_path, skill_line in retired:
        for ins in inv.instructions:
            text = _read(ins.path)
            for lineno, ln in _prose_lines(text):
                if token not in ln:
                    continue
                if not any(w in ln.lower() for w in _MANDATE_WORDS):
                    continue
                key = (ins.path, lineno, token)
                if key in reported:
                    continue
                reported.add(key)
                yield make(
                    REG["IN008"],
                    f"line {lineno} still requires {token!r} ({ln.strip()[:110]!r}), but "
                    f"{os.path.basename(os.path.dirname(skill_path))}/SKILL.md:{skill_line} "
                    "declares it retired. The two rules contradict.",
                    path=ins.path,
                    line=lineno,
                    evidence={
                        "token": token,
                        "instruction_line": ln.strip(),
                        "skill": skill_path,
                        "skill_line": skill_line,
                    },
                    remedy=f"Remove the {token!r} requirement and point at the skill instead.",
                )


@rule(
    "IN010",
    "Instruction section re-documents a skill instead of pointing at it",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "instructions",
)
def in010(inv: Inventory, cfg: Config):
    """Catch paraphrased duplication, which verbatim matching (IN005) misses.

    The common shape is a section that names a skill, then restates that skill's
    rules as a bullet list. The rules are now in two places: one always loaded,
    one loaded on demand, and nothing keeps them consistent. A pointer plus the
    trigger condition is all the always-loaded file needs.
    """
    local_names = {s.name: s for s in inv.skills if s.origin is Origin.LOCAL and s.name}
    if not local_names:
        return

    for ins in inv.instructions:
        text = _read(ins.path)
        lines = text.split("\n")
        # Split into (heading line number, heading text, body line numbers).
        sections: list[tuple[int, str, list[int]]] = []
        current: tuple[int, str, list[int]] | None = None
        fenced = False
        for i, ln in enumerate(lines, start=1):
            if ln.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            if re.match(r"^#{2,4}\s+\S", ln):
                if current:
                    sections.append(current)
                current = (i, ln.strip("# ").strip(), [])
            elif current:
                current[2].append(i)
        if current:
            sections.append(current)

        for head_line, heading, body_lines in sections:
            body_text = "\n".join(lines[i - 1] for i in body_lines)
            named = _names_in(heading, local_names) or _names_in(body_text, local_names)
            if not named:
                continue
            # Routing tables are the intended mechanism, not duplication.
            bullets = [
                i
                for i in body_lines
                if re.match(r"^\s*(?:[-*+]|\d+\.)\s+\S", lines[i - 1])
                and not lines[i - 1].lstrip().startswith("|")
                and _carries_rule_text(lines[i - 1])
            ]
            if len(bullets) < 4:
                continue
            skill = local_names[named[0]]
            yield make(
                REG["IN010"],
                f"section {heading!r} (line {head_line}) names the {skill.name!r} skill and "
                f"then restates {len(bullets)} rule bullets. The skill already carries its "
                "own rules and loads on demand, so this is the same policy maintained twice.",
                path=ins.path,
                line=head_line,
                evidence={
                    "heading": heading,
                    "skill": skill.name,
                    "skill_path": skill.path,
                    "bullet_lines": bullets[:12],
                    "bullet_count": len(bullets),
                    "skills_named": named,
                },
                remedy=f"Reduce the section to the trigger condition plus a pointer to "
                f"the {skill.name!r} skill; keep the detail in the skill only.",
            )


@rule(
    "IN009",
    "Instruction blocks duplicated across runtimes without a managed generator",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "instructions",
)
def in009(inv: Inventory, cfg: Config):
    """CLAUDE.md and AGENTS.md holding the same rules by hand is the duplication
    the Claude 5 guidance calls out, and it drifts. A canonical source rendered
    into both removes the class of problem, so declared generated files are exempt.
    """
    generated = {os.path.expanduser(g.get("target", "")) for g in cfg.generated}
    by_runtime: dict[str, list] = {}
    for ins in inv.instructions:
        by_runtime.setdefault(ins.runtime.value, []).append(ins)
    claude = by_runtime.get("claude", [])
    codex = by_runtime.get("codex", [])
    for a in claude:
        for b in codex:
            if a.path in generated or b.path in generated:
                continue
            ka = {
                _norm(ln)
                for _, ln in _prose_lines(_read(a.path))
                if _carries_rule_text(ln) and len(_norm(ln)) >= 16
            }
            kb = {
                _norm(ln)
                for _, ln in _prose_lines(_read(b.path))
                if _carries_rule_text(ln) and len(_norm(ln)) >= 16
            }
            shared = ka & kb
            if len(shared) < 10:
                continue
            yield make(
                REG["IN009"],
                f"{len(shared)} identical rule lines are maintained by hand in both "
                f"{os.path.basename(a.path)} and {os.path.basename(b.path)}. They will drift.",
                path=a.path,
                evidence={
                    "other": b.path,
                    "shared_line_count": len(shared),
                    "claude_lines": a.lines,
                    "codex_lines": b.lines,
                },
                remedy="Render both from canonical/core.md and declare them in "
                "canonical/governance.json under `generated`.",
            )
