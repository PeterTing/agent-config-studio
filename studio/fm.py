"""Minimal YAML-frontmatter reader.

Deliberately dependency-free: this tool audits the agent config, so it must run
from launchd on a bare interpreter with no venv and no pip state.

Supports the subset that actually appears in SKILL.md / agent / command files:
plain scalars, quoted scalars, block scalars (``|`` ``>`` with ``-``/``+``
chomping), inline flow lists, and block lists. Nested mappings are captured as
raw text under their key rather than parsed, which is enough for every check we
run and avoids pretending to be a YAML implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DELIM = re.compile(r"^---[ \t]*$")
_KEY = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_.\-]+)[ \t]*:(?P<rest>.*)$")
_BLOCK = re.compile(r"^([|>])([+-]?)(\d*)$")


@dataclass
class Frontmatter:
    """Parsed frontmatter plus the body that followed it."""

    present: bool
    data: dict[str, object] = field(default_factory=dict)
    body: str = ""
    #: 1-indexed line number of each top-level key, for precise finding anchors.
    key_lines: dict[str, int] = field(default_factory=dict)
    #: Non-fatal problems noticed while parsing (e.g. tabs used for indentation).
    warnings: list[str] = field(default_factory=list)

    def get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)

    def text(self, key: str) -> str | None:
        """Return a key as a single-line string, or None when absent/non-scalar."""
        v = self.data.get(key)
        if v is None:
            return None
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
        return str(v)


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _parse_flow_list(s: str) -> list[str]:
    inner = s.strip()[1:-1].strip()
    if not inner:
        return []
    out, buf, quote = [], [], ""
    for ch in inner:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch in "\"'":
            quote = ch
        elif ch == ",":
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf or out:
        out.append("".join(buf).strip())
    return [_strip_quotes(x) for x in out if x.strip()]


def _dedent_block(lines: list[str]) -> list[str]:
    indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    cut = min(indents) if indents else 0
    return [ln[cut:] if len(ln) >= cut else ln.lstrip() for ln in lines]


def _fold(lines: list[str]) -> str:
    """Folded (``>``) scalar: blank lines become newlines, others join with spaces."""
    parts: list[str] = []
    run: list[str] = []
    for ln in lines:
        if ln.strip():
            run.append(ln.strip())
        else:
            if run:
                parts.append(" ".join(run))
                run = []
            parts.append("")
    if run:
        parts.append(" ".join(run))
    return "\n".join(parts)


def parse(text: str) -> Frontmatter:
    """Parse leading frontmatter out of ``text``."""
    lines = text.split("\n")
    if not lines or not _DELIM.match(lines[0]):
        return Frontmatter(present=False, body=text)

    end = None
    for i in range(1, len(lines)):
        if _DELIM.match(lines[i]):
            end = i
            break
    if end is None:
        # Opened but never closed: treat the whole file as body so we still lint it.
        return Frontmatter(
            present=False, body=text, warnings=["frontmatter opened with --- but never closed"]
        )

    block = lines[1:end]
    body = "\n".join(lines[end + 1 :])
    fm = Frontmatter(present=True, body=body)

    i = 0
    while i < len(block):
        raw = block[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        m = _KEY.match(raw)
        if not m:
            i += 1
            continue
        indent, key, rest = m.group("indent"), m.group("key"), m.group("rest")
        if indent.strip("\t") != indent or "\t" in indent:
            fm.warnings.append(f"tab used for indentation near key '{key}'")
        if len(indent) > 0:
            # Nested key: skip; top-level capture below already grabbed the block.
            i += 1
            continue

        fm.key_lines[key] = i + 2  # +1 for the opening ---, +1 for 1-indexing
        rest_s = rest.strip()
        bm = _BLOCK.match(rest_s)

        if bm:
            style, chomp = bm.group(1), bm.group(2)
            collected: list[str] = []
            j = i + 1
            while j < len(block):
                ln = block[j]
                if ln.strip() and not ln.startswith((" ", "\t")):
                    break
                collected.append(ln)
                j += 1
            ded = _dedent_block(collected)
            val = "\n".join(ded) if style == "|" else _fold(ded)
            if chomp == "-":
                val = val.rstrip("\n")
            elif chomp == "":
                val = val.rstrip("\n")
            fm.data[key] = val.strip() if style == ">" else val
            i = j
            continue

        if rest_s.startswith("[") and rest_s.endswith("]"):
            fm.data[key] = _parse_flow_list(rest_s)
            i += 1
            continue

        if rest_s == "":
            # Either a block list or a nested mapping.
            items: list[str] = []
            nested: list[str] = []
            j = i + 1
            while j < len(block):
                ln = block[j]
                if ln.strip() and not ln.startswith((" ", "\t")):
                    break
                s = ln.strip()
                if s.startswith("- "):
                    items.append(_strip_quotes(s[2:]))
                elif s:
                    nested.append(ln)
                j += 1
            fm.data[key] = items if items else "\n".join(_dedent_block(nested))
            i = j
            continue

        # Plain scalar, possibly continued by more-indented lines.
        val = _strip_quotes(rest_s)
        j = i + 1
        cont: list[str] = []
        while j < len(block):
            ln = block[j]
            if not ln.strip():
                break
            if not ln.startswith((" ", "\t")):
                break
            if _KEY.match(ln) and len(ln) - len(ln.lstrip()) == 0:
                break
            cont.append(ln.strip())
            j += 1
        if cont:
            val = " ".join([val] + cont).strip()
        fm.data[key] = val
        i = j if cont else i + 1

    return fm


def read(path) -> Frontmatter:
    """Parse the file at ``path``; unreadable files yield an empty Frontmatter."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse(fh.read())
    except OSError as exc:
        return Frontmatter(present=False, warnings=[f"unreadable: {exc}"])
