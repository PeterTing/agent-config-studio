"""AI-proposed consolidation, applied only after deterministic validation.

These are the findings with no single correct answer: which sections of an
oversized skill should move out, which of two identical files should survive.
A checker cannot decide them, and until now the tool said so and stopped.

The split of responsibility is strict:

* **The model proposes.** It sees section names, sizes and content, and returns a
  structured plan. It never sees a file handle and never writes anything.
* **Code decides whether the plan is admissible.** Every plan is checked against
  the file it claims to act on: do the named sections exist, is anything claimed
  twice, does the result actually satisfy the rule that raised the finding, is
  any content lost. A plan that fails any check is rejected whole - there is no
  partial application.
* **The existing machinery applies it.** Validated plans become the same
  `ChangeSet` everything else uses, so they are previewable, backed up and
  reversible.

The consequence worth stating plainly: a wrong proposal costs you a rejected
plan or a rollback, never a damaged file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import ai, patch, refactor
from .model import Finding
from .rules import SKILL_BODY_MAX_LINES


@dataclass
class Proposal:
    """A validated-or-rejected consolidation plan."""

    rule: str
    path: str
    ok: bool
    summary: str = ""
    rationale: str = ""
    change_set: patch.ChangeSet | None = None
    rejected_because: list[str] = field(default_factory=list)
    raw_plan: dict | list | None = None
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "path": self.path,
            "ok": self.ok,
            "summary": self.summary,
            "rationale": self.rationale,
            "rejected_because": self.rejected_because,
            "cost_usd": round(self.cost_usd, 4),
            "changes": self.change_set.manifest()["changes"] if self.change_set else [],
            "diff": self.change_set.diff() if self.change_set else "",
        }


# --------------------------------------------------------------------------- #
# SK007 - split an oversized SKILL.md
# --------------------------------------------------------------------------- #

_SPLIT_PROMPT = """You are planning how to split an oversized agent skill file.

The official guidance keeps SKILL.md bodies under {budget} lines and moves
long-form detail into reference files that load on demand. What stays is what the
agent needs to *decide* with: when to use the skill, the core model, the quick
commands. What moves is look-up material: worked examples, exhaustive option
tables, troubleshooting trees.

Skill: {name}
Current body: {body_lines} lines (budget {budget})
File: {path}

Sections, with line counts:
{sections}

Propose which sections move into which reference files. Reply with ONLY a JSON
object, no prose:

{{
  "moves": [
    {{"target": "reference/<kebab-name>.md",
      "heading": "<title for the new file>",
      "note": "<one line telling the agent when to open it>",
      "sections": ["<exact section title>", "..."]}}
  ],
  "rationale": "<two sentences: what you kept in SKILL.md and why>"
}}

