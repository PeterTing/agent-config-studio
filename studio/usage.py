"""Build a usage index from local Claude Code / Codex history.

This exists so the "is this plugin actually used?" question is answered from
evidence instead of a guess. The index reports its own coverage (files and bytes
read, plus anything skipped) so a zero-usage result can be distinguished from
"no history was available to look at".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CODEX_DIR = os.path.join(HOME, ".codex")

#: Slash command at the start of a typed prompt, optionally plugin-qualified.
_SLASH_RE = re.compile(r"(?:^|\s)/([a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)?)", re.I)

#: Cap on transcript bytes read per run. Coverage is reported, never silently
#: truncated, so a capped run is visibly a capped run.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024 * 1024

#: Per-file result cache. Transcripts are append-mostly and the archive only
#: grows, so re-reading 25 GB every run is wasted work: a scheduled daily check
#: should touch only what changed. Keyed on (size, mtime) - a transcript whose
#: size and mtime both match has not been rewritten.
CACHE_VERSION = 3


def _coverage_pct(read: int, total: int) -> float | None:
    """Percentage of available history files actually read.

    None only when there was nothing to read at all - which is different from
    "read nothing of what existed", and the two must not look alike to a rule
    deciding whether it may call something unused.
    """
    if total <= 0:
        return None
    return round(100.0 * read / total, 1)


@dataclass
class UsageIndex:
    #: skill/command token -> invocation count
    tokens: dict[str, int] = field(default_factory=dict)
    #: plugin name -> invocation count (derived from `plugin:skill` prefixes)
    plugins: dict[str, int] = field(default_factory=dict)
    #: raw MCP tool name -> call count. Many plugins ship MCP tools rather than
    #: skills, so counting only Skill calls would report them as unused.
    mcp_tools: dict[str, int] = field(default_factory=dict)
    #: subagent_type -> spawn count. Some plugins ship only agent definitions, so
    #: this is the third way a plugin can be in real use.
    agent_types: dict[str, int] = field(default_factory=dict)
    #: Codex built-in tool name -> call count, kept separate from MCP tools.
    codex_tools: dict[str, int] = field(default_factory=dict)
    files_read: int = 0
    bytes_read: int = 0
    files_skipped: int = 0
    sources: list[str] = field(default_factory=list)
    truncated: bool = False
    total_transcripts: int = 0
    #: History files that actually exist. Counted rather than assumed: hardcoding
    #: two meant a machine using only one runtime could never reach 100%, and the
    #: CB001 fixer refuses to act below full coverage - so complete evidence was
    #: being rejected as incomplete.
    total_history_files: int = 0
    total_transcript_bytes: int = 0
    #: Files satisfied from the per-file cache rather than re-read from disk.
    files_cached: int = 0

    def bump(self, token: str, n: int = 1) -> None:
        token = token.strip()
        if not token:
            return
        self.tokens[token] = self.tokens.get(token, 0) + n
        if ":" in token:
            plugin = token.split(":", 1)[0]
            self.plugins[plugin] = self.plugins.get(plugin, 0) + n

    @property
    def available(self) -> bool:
        return self.files_read > 0

    def summary(self) -> dict:
        return {
            "available": self.available,
            "distinct_tokens": len(self.tokens),
            "total_invocations": sum(self.tokens.values()),
            "plugins_seen": len(self.plugins),
            "distinct_mcp_tools": len(self.mcp_tools),
            "mcp_tool_calls": sum(self.mcp_tools.values()),
            "distinct_agent_types": len(self.agent_types),
            "agent_spawns": sum(self.agent_types.values()),
            "codex_tool_calls": sum(self.codex_tools.values()),
            "files_read": self.files_read,
            "bytes_read": self.bytes_read,
            "files_skipped": self.files_skipped,
            "truncated": self.truncated,
            "files_cached": self.files_cached,
            "transcripts_total": self.total_transcripts,
            "history_files_total": self.total_history_files,
            # What the percentage is actually computed over. Without it the page
            # could only show `files_read / transcripts_total`, which reads as
            # more files read than exist - and this is the evidence the
            # unused-plugin rules rest on.
            "files_total": self.total_transcripts + self.total_history_files,
            "transcript_bytes_total": self.total_transcript_bytes,
            "file_coverage_pct": _coverage_pct(
                self.files_read, self.total_transcripts + self.total_history_files
            ),
            "sources": self.sources,
        }


def _index_history(idx: UsageIndex) -> None:
    """User-typed prompts, which is where slash commands appear."""
    path = os.path.join(CLAUDE_DIR, "history.jsonl")
    if os.path.isfile(path):
        idx.total_history_files += 1
    if not os.path.isfile(path):
        return
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                display = rec.get("display") or ""
                if not isinstance(display, str):
                    continue
                for m in _SLASH_RE.finditer(display[:400]):
                    idx.bump(m.group(1).lower())
        idx.files_read += 1
        idx.bytes_read += size
        idx.sources.append(path)
    except OSError:
        idx.files_skipped += 1


def _blank() -> dict:
    return {"tokens": {}, "mcp_tools": {}, "agent_types": {}, "codex_tools": {}}


def _merge(idx: "UsageIndex", counts: dict) -> None:
    for token, n in counts.get("tokens", {}).items():
        idx.bump(token, n)
    for key in ("mcp_tools", "agent_types", "codex_tools"):
        target = getattr(idx, key)
        for name, n in counts.get(key, {}).items():
            target[name] = target.get(name, 0) + n


def _scan_claude_transcript(path: str) -> dict | None:
    """Index Skill tool calls in one transcript.

    Both Skill invocations and MCP tool calls are counted. Only lines containing
    one of those markers are JSON-parsed. Transcripts
    run to gigabytes in total, and parsing every line made a full pass impossible,
    which forced a byte cap and left coverage at a few percent - low enough that a
    zero-usage result meant nothing.
    """
    out = _blank()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if (
                    '"Skill"' not in line
                    and '"mcp__' not in line
                    and "subagent_type" not in line
                ):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or ""
                    if name == "Skill":
                        args = block.get("input") or {}
                        if isinstance(args, dict):
                            token = args.get("skill") or args.get("name") or ""
                            if isinstance(token, str) and token.strip():
                                k = token.strip().lower()
                                out["tokens"][k] = out["tokens"].get(k, 0) + 1
                    elif isinstance(name, str) and name.startswith("mcp__"):
                        out["mcp_tools"][name] = out["mcp_tools"].get(name, 0) + 1
                    elif name in ("Agent", "Task"):
                        args = block.get("input") or {}
                        if isinstance(args, dict):
                            at = args.get("subagent_type") or ""
                            if isinstance(at, str) and at:
                                out["agent_types"][at] = out["agent_types"].get(at, 0) + 1
    except OSError:
        return None
    return out


#: A SKILL.md path inside a shell argument. Codex has no Skill tool: it reads the
#: file, so a read of `<something>/skills/<name>/SKILL.md` is a skill invocation.
_SKILL_READ_RE = re.compile(r"skills/([A-Za-z0-9._-]+)/SKILL\.md")


def _scan_codex_session(path: str) -> dict | None:
    """Index one Codex rollout transcript.

    Codex records tool calls as ``payload.type == "function_call"`` with the tool
    in ``payload.name``. Skill usage shows up as a shell command reading a
    SKILL.md path rather than a dedicated tool call, so both are counted.
    """
    out = _blank()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"function_call"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "function_call":
                    continue
                name = payload.get("name") or ""
                if isinstance(name, str) and name:
                    if any(sep in name for sep in ("__", ".", ":")):
                        out["mcp_tools"][name] = out["mcp_tools"].get(name, 0) + 1
                    else:
                        out["codex_tools"][name] = out["codex_tools"].get(name, 0) + 1
                args = payload.get("arguments")
                if isinstance(args, str) and "SKILL.md" in args:
                    for m in _SKILL_READ_RE.finditer(args):
                        k = m.group(1).lower()
                        out["tokens"][k] = out["tokens"].get(k, 0) + 1
    except OSError:
        return None
    return out


def _index_codex_history(idx: UsageIndex) -> None:
    path = os.path.join(CODEX_DIR, "history.jsonl")
    if os.path.isfile(path):
        idx.total_history_files += 1
    if not os.path.isfile(path):
        return
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = rec.get("text") or rec.get("display") or rec.get("content") or ""
                if not isinstance(text, str):
                    continue
                for m in _SLASH_RE.finditer(text[:400]):
                    idx.bump(m.group(1).lower())
        idx.files_read += 1
        idx.bytes_read += size
        idx.sources.append(path)
    except OSError:
        idx.files_skipped += 1


def _discover(roots: list[str], idx: "UsageIndex") -> list[tuple[float, int, str]]:
    """(mtime, size, path) for every .jsonl transcript under ``roots``."""
    found: list[tuple[float, int, str]] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
            for f in filenames:
                if not f.endswith(".jsonl"):
                    continue
                full = os.path.join(dirpath, f)
                try:
                    st = os.stat(full)
                except OSError:
                    idx.files_skipped += 1
                    continue
                found.append((st.st_mtime, st.st_size, full))
    # Newest first: if the byte cap bites, recent behaviour is what we keep.
    found.sort(reverse=True)
    return found


def _load_cache(path: str | None) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_cache(path: str | None, files: dict) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": CACHE_VERSION, "files": files}, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache miss is a slow run, not a failure


def build(
    max_bytes: int = DEFAULT_MAX_BYTES,
    *,
    cache_path: str | None = None,
    use_cache: bool = True,
) -> UsageIndex:
    """Index every local transcript, reusing cached per-file counts where valid.

    Transcripts are append-mostly and the archive only grows, so a full re-read of
    tens of gigabytes on every scheduled run is wasted I/O. A file whose size and
    mtime both match the cache is reused; anything else is re-scanned. Coverage
    numbers count cached files as read, because their contents are still fully
    represented in the totals.
    """
    idx = UsageIndex()
    _index_history(idx)
    _index_codex_history(idx)

    cache = _load_cache(cache_path) if use_cache else {}
    fresh_cache: dict = {}

    claude_files = _discover([os.path.join(CLAUDE_DIR, "projects")], idx)
    codex_files = _discover(
        [os.path.join(CODEX_DIR, "sessions"), os.path.join(CODEX_DIR, "archived_sessions")], idx
    )

    idx.total_transcripts = len(claude_files) + len(codex_files)
    idx.total_transcript_bytes = sum(s for _m, s, _p in claude_files + codex_files)

    budget = max_bytes
    for group, scanner in ((claude_files, _scan_claude_transcript), (codex_files, _scan_codex_session)):
        for mtime, size, full in group:
            key = full
            cached = cache.get(key)
            if cached and cached.get("size") == size and cached.get("mtime") == int(mtime):
                _merge(idx, cached)
                fresh_cache[key] = cached
                idx.files_read += 1
                idx.bytes_read += size
                idx.files_cached += 1
                continue
            if budget - size < 0:
                idx.truncated = True
                idx.files_skipped += 1
                continue
            counts = scanner(full)
            if counts is None:
                idx.files_skipped += 1
                continue
            _merge(idx, counts)
            fresh_cache[key] = {**counts, "size": size, "mtime": int(mtime)}
            idx.files_read += 1
            idx.bytes_read += size
            budget -= size

    if claude_files:
        idx.sources.append(f"{os.path.join(CLAUDE_DIR, 'projects')}/**/*.jsonl")
    if codex_files:
        idx.sources.append(f"{CODEX_DIR}/sessions/**/*.jsonl")
        idx.sources.append(f"{CODEX_DIR}/archived_sessions/*.jsonl")

    if use_cache and not idx.truncated:
        _save_cache(cache_path, fresh_cache)
    return idx


def plugin_usage(idx: UsageIndex, inventory) -> dict[str, int]:
    """Map raw token and MCP-tool counts onto plugin names.

    Three paths contribute:

    * a ``plugin:skill`` token names its plugin directly;
    * an unqualified skill token resolves through the inventory to the plugin
      that ships that skill;
    * an ``mcp__plugin_<name>_<server>__<tool>`` call attributes to ``<name>``.

    MCP attribution matches against the longest known plugin name rather than
    splitting on underscores, because both plugin and server names contain
    underscores and hyphens (``mcp__plugin_adobe-for-creativity_Adobe_for_...``).
    """
    # Keyed by *bare* plugin name throughout. That is the only identity the logs
    # carry: `plug:skill`, `mcp__plugin_plug_server__tool` and an agent type
    # `plug:coder` all name the plugin without its marketplace, so two installs
    # of one name are indistinguishable here and are counted together. Skills and
    # metadata bytes are keyed by the full `plugin@marketplace` install instead -
    # mixing the two key spaces silently produced zeroes on every lookup.
    counts: dict[str, int] = dict(idx.plugins)

    by_name: dict[str, str] = {}
    for s in inventory.skills:
        if s.plugin and s.name:
            bare = s.plugin.split("@")[0]
            by_name.setdefault(s.name.lower(), bare)
            by_name.setdefault(s.dir_name.lower(), bare)
    for token, n in idx.tokens.items():
        if ":" in token:
            continue
        plugin = by_name.get(token)
        if plugin:
            counts[plugin] = counts.get(plugin, 0) + n

    known = sorted({p.key.split("@")[0] for p in inventory.plugins}, key=len, reverse=True)

    # A plugin agent is addressed as `<plugin>:<agent>`, e.g. `ruflo-core:coder`.
    for at, n in idx.agent_types.items():
        prefix = at.split(":", 1)[0] if ":" in at else ""
        if prefix and prefix in known:
            counts[prefix] = counts.get(prefix, 0) + n

    for tool, n in idx.mcp_tools.items():
        if not tool.startswith("mcp__plugin_"):
            continue
        rest = tool[len("mcp__plugin_") :]
        for name in known:
            if rest == name or rest.startswith(name + "_"):
                counts[name] = counts.get(name, 0) + n
                break
    return counts
