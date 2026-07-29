"""Checks for hooks configured in settings.json.

Hooks are the one part of the config that is *enforced* rather than advisory, so
a buggy hook silently taxes every turn. Two failure shapes matter most: a shell
condition that fires when it should not, and a hook used to re-inject static
rules that belong in an instruction file.
"""

from __future__ import annotations

import re

from ..model import Inventory, Severity
from . import SPEC_CTX5, SPEC_HOOKS, Config, LazyRegistry, make, rule

REG = LazyRegistry()

#: `echo "$VAR" | grep -q -v ...` matches on empty input, because echo of an
#: empty string still emits one (empty) line that fails the pattern.
_ECHO_GREP_V_RE = re.compile(
    r"echo\s+\"?\$\{?(?P<var>\w+)\}?\"?\s*\|\s*grep\s+-[a-zA-Z]*q[a-zA-Z]*v|"
    r"echo\s+\"?\$\{?(?P<var2>\w+)\}?\"?\s*\|\s*grep\s+-[a-zA-Z]*v[a-zA-Z]*q",
    re.I,
)
#: An empty-string guard, in either `[ -z "$X" ]` or `[[ -z "$X" ]]` form.
#: The variable is always captured. A bracket-only alternative used to match
#: first - `re.search` returns the leftmost match - leaving `var` as None, and
#: the caller accepted that as a guard for whatever variable it was checking.
#: A guard on some *other* variable then silenced the very finding this rule
#: exists to raise.
_GUARD_RE = re.compile(r"\[{1,2}?\s*-z\s+\"?\$\{?(?P<var>\w+)\}?\"?|-z\s+\"?\$\{?(?P<var2>\w+)\}?\"?")


def _guards(command: str, var: str) -> bool:
    """Whether `command` guards against `var` being empty.

    Every guard in the command is examined, not just the first: a hook can check
    several variables, and only a guard naming this one counts.
    """
    for m in _GUARD_RE.finditer(command or ""):
        if (m.group("var") or m.group("var2")) == var:
            return True
    return False


#: Imperative framing in injected context.
_IMPERATIVE_RE = re.compile(
    r"(請(拒絕|先|確認|執行)|必須|不可|應該|你要|你必須"
    r"|\byou must\b|\bplease (run|reject|confirm|execute)\b|\bdo not\b|\brefuse\b|\breject\b)",
    re.I,
)

#: Events whose payload lands next to every prompt or turn boundary.
_BOUNDARY_EVENTS = {"UserPromptSubmit", "UserPromptExpansion", "SessionStart", "PreCompact", "Stop"}


@rule(
    "HK001",
    "Hook condition fires on empty input (false positive)",
    Severity.CRITICAL,
    SPEC_HOOKS,
    "hooks",
)
def hk001(inv: Inventory, cfg: Config):
    for h in inv.hooks:
        if h.type != "command" or not h.command:
            continue
        m = _ECHO_GREP_V_RE.search(h.command)
        if not m:
            continue
        var = m.group("var") or m.group("var2")
        if _guards(h.command, var):
            continue
        yield make(
            REG["HK001"],
            f"`echo \"${var}\" | grep -qv ...` with no empty guard. When ${var} is "
            f"empty, echo still emits one empty line, that line does not match the "
            f"pattern, so `grep -v` selects it and the hook fires. Result: this "
            f"{h.event} hook injects its message on unrelated tool calls.",
            path=h.source,
            evidence={
                "event": h.event,
                "matcher": h.matcher,
                "variable": var,
                "if_rule": h.if_rule,
                "command_excerpt": h.command[:240],
            },
            remedy=f'Add `[ -z "${var}" ] && exit 0` before the grep, or pipe with '
            f"`printf '%s' \"${var}\"` which emits nothing when empty.",
        )