Constraints you must satisfy:
- Use section titles exactly as given. Do not invent, rename or merge them.
- Every section appears in at most one move. Sections you do not list stay put.
- What remains must be under {budget} lines.
- Keep at least the sections needed to decide whether and how to use the skill.
- Group by what a reader would want together, not by size alone.
"""


def _sections_of(path: str):
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    from . import fm

    parsed = fm.parse(raw)
    return refactor.parse_sections(parsed.body), parsed.body


def propose_split(finding: Finding, repo_root: str, *, model: str | None = None) -> Proposal:
    """Ask for a split plan for an oversized SKILL.md, then check it holds up."""
    path = finding.path
    p = Proposal(rule="SK007", path=path, ok=False)

    if not os.path.isfile(path):
        p.rejected_because.append("檔案不存在")
        return p

    sections, body = _sections_of(path)
    if len(sections) < 3:
        p.rejected_because.append("章節太少，拆分沒有意義")
        return p

    body_lines = len(body.split("\n"))
    listing = "\n".join(f"- {s.title} ({s.line_count} lines)" for s in sections)
    answer = ai.ask(
        _SPLIT_PROMPT.format(
            budget=SKILL_BODY_MAX_LINES,
            name=os.path.basename(os.path.dirname(path)),
            body_lines=body_lines,
            path=path,
            sections=listing,
        ),
        repo_root=repo_root,
        label="split-plan",
        model=model,
    )
    p.cost_usd = answer.cost_usd
    if not answer.ok or not isinstance(answer.data, dict):
        p.rejected_because.append(answer.error or "模型沒有回傳有效方案")
        return p

    p.raw_plan = answer.data
    p.rationale = str(answer.data.get("rationale") or "")
    moves = answer.data.get("moves")
    if not isinstance(moves, list) or not moves:
        p.rejected_because.append("方案沒有列出任何搬移")
        return p

    # --- validation: everything below is deterministic ---------------------- #
    titles = {s.title: s for s in sections}
    claimed: set[str] = set()
    clean_moves: list[dict] = []

    for move in moves:
        if not isinstance(move, dict):
            p.rejected_because.append("方案格式錯誤")
            continue
        target = str(move.get("target") or "")
        names = move.get("sections")
        if not target.startswith("reference/") or not target.endswith(".md"):
            p.rejected_because.append(f"目標路徑不合規：{target!r}（必須是 reference/*.md）")
            continue
        if "/" in target[len("reference/") :] or ".." in target:
            # One level deep, inside the skill directory: anything else breaks
            # progressive disclosure or escapes the skill.
            p.rejected_because.append(f"目標路徑層級過深或越界：{target!r}")
            continue
        if os.path.exists(os.path.join(os.path.dirname(path), target)):
            # split_skill writes the target wholesale, and the lost-line check
            # only compares the SKILL.md body - so an existing reference file
            # would be silently replaced and its contents would not register as
            # lost. Rejecting is the only outcome that cannot destroy content.
            p.rejected_because.append(f"目標檔案已存在，不覆寫：{target!r}")
            continue
        if not isinstance(names, list) or not names:
            p.rejected_because.append(f"{target} 沒有指定章節")
            continue
        for n in names:
            if n not in titles:
                p.rejected_because.append(f"章節不存在：{n!r}")
            elif n in claimed:
                p.rejected_because.append(f"章節被重複指派：{n!r}")
            else:
                claimed.add(n)
        clean_moves.append(
            {
                "target": target,
                "heading": str(move.get("heading") or target),
                "note": str(move.get("note") or ""),
                "sections": [n for n in names if n in titles],
            }
        )

    if p.rejected_because:
        return p

    moved_lines = sum(titles[n].line_count for n in claimed)
    remaining = body_lines - moved_lines
    if remaining > SKILL_BODY_MAX_LINES:
        p.rejected_because.append(
            f"搬完仍有 {remaining} 行，超過 {SKILL_BODY_MAX_LINES} 行上限"
        )
        return p
    if remaining < 20:
        p.rejected_because.append(f"只剩 {remaining} 行，等於把整個 skill 掏空")
        return p

    try:
        changes = refactor.split_skill(
            path,
            clean_moves,
            pointer_note="細節在這些檔案裡，需要時才載入。",
        )
    except (KeyError, ValueError) as exc:
        p.rejected_because.append(f"套用方案失敗：{exc}")
        return p

    cs = patch.ChangeSet(
        name="ai-split-skill",
        description=f"Split {os.path.basename(os.path.dirname(path))} into "
        f"{len(clean_moves)} reference file(s), per an AI plan validated against the file.",
        changes=changes,
    )

    lost = _lost_lines(body, cs)
    if lost:
        p.rejected_because.append(f"會遺失 {len(lost)} 行內容，方案作廢")
        return p

    # Measure the file that will actually be written. The arithmetic above only
    # subtracts the moved sections, while split_skill then appends a heading, a
    # note and one pointer per output file - so a plan landing just under the
    # budget could still produce an oversized SKILL.md and "fix" nothing.
    written = next((c for c in cs.changes if c.path == path), None)
    if written is not None:
        from . import fm

        parsed = fm.parse(written.new_text)
        actual = len((parsed.body if parsed.present else written.new_text).split("\n"))
        if actual > SKILL_BODY_MAX_LINES:
            p.rejected_because.append(
                f"實際產生的 SKILL.md 仍有 {actual} 行（估算是 {remaining} 行），"
                f"超過 {SKILL_BODY_MAX_LINES} 行上限"
            )
            return p
        remaining = actual

    p.ok = True
    p.change_set = cs
    p.summary = (
        f"{body_lines} → {remaining} 行，搬出 {len(claimed)} 個章節到 "
        f"{len(clean_moves)} 個 reference 檔"
    )
    return p


def _lost_lines(original_body: str, cs: patch.ChangeSet) -> list[str]:
    """Non-blank lines present before but absent from everything produced.

    The check that matters most: a split must move content, never drop it.
    """
    before = {ln.strip() for ln in original_body.split("\n") if ln.strip()}
    after: set[str] = set()
    for c in cs.changes:
        after |= {ln.strip() for ln in c.new_text.split("\n") if ln.strip()}
    return sorted(before - after)


# --------------------------------------------------------------------------- #
# SK013 - two files with identical content
# --------------------------------------------------------------------------- #

_DUPLICATE_PROMPT = """Two agent skill files have byte-identical content.

Paths:
{paths}

Runtimes involved: {runtimes}

There are two sensible outcomes:
- "mirror": they are the same skill shared across runtimes on purpose. Declare
  one as the source and keep both in sync automatically.
- "delete": one is a leftover. Keep the other.

Content (first 60 lines):
{excerpt}

