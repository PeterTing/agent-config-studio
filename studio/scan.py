"""Filesystem scanners that build an :class:`~studio.model.Inventory`.

Read-only by construction: nothing in this module opens a file for writing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from datetime import datetime, timezone

from . import fm
from .model import (
    AgentDef,
    Command,
    Hook,
    Instruction,
    Inventory,
    Origin,
    Plugin,
    Runtime,
    Skill,
    Workflow,
)

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CODEX_DIR = os.path.join(HOME, ".codex")
AGENT_LIB_DIR = os.path.join(HOME, ".agent")

#: Absolute or ~-anchored paths mentioned in instruction/skill prose.
#: An absolute or home-relative path to a file a skill points at. The
#: extension must end at a word boundary: without `\b`, `skill-usage.jsonl`
#: matched as `skill-usage.json`, inventing a path that does not exist and
#: reporting it as a broken reference.
_PATH_RE = re.compile(r"(?:~/|/Users/[A-Za-z0-9_.\-]+/)[\w./~\-]+\.(?:md|jsonl|json|py|html|sh|toml|plist)\b")
#: Markdown link targets that stay inside the skill directory.
_MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)\)")
#: Slash-command / skill invocations, e.g. `/qa`, `superpowers:brainstorming`.
_INVOKE_RE = re.compile(r"(?<![\w/])(?:/([a-z][a-z0-9-]{1,63})|([a-z][a-z0-9-]{1,40}:[a-z][a-z0-9-]{1,63}))")

_SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
}


def _digest(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _line_count(text: str) -> int:
    """Physical lines, the way an editor counts them.

    `split("\n")` yields a trailing empty element for any file ending in a
    newline - which is nearly all of them - so a 500-line skill measured 501 and
    tripped the 500-line rule. The same off-by-one shifted the 200-line
    instruction threshold.
    """
    return len(text.splitlines())


def _purpose(text: str, limit: int = 400) -> str:
    """A one-line 'what is this for' for files that have no description field.

    Workflows and commands are plain markdown. Prefer an explicit frontmatter
    description; otherwise take the first real prose line, skipping the title
    heading, which usually just repeats the filename.
    """
    parsed = fm.parse(text)
    described = (parsed.text("description") or "").strip()
    if described:
        return described[:limit]

    body = parsed.body if parsed.present else text
    heading = ""
    in_fence = False
    for line in body.splitlines():
        line = line.strip()
        # Skipping only the ``` marker would let the first line *inside* a code
        # block become the description, so track the fence rather than the line.
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith(("---", "<!--", "|", ">")):
            continue
        if line.startswith("#"):
            if not heading:
                heading = line.lstrip("#").strip()
            continue
        # A list item is a step, not a statement of purpose. Falling back to the
        # heading reads better than quoting step one out of context.
        if re.match(r"^([-*+]|\d+[.)])\s", line):
            break
        return line[:limit]
    return heading[:limit]


def _strip_fences(text: str) -> str:
    """Blank out fenced code blocks.

    A path inside a shell snippet is a redirect target or an argument, not a
    file the skill points the reader at. Counting them as references produced
    phantom broken links - `>> ~/.gstack/analytics/skill-usage.jsonl` is a file
    the script creates on demand, and it was being reported as missing.
    """
    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else line)
    return "\n".join(out)


def _refs(text: str, base_dir: str | None = None) -> list[str]:
    """Extract referenced file paths, expanding ``~`` and relative md links."""
    text = _strip_fences(text)
    out: list[str] = []
    for m in _PATH_RE.findall(text):
        out.append(m.replace("~/", HOME + "/", 1) if m.startswith("~/") else m)
    if base_dir:
        for rel in _MDLINK_RE.findall(text):
            if rel.startswith(("http://", "https://", "/")):
                continue
            out.append(os.path.normpath(os.path.join(base_dir, rel)))
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _invokes(text: str) -> list[str]:
    """Extract skill/command invocations, ignoring fenced code blocks."""
    stripped = re.sub(r"```.*?```", "", text, flags=re.S)
    found: set[str] = set()
    for slash, qualified in _INVOKE_RE.findall(stripped):
        token = slash or qualified
        if not token:
            continue
        # Filter obvious false positives: file extensions and URL fragments.
        if token.endswith((".md", ".py", ".json")):
            continue
        found.add(token)
    return sorted(found)


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #


def _scan_skill_dir(
    root: str, runtime: Runtime, origin: Origin, plugin: str | None, errors: list[str]
) -> list[Skill]:
    out: list[Skill] = []
    if not os.path.isdir(root):
        return out
    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        errors.append(f"{root}: {exc}")
        return out
    for entry in entries:
        d = os.path.join(root, entry)
        path = os.path.join(d, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            raw = _read(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        parsed = fm.parse(raw)
        name = (parsed.text("name") or "").strip()
        desc = (parsed.text("description") or "").strip()
        out.append(
            Skill(
                id=f"skill:{runtime.value}:{entry}" if plugin is None else f"skill:{plugin}:{entry}",
                name=name,
                dir_name=entry,
                path=path,
                runtime=runtime,
                origin=origin,
                description=desc,
                body_lines=_line_count(parsed.body),
                body_bytes=len(parsed.body.encode("utf-8")),
                content_hash=_digest(raw),
                refs=_refs(parsed.body, base_dir=d),
                invokes=_invokes(parsed.body),
                plugin=plugin,
                frontmatter_present=parsed.present,
                frontmatter_keys=sorted(parsed.data.keys()),
                parse_warnings=list(parsed.warnings),
            )
        )
    return out


def _enabled_plugin_keys() -> set[str]:
    """Enabled plugins, keyed by the full ``plugin@marketplace`` identifier.

    The marketplace is part of the identity, not decoration. The same plugin name
    can be installed from two marketplaces and enabled in only one - here
    ``superpowers@claude-plugins-official`` is off while
    ``superpowers@superpowers-marketplace`` is on. Keying on the bare name made
    the disabled install look enabled too, so its skills were counted as
    preloaded and every one of them produced a finding about a skill that is
    never loaded.
    """
    path = os.path.join(CLAUDE_DIR, "settings.json")
    if not os.path.isfile(path):
        return set()
    try:
        data = json.loads(_read(path))
    except (OSError, json.JSONDecodeError):
        return set()
    return {key for key, on in (data.get("enabledPlugins") or {}).items() if on}


def _installed_plugins(errors: list[str]) -> dict[str, dict]:
    """Authoritative per-plugin install record.

    ``installed_plugins.json`` is the only source that says *which* checkout is
    live. Several versions of a plugin can sit side by side under
    ``plugins/cache/<marketplace>/<plugin>/<version>/``, and the marketplace
    manifest's sha does not match those directory names, so inferring the active
    one from the filesystem picks the wrong revision.
    """
    path = os.path.join(CLAUDE_DIR, "plugins", "installed_plugins.json")
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(_read(path))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path}: {exc}")
        return {}
    out: dict[str, dict] = {}
    for key, entries in (data.get("plugins") or {}).items():
        if not isinstance(entries, list) or not entries:
            continue
        # Prefer the user-scope install, else the first recorded one.
        chosen = next((e for e in entries if e.get("scope") == "user"), entries[0])
        if isinstance(chosen, dict) and chosen.get("installPath"):
            out[key] = chosen
    return out


def _skill_dirs_under(root: str) -> list[str]:
    """Every directory under ``root`` holding a *loadable* SKILL.md.

    A SKILL.md nested inside another skill's directory does not load - it is
    bundled reference material the parent skill points at. Counting those as
    skills inflates the preloaded-metadata figure and invents name collisions
    between a skill and its own vendored copy (``skills/ai-sdk/SKILL.md`` versus
    ``skills/ai-sdk/upstream/SKILL.md``). Once a directory is claimed as a skill,
    stop descending into it.
    """
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        # Hidden directories inside a package are the package repo's own tooling
        # - vercel ships a `.claude/skills/` of build helpers that never reach a
        # user's session. Counting them charges you for someone else's dev setup.
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")
        ]
        if "SKILL.md" in filenames:
            found.append(dirpath)
            dirnames[:] = []  # anything below this is the skill's own material
    return sorted(found)


def _declared_skill_paths(errors: list[str]) -> dict[str, list[str]]:
    """Map ``plugin@marketplace`` -> the skill paths that manifest declares.

    Paths stay relative because several plugins can share one source tree while
    declaring different subsets of it (``anthropic-agent-skills`` ships
    document-skills, example-skills and claude-api from one directory). Resolving
    them against each plugin's own install path is what keeps the three from each
    claiming all sixteen skills.

    Keyed by marketplace as well as name, because two marketplaces can each ship
    a plugin called ``superpowers`` declaring different skill sets. Merging them
    under the bare name let a disabled install's declarations pull directories
    out of the enabled install's tree and report them as preloaded.
    """
    plugins_root = os.path.join(CLAUDE_DIR, "plugins")
    out: dict[str, list[str]] = {}
    for base in ("repos", "marketplaces"):
        top = os.path.join(plugins_root, base)
        if not os.path.isdir(top):
            continue
        try:
            markets = sorted(os.listdir(top))
        except OSError as exc:
            errors.append(f"{top}: {exc}")
            continue
        for market in markets:
            manifest = os.path.join(top, market, ".claude-plugin", "marketplace.json")
            if not os.path.isfile(manifest):
                continue
            try:
                data = json.loads(_read(manifest))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{manifest}: {exc}")
                continue
            for entry in data.get("plugins") or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                declared = entry.get("skills")
                if isinstance(declared, list) and declared:
                    out.setdefault(f"{entry['name']}@{market}", []).extend(
                        d for d in declared if isinstance(d, str)
                    )
    return out


def _scan_plugin_skills(errors: list[str]) -> tuple[list[Skill], dict[str, int]]:
    """Scan the skills that enabled plugins actually preload.

    Only enabled plugins are counted, because a plugin that is installed but
    disabled contributes nothing to the startup context. Counting it would
    overstate the metadata budget and point remediation at the wrong plugins.
    """
    enabled = _enabled_plugin_keys()
    declared = _declared_skill_paths(errors)
    installed = _installed_plugins(errors)

    index: dict[str, list[str]] = {}
    for key, rec in installed.items():
        if key not in enabled:
            continue
        root = rec["installPath"]
        rels = declared.get(key)
        if rels:
            # An explicit declaration is authoritative: use exactly those paths.
            dirs = [
                os.path.normpath(os.path.join(root, rel))
                for rel in rels
                if os.path.isfile(os.path.join(root, rel, "SKILL.md"))
            ]
        else:
            dirs = _skill_dirs_under(root)
        # Keyed by the full identifier: two enabled installs of the same plugin
        # name are two plugins, and merging them double-counted each one's skills
        # against the other.
        index.setdefault(key, []).extend(dirs)

    out: list[Skill] = []
    per_plugin: dict[str, int] = {}
    seen: set[str] = set()

    for plugin, dirs in sorted(index.items()):
        for d in dirs:
            path = os.path.join(d, "SKILL.md")
            if not os.path.isfile(path):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                raw = _read(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            parsed = fm.parse(raw)
            entry = os.path.basename(d)
            per_plugin[plugin] = per_plugin.get(plugin, 0) + 1
            out.append(
                Skill(
                    id=f"skill:plugin:{plugin}:{entry}",
                    name=(parsed.text("name") or entry).strip(),
                    dir_name=entry,
                    path=path,
                    runtime=Runtime.CLAUDE,
                    origin=Origin.PLUGIN,
                    description=(parsed.text("description") or "").strip(),
                    body_lines=_line_count(parsed.body),
                    body_bytes=len(parsed.body.encode("utf-8")),
                    content_hash=_digest(raw),
                    refs=[],
                    invokes=[],
                    plugin=plugin,
                    frontmatter_present=parsed.present,
                    frontmatter_keys=sorted(parsed.data.keys()),
                    parse_warnings=list(parsed.warnings),
                )
            )
    return out, per_plugin


def _scan_orphan_library(errors: list[str]) -> list[Skill]:
    root = os.path.join(AGENT_LIB_DIR, "skills")
    return _scan_skill_dir(root, Runtime.UNKNOWN, Origin.ORPHAN_LIBRARY, None, errors)


# --------------------------------------------------------------------------- #
# instructions / workflows / commands / agents
# --------------------------------------------------------------------------- #


def _scan_instructions(errors: list[str]) -> list[Instruction]:
    targets = [
        (os.path.join(CLAUDE_DIR, "CLAUDE.md"), Runtime.CLAUDE),
        (os.path.join(CODEX_DIR, "AGENTS.md"), Runtime.CODEX),
    ]
    rules_dir = os.path.join(CLAUDE_DIR, "rules")
    if os.path.isdir(rules_dir):
        for f in sorted(os.listdir(rules_dir)):
            if f.endswith(".md"):
                targets.append((os.path.join(rules_dir, f), Runtime.CLAUDE))
    codex_rules = os.path.join(CODEX_DIR, "rules")
    if os.path.isdir(codex_rules):
        for f in sorted(os.listdir(codex_rules)):
            if f.endswith((".md", ".rules")):
                targets.append((os.path.join(codex_rules, f), Runtime.CODEX))

    out: list[Instruction] = []
    for path, runtime in targets:
        if not os.path.isfile(path):
            continue
        try:
            raw = _read(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        out.append(
            Instruction(
                id=f"instruction:{runtime.value}:{os.path.basename(path)}",
                path=path,
                runtime=runtime,
                lines=_line_count(raw),
                bytes=len(raw.encode("utf-8")),
                sections=[m.group(2).strip() for m in re.finditer(r"^(#{1,3})[ \t]+(.+)$", raw, re.M)],
                refs=_refs(raw),
                invokes=_invokes(raw),
            )
        )
    return out


def _scan_md_dir(root: str, runtime: Runtime, kind: str, errors: list[str]):
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for f in sorted(filenames):
            if not f.endswith(".md"):
                continue
            path = os.path.join(dirpath, f)
            try:
                raw = _read(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            name = os.path.splitext(f)[0]
            base = dict(
                id=f"{kind}:{runtime.value}:{os.path.relpath(path, root)}",
                path=path,
                runtime=runtime,
                lines=_line_count(raw),
            )
            if kind == "workflow":
                out.append(
                    Workflow(
                        **base, description=_purpose(raw), refs=_refs(raw), invokes=_invokes(raw)
                    )
                )
            elif kind == "command":
                out.append(
                    Command(**base, name=name, description=_purpose(raw), invokes=_invokes(raw))
                )
            else:
                parsed = fm.parse(raw)
                out.append(
                    AgentDef(
                        **base,
                        name=(parsed.text("name") or name).strip(),
                        description=(parsed.text("description") or "").strip(),
                        frontmatter_present=parsed.present,
                        declared_name=(parsed.text("name") or "").strip(),
                    )
                )
    return out


# --------------------------------------------------------------------------- #
# hooks and plugins
# --------------------------------------------------------------------------- #

_ADDITIONAL_CTX_RE = re.compile(r'additionalContext\s*:\s*\\?"((?:[^"\\]|\\.)*)\\?"')


def _extract_injection(command: str) -> str:
    m = _ADDITIONAL_CTX_RE.search(command)
    if m:
        return m.group(1).replace('\\"', '"')
    return ""


def _scan_hooks(errors: list[str]) -> list[Hook]:
    out: list[Hook] = []
    for fname in ("settings.json", "settings.local.json"):
        path = os.path.join(CLAUDE_DIR, fname)
        if not os.path.isfile(path):
            continue
        try:
            data = json.loads(_read(path))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        raw_hooks = data.get("hooks") or {}
        if not isinstance(raw_hooks, dict):
            # Valid JSON of the wrong shape used to raise AttributeError and
            # abort the whole scan, so one malformed key took down every check
            # instead of being reported as one unreadable section.
            errors.append(f"{path}: 'hooks' is {type(raw_hooks).__name__}, expected an object")
            raw_hooks = {}
        for event, groups in raw_hooks.items():
            if not isinstance(groups, list):
                continue
            for gi, group in enumerate(groups):
                matcher = group.get("matcher") if isinstance(group, dict) else None
                handlers = group.get("hooks", []) if isinstance(group, dict) else []
                for hi, h in enumerate(handlers):
                    if not isinstance(h, dict):
                        continue
                    cmd = h.get("command", "") or ""
                    out.append(
                        Hook(
                            id=f"hook:{fname}:{event}:{gi}:{hi}",
                            event=event,
                            matcher=matcher,
                            index=hi,
                            type=h.get("type", "command"),
                            command=cmd,
                            if_rule=h.get("if"),
                            status_message=h.get("statusMessage"),
                            source=path,
                            injects=_extract_injection(cmd) or (h.get("prompt", "") or ""),
                        )
                    )
    return out


def _scan_plugins(per_plugin_skills: dict[str, int], errors: list[str]) -> list[Plugin]:
    out: list[Plugin] = []

    # --- Claude Code -------------------------------------------------------
    settings_path = os.path.join(CLAUDE_DIR, "settings.json")
    enabled: dict[str, bool] = {}
    if os.path.isfile(settings_path):
        try:
            enabled = json.loads(_read(settings_path)).get("enabledPlugins", {}) or {}
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{settings_path}: {exc}")

    marketplaces: dict[str, dict] = {}
    km = os.path.join(CLAUDE_DIR, "plugins", "known_marketplaces.json")
    if os.path.isfile(km):
        try:
            payload = json.loads(_read(km))
            if isinstance(payload, dict):
                marketplaces = payload.get("marketplaces", payload)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{km}: {exc}")

    installed = _installed_plugins(errors)

    for key, on in sorted(enabled.items()):
        _, _, market = key.partition("@")
        meta = marketplaces.get(market, {}) if isinstance(marketplaces, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        # `source` is an object such as {"source": "github", "repo": "owner/name"};
        # the repo is what a remote comparison actually needs.
        src = meta.get("source", "")
        repo = ""
        source_type = str(meta.get("source_type") or "")
        if isinstance(src, dict):
            source_type = source_type or str(src.get("source") or "")
            repo = str(src.get("repo") or "")
            src = str(src.get("url") or (f"https://github.com/{repo}" if repo else ""))
        rec = installed.get(key) or {}
        out.append(
            Plugin(
                id=f"plugin:claude:{key}",
                key=key,
                marketplace=market,
                runtime=Runtime.CLAUDE,
                enabled=bool(on),
                source_type=source_type,
                source=str(src),
                last_revision=str(rec.get("gitCommitSha") or ""),
                version=str(rec.get("version") or ""),
                commit=str(rec.get("gitCommitSha") or ""),
                install_path=str(rec.get("installPath") or ""),
                marketplace_repo=repo,
                # Looked up by the full `plugin@marketplace` key, matching how
                # the skills were counted. A bare-name lookup made two installs
                # of one plugin each report the other's skills as well.
                skill_count=per_plugin_skills.get(key, 0),
            )
        )

    # --- Codex -------------------------------------------------------------
    cfg = os.path.join(CODEX_DIR, "config.toml")
    if os.path.isfile(cfg):
        try:
            with open(cfg, "rb") as fh:
                toml = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"{cfg}: {exc}")
            toml = {}
        cx_markets = toml.get("marketplaces", {}) or {}
        for key, meta in sorted((toml.get("plugins", {}) or {}).items()):
            if not isinstance(meta, dict):
                continue
            _, _, market = key.partition("@")
            mm = cx_markets.get(market, {}) if isinstance(cx_markets, dict) else {}
            if not isinstance(mm, dict):
                mm = {}
            out.append(
                Plugin(
                    id=f"plugin:codex:{key}",
                    key=key,
                    marketplace=market,
                    runtime=Runtime.CODEX,
                    enabled=bool(meta.get("enabled")),
                    source_type=str(mm.get("source_type", "")),
                    source=str(mm.get("source", "")),
                    last_revision=str(mm.get("last_revision", "")),
                )
            )
    return out


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def scan() -> Inventory:
    """Scan every known local agent-config root. Never writes."""
    errors: list[str] = []
    inv = Inventory(
        scanned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        roots={
            "claude": CLAUDE_DIR,
            "codex": CODEX_DIR,
            "agent_library": AGENT_LIB_DIR,
        },
    )

    inv.skills += _scan_skill_dir(
        os.path.join(CLAUDE_DIR, "skills"), Runtime.CLAUDE, Origin.LOCAL, None, errors
    )
    inv.skills += _scan_skill_dir(
        os.path.join(CODEX_DIR, "skills"), Runtime.CODEX, Origin.LOCAL, None, errors
    )
    plugin_skills, per_plugin = _scan_plugin_skills(errors)
    inv.skills += plugin_skills
    inv.skills += _scan_orphan_library(errors)

    # Reclassify skills that an external toolkit owns. They live at a local path
    # but are replaced on toolkit upgrade, so editing them in place does not stick.
    from . import toolkits as toolkits_mod

    kits = toolkits_mod.discover(
        [os.path.join(CLAUDE_DIR, "skills"), os.path.join(CODEX_DIR, "skills")]
    )
    owned = toolkits_mod.managed_paths(kits)
    for s in inv.skills:
        if s.origin is Origin.LOCAL and s.path in owned:
            s.origin = Origin.TOOLKIT
    inv.toolkits = [k.to_dict() for k in kits]

    inv.instructions = _scan_instructions(errors)
    inv.workflows = _scan_md_dir(
        os.path.join(CLAUDE_DIR, "workflows"), Runtime.CLAUDE, "workflow", errors
    ) + _scan_md_dir(os.path.join(CODEX_DIR, "workflows"), Runtime.CODEX, "workflow", errors)
    inv.commands = _scan_md_dir(
        os.path.join(CLAUDE_DIR, "commands"), Runtime.CLAUDE, "command", errors
    ) + _scan_md_dir(os.path.join(CODEX_DIR, "commands"), Runtime.CODEX, "command", errors)
    inv.agents = _scan_md_dir(os.path.join(CLAUDE_DIR, "agents"), Runtime.CLAUDE, "agent", errors)
    inv.hooks = _scan_hooks(errors)
    inv.plugins = _scan_plugins(per_plugin, errors)
    inv.scan_errors = errors
    return inv