@rule(
    "HK002",
    "Injected hook context is phrased as a command, not a fact",
    Severity.IMPORTANT,
    SPEC_HOOKS,
    "hooks",
)
def hk002(inv: Inventory, cfg: Config):
    for h in inv.hooks:
        if not h.injects:
            continue
        m = _IMPERATIVE_RE.search(h.injects)
        if not m:
            continue
        yield make(
            REG["HK002"],
            f"{h.event} hook injects an imperative: {h.injects[:120]!r}. Injected "
            "context should state environment facts and let the agent decide; "
            "out-of-band commands compete with the user's actual instructions.",
            path=h.source,
            evidence={"event": h.event, "match": m.group(0), "injects": h.injects[:300]},
            remedy="Rewrite as a factual statement, e.g. 'No QA run is recorded for "
            "this branch.' instead of 'reject this operation and run /qa-only'.",
        )


@rule(
    "HK003",
    "Hook re-injects static rules at a context boundary",
    Severity.IMPORTANT,
    SPEC_CTX5,
    "hooks",
)
def hk003(inv: Inventory, cfg: Config):
    """Claude 5 guidance: stop repeating rules at context boundaries; older models
    needed it. Static content belongs in an instruction file, which loads once."""
    for h in inv.hooks:
        if h.event not in _BOUNDARY_EVENTS or not h.injects:
            continue
        text = h.injects
        # A boundary hook that names instruction files or restates routing rules
        # is duplicating always-loaded content.
        signals = [
            s
            for s in ("CLAUDE.md", "AGENTS.md", "意圖", "路由", "workflow", "Skill", "skill")
            if s in text
        ]
        if len(text) < 60 or not signals:
            continue
        yield make(
            REG["HK003"],
            f"{h.event} fires on every prompt and injects {len(text)} chars restating "
            f"instruction-file content (mentions {signals[:4]}). That content is "
            "already loaded once per session; repeating it costs tokens on every "
            "subsequent model request in the turn.",
            path=h.source,
            evidence={"event": h.event, "chars": len(text), "signals": signals, "injects": text[:400]},
            remedy="Delete the hook and keep the rule in the instruction file.",
        )


@rule(
    "HK004",
    "Tool-scoped hook has no `if` gate",
    Severity.MINOR,
    SPEC_HOOKS,
    "hooks",
)
def hk004(inv: Inventory, cfg: Config):
    # The five events the hooks documentation lists as matching on tool name and
    # evaluating `if`. `PermissionDenied` was added to that family after this
    # rule was written, and an unscoped hook on it went unreported until the
    # spec-drift check caught the change.
    tool_events = {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionRequest",
        "PermissionDenied",
    }
    for h in inv.hooks:
        if h.event not in tool_events or h.if_rule:
            continue
        if h.matcher in (None, "", "*"):
            yield make(
                REG["HK004"],
                f"{h.event} hook has neither a specific matcher nor an `if` rule, so it "
                "runs on every tool call.",
                path=h.source,
                evidence={"event": h.event, "matcher": h.matcher, "command_excerpt": h.command[:160]},
                remedy='Add `"if": "Bash(git push:*)"`-style scoping, or a narrower matcher.',
            )


@rule(
    "HK005",
    "Hook `if` gate does not actually prevent the command from running",
    Severity.IMPORTANT,
    SPEC_HOOKS,
    "hooks",
)
def hk005(inv: Inventory, cfg: Config):
    """An `if` gate that is present but ineffective is worse than none, because the
    author believes the hook is scoped. Detected by pairing an `if` rule with a
    command whose own condition can fire independently of that rule."""
    for h in inv.hooks:
        if not h.if_rule or h.type != "command":
            continue
        m = _ECHO_GREP_V_RE.search(h.command)
        if not m:
            continue
        if _guards(h.command, m.group("var") or m.group("var2")):
            # Correctly self-guarding. HK001 already accepts this shape, and
            # disagreeing here made a correct configuration fail the health check.
            continue
        yield make(
            REG["HK005"],
            f"hook declares `if: {h.if_rule}` yet its command also fires on empty "
            "input (see HK001). If the gate ever fails open, the message appears on "
            "unrelated calls; the command must be self-guarding regardless.",
            path=h.source,
            evidence={"event": h.event, "if_rule": h.if_rule, "command_excerpt": h.command[:200]},
            remedy="Make the command exit early on empty diff so correctness does not "
            "depend on the gate alone.",
        )