Reply with ONLY JSON:

{{"decision": "mirror" | "delete",
  "source": "<path to keep or use as the source>",
  "remove": ["<path to delete, only when decision is delete>"],
  "name": "<short kebab-case name, only when decision is mirror>",
  "rationale": "<one or two sentences>"}}

Prefer "mirror" when the paths sit in different runtimes and the skill is
plausibly meant for both. Prefer "delete" when one path looks like a rename or
copy left behind in the same runtime.
"""


def propose_duplicate_resolution(
    finding: Finding, repo_root: str, *, model: str | None = None
) -> Proposal:
    """Ask whether two identical files should be mirrored or one removed."""
    p = Proposal(rule="SK013", path=finding.path, ok=False)
    paths = list((finding.evidence or {}).get("paths") or [])
    if len(paths) != 2:
        p.rejected_because.append("這條 finding 沒有剛好兩個路徑，無法自動決定")
        return p
    if not all(os.path.isfile(x) for x in paths):
        p.rejected_because.append("其中一個路徑已不存在")
        return p

    with open(paths[0], encoding="utf-8", errors="replace") as fh:
        excerpt = "\n".join(fh.read().split("\n")[:60])
    runtimes = sorted({"claude" if "/.claude/" in x else "codex" if "/.codex/" in x else "?" for x in paths})

    answer = ai.ask(
        _DUPLICATE_PROMPT.format(
            paths="\n".join(f"- {x}" for x in paths),
            runtimes=", ".join(runtimes),
            excerpt=excerpt,
        ),
        repo_root=repo_root,
        label="duplicate-decision",
        model=model,
    )
    p.cost_usd = answer.cost_usd
    if not answer.ok or not isinstance(answer.data, dict):
        p.rejected_because.append(answer.error or "模型沒有回傳有效決定")
        return p

    p.raw_plan = answer.data
    p.rationale = str(answer.data.get("rationale") or "")
    decision = answer.data.get("decision")
    source = str(answer.data.get("source") or "")

    if source not in paths:
        p.rejected_because.append(f"指定的 source 不在這兩個路徑內：{source!r}")
        return p

    if decision == "delete":
        remove = [x for x in (answer.data.get("remove") or []) if x in paths and x != source]
        if len(remove) != 1:
            p.rejected_because.append("delete 方案必須剛好指定一個要刪除的路徑")
            return p
        p.change_set = patch.ChangeSet(
            name="ai-remove-duplicate",
            description=f"Remove the duplicate at {remove[0]}, keeping {source}.",
            changes=[
                patch.Change(path=remove[0], new_text="", action="delete", reason="duplicate of " + source)
            ],
        )
        p.summary = f"刪除 {os.path.relpath(remove[0], os.path.expanduser('~'))}，保留 source"
        p.ok = True
        return p

    if decision == "mirror":
        gov = os.path.join(repo_root, "canonical", "governance.json")
        if not os.path.isfile(gov):
            p.rejected_because.append("找不到 canonical/governance.json，無法宣告鏡像")
            return p
        with open(gov, encoding="utf-8") as fh:
            data = json.load(fh)
        name = str(answer.data.get("name") or os.path.basename(os.path.dirname(source)))
        home = os.path.expanduser("~")
        short = [x.replace(home, "~") for x in paths]
        if any(g.get("name") == name for g in data.get("mirrors", [])):
            p.rejected_because.append(f"鏡像群組 {name!r} 已存在")
            return p
        data.setdefault("mirrors", []).append(
            {"name": name, "source": source.replace(home, "~"), "paths": short}
        )
        p.change_set = patch.ChangeSet(
            name="ai-declare-mirror",
            description=f"Declare {name} as a managed mirror so MR001 keeps the copies in sync.",
            changes=[
                patch.Change(
                    path=gov,
                    new_text=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    reason="declare a managed mirror",
                )
            ],
        )
        p.summary = f"宣告為鏡像群組 {name!r}，之後 drift 會被 MR001 抓到"
        p.ok = True
        return p

    p.rejected_because.append(f"無法辨識的決定：{decision!r}")
    return p


# --------------------------------------------------------------------------- #

PLANNERS = {
    "SK007": propose_split,
    "SK013": propose_duplicate_resolution,
}


def can_propose(rule: str) -> bool:
    return rule in PLANNERS and ai.available()


def propose(finding: Finding, repo_root: str, *, model: str | None = None) -> Proposal:
    planner = PLANNERS.get(finding.rule)
    if planner is None:
        return Proposal(
            rule=finding.rule,
            path=finding.path,
            ok=False,
            rejected_because=[f"{finding.rule} 沒有對應的 AI 整合規劃器"],
        )
    return planner(finding, repo_root, model=model)
