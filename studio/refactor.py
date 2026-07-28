"""Split an oversized SKILL.md into one-level-deep reference files.

Progressive disclosure is the official remedy for a SKILL.md over the 500-line
budget: keep the decision-making content in SKILL.md and move the long-form
detail into files that load only when needed.

Section parsing is fence-aware. Skills routinely contain shell blocks whose
comments start with ``#``, and treating those as headings would slice a file in
the middle of a code block.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import fm
from .patch import Change

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")


@dataclass
class Section:
    level: int
    title: str
    #: 0-based index of the heading line within the parsed body.
    start: int
    #: 0-based index one past the last line of the section.
    end: int
    lines: list[str]

    @property
    def heading(self) -> str:
        return f"{'#' * self.level} {self.title}"

    @property
    def line_count(self) -> int:
        return self.end - self.start


def parse_sections(body: str, *, max_level: int = 2) -> list[Section]:
    """Split ``body`` into top-level sections, ignoring headings inside fences."""
    lines = body.split("\n")
    fence: str | None = None
    heads: list[tuple[int, int, str]] = []

    for i, line in enumerate(lines):
        m_fence = _FENCE_RE.match(line)
        if m_fence:
            token = m_fence.group(1)
            if fence is None:
                fence = token[0]
            elif line.strip().startswith(fence * 3):
                fence = None
            continue
        if fence is not None:
            continue
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= max_level:
            heads.append((i, len(m.group(1)), m.group(2)))

    out: list[Section] = []
    for idx, (start, level, title) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        out.append(Section(level=level, title=title, start=start, end=end, lines=lines[start:end]))
    return out


def _toc(section_titles: list[str]) -> list[str]:
    """A contents list, required for reference files over 100 lines."""
    return ["## Contents", ""] + [f"- {t}" for t in section_titles] + [""]


def split_skill(
    skill_path: str,
    moves: list[dict],
    *,
    pointer_title: str = "Reference files",
    pointer_note: str = "",
) -> list[Change]:
    """Move named sections out of a SKILL.md into sibling reference files.

    ``moves`` entries look like::

        {"target": "reference/patterns.md",
         "heading": "QA patterns",
         "sections": ["Batch Execution", "Form QA Pattern"]}

    Section names are matched on the heading text, exactly. A name that matches
    nothing raises, so a silent partial split cannot happen.
    """
    with open(skill_path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    parsed = fm.parse(raw)
    if not parsed.present:
        raise ValueError(f"{skill_path} has no frontmatter; refusing to split")

    front_end = raw.index("\n---", 3) + len("\n---")
    front = raw[:front_end]
    body = parsed.body

    sections = parse_sections(body)
    by_title: dict[str, Section] = {}
    for s in sections:
        by_title.setdefault(s.title, s)

    claimed: set[int] = set()
    changes: list[Change] = []
    pointers: list[str] = []
    skill_dir = os.path.dirname(skill_path)

    for move in moves:
        wanted = move["sections"]
        picked: list[Section] = []
        for title in wanted:
            sec = by_title.get(title)
            if sec is None:
                raise KeyError(f"{skill_path}: no section titled {title!r}")
            if sec.start in claimed:
                raise ValueError(f"{skill_path}: section {title!r} claimed twice")
            claimed.add(sec.start)
            picked.append(sec)

        target_rel = move["target"]
        target_abs = os.path.join(skill_dir, target_rel)
        heading = move.get("heading") or os.path.basename(target_rel)

        content: list[str] = [f"# {heading}", ""]
        if move.get("note"):
            content += [move["note"], ""]
        moved_lines = sum(s.line_count for s in picked)
        if moved_lines > 100:
            content += _toc([s.title for s in picked])
        for sec in picked:
            content += [ln.rstrip() for ln in sec.lines]
            if content and content[-1] != "":
                content.append("")

        changes.append(
            Change(
                path=target_abs,
                new_text="\n".join(content).rstrip("\n") + "\n",
                reason=f"progressive disclosure: {len(picked)} section(s) moved out of "
                f"{os.path.basename(skill_path)}",
                action="create" if not os.path.exists(target_abs) else "modify",
            )
        )
        pointers.append(f"- **{heading}**: [{target_rel}]({target_rel})")

    # Rebuild the SKILL.md body without the moved sections.
    kept: list[str] = []
    body_lines = body.split("\n")
    first_section_start = sections[0].start if sections else len(body_lines)
    kept += body_lines[:first_section_start]
    for sec in sections:
        if sec.start in claimed:
            continue
        kept += sec.lines

    while kept and kept[-1].strip() == "":
        kept.pop()
    kept += ["", f"## {pointer_title}", ""]
    if pointer_note:
        kept += [pointer_note, ""]
    kept += pointers
    kept.append("")

    changes.insert(
        0,
        Change(
            path=skill_path,
            new_text=front + "\n" + "\n".join(kept).rstrip("\n") + "\n",
            reason="trimmed to the 500-line SKILL.md budget",
        ),
    )
    return changes


def set_description(skill_path: str, description: str) -> Change:
    """Replace the frontmatter description, leaving every other key untouched."""
    with open(skill_path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{skill_path}: no frontmatter")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")

    out: list[str] = [lines[0]]
    i = 1
    replaced = False
    while i < end:
        line = lines[i]
        m = re.match(r"^description:\s*(.*)$", line)
        if not m:
            out.append(line)
            i += 1
            continue
        # Skip the old value, including any block-scalar or wrapped continuation.
        i += 1
        while i < end and (lines[i].startswith((" ", "\t")) or lines[i].strip() == ""):
            if lines[i].strip() == "" and not (
                i + 1 < end and lines[i + 1].startswith((" ", "\t"))
            ):
                break
            i += 1
        out.append(f"description: {description}")
        replaced = True
    if not replaced:
        out.insert(1, f"description: {description}")
    out += lines[end:]
    return Change(
        path=skill_path,
        new_text="\n".join(out),
        reason="description now states what the skill does and when to use it",
    )
