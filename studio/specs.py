"""Detect when the guidance a rule is built on has changed.

Every rule cites a published document. Those documents get revised as models
change, and a rule written against last year's advice becomes confidently wrong
without anything in the system noticing. That is the failure this module exists
to catch: not a broken rule, a *stale* one.

Two stages, deliberately separate:

* **Deterministic drift detection.** A baseline records a normalised hash of each
  cited document. Re-fetching and comparing says *whether* something changed,
  with no judgement and no model involved.
* **Optional AI review of what changed.** For a document that moved, the model is
  shown the rules that depend on it and the current text, and asked whether each
  still matches. It produces a review, and only a review - rule code is never
  edited automatically. A rule is a claim about what the guidance says, and that
  claim should change only when a person agrees it should.

Normalisation collapses whitespace before hashing, so reflowing a paragraph does
not read as a change of guidance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

BASELINE = os.path.join("canonical", "spec-baseline.json")
FETCH_TIMEOUT = 30


@dataclass
class SpecState:
    url: str
    rules: list[str] = field(default_factory=list)
    baseline_hash: str = ""
    current_hash: str = ""
    checked_at: str = ""
    status: str = "unknown"  # "unchanged" | "changed" | "new" | "unreachable"
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "rules": self.rules,
            "baseline_hash": self.baseline_hash,
            "current_hash": self.current_hash,
            "checked_at": self.checked_at,
            "status": self.status,
            "note": self.note,
        }


def _normalise(text: str) -> str:
    """Strip formatting noise so only substantive edits register as changes."""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _hash(text: str) -> str:
    return hashlib.sha256(_normalise(text).encode("utf-8")).hexdigest()[:32]


def _fetch(url: str) -> tuple[str | None, str]:
    """Fetch a doc. Tries the .md variant first: these sites serve a markdown
    version that is far more stable than the rendered HTML shell."""
    candidates = [url]
    if not url.endswith(".md"):
        candidates.insert(0, url.rstrip("/") + ".md")
    last = ""
    for candidate in candidates:
        try:
            req = urllib.request.Request(candidate, headers={"User-Agent": "agent-config-studio"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310
                if resp.status != 200:
                    last = f"HTTP {resp.status}"
                    continue
                return resp.read().decode("utf-8", "replace"), candidate
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
    return None, last


def _spec_rules() -> dict[str, list[str]]:
    """Which rules depend on which document."""
    from .rules import REGISTRY, ensure_loaded

    ensure_loaded()
    out: dict[str, list[str]] = {}
    for r in REGISTRY:
        out.setdefault(r.spec, []).append(r.code)
    return {k: sorted(v) for k, v in sorted(out.items())}


def load_baseline(repo_root: str) -> dict:
    path = os.path.join(repo_root, BASELINE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_baseline(repo_root: str, data: dict) -> str:
    path = os.path.join(repo_root, BASELINE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return path


def check(repo_root: str, *, allow_network: bool = True) -> list[SpecState]:
    """Compare every cited document against the recorded baseline."""
    baseline = load_baseline(repo_root)
    specs = baseline.get("specs", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[SpecState] = []

    for url, rules in _spec_rules().items():
        state = SpecState(url=url, rules=rules, checked_at=now)
        recorded = specs.get(url) or {}
        state.baseline_hash = recorded.get("hash", "")

        if not allow_network:
            state.status = "unknown"
            state.note = "未連線檢查"
            out.append(state)
            continue

        text, source = _fetch(url)
        if text is None:
            state.status = "unreachable"
            state.note = source
            out.append(state)
            continue

        state.current_hash = _hash(text)
        if not state.baseline_hash:
            state.status = "new"
            state.note = "尚未建立基準"
        elif state.current_hash == state.baseline_hash:
            state.status = "unchanged"
        else:
            state.status = "changed"
            state.note = f"自 {recorded.get('recorded_at', '?')} 起有變動"
        out.append(state)
    return out


def record(repo_root: str, states: list[SpecState]) -> str:
    """Accept the current content as the new baseline."""
    baseline = load_baseline(repo_root)
    specs = baseline.setdefault("specs", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in states:
        if not s.current_hash:
            continue
        specs[s.url] = {"hash": s.current_hash, "recorded_at": now, "rules": s.rules}
    baseline["updated_at"] = now
    baseline["note"] = (
        "Hashes of the documents each rule cites. `studio specs check` compares "
        "against these; a difference means the guidance moved and the rules that "
        "depend on it should be re-read. Never updated automatically - accepting "
        "a new baseline is a statement that you have reviewed the change."
    )
    return save_baseline(repo_root, baseline)


# --------------------------------------------------------------------------- #
# optional AI review of a changed document
# --------------------------------------------------------------------------- #

_REVIEW_PROMPT = """A specification this tool's rules are built on has changed.

Document: {url}

Rules that cite it, with the thresholds and intent they currently encode:
{rules}

Current text of the document:
---
{text}
---

For each rule, say whether it still matches the guidance as written today.

Reply with ONLY JSON:

{{"reviews": [
   {{"rule": "<code>",
     "verdict": "still-valid" | "needs-update" | "no-longer-supported",
     "what_changed": "<what in the document affects this rule, or 'nothing relevant'>",
     "suggested_change": "<concrete change to the rule, or empty if none>"}}
 ],
 "summary": "<two sentences on what moved in this document>"}}

Be conservative. "needs-update" means the document now states something the rule
contradicts or no longer reflects - not that the wording differs. If a threshold
the rule encodes still appears with the same value, the rule is still valid.
"""


def review_change(
    state: SpecState, repo_root: str, *, model: str | None = None, max_chars: int = 60000
) -> dict:
    """Ask what changed and whether the dependent rules still hold.

    Returns a review. It never edits a rule: a rule is a claim about what the
    guidance says, and that claim should change only when a person agrees.
    """
    from . import ai
    from .rules import find

    if not ai.available():
        return {"ok": False, "error": "找不到 claude CLI，無法做規範變動分析。"}

    text, _src = _fetch(state.url)
    if text is None:
        return {"ok": False, "error": f"抓不到文件：{state.url}"}

    lines = []
    for code in state.rules:
        try:
            r = find(code)
        except KeyError:
            continue
        lines.append(f"- {code}: {r.title}（severity={r.severity.value}）")

    answer = ai.ask(
        _REVIEW_PROMPT.format(url=state.url, rules="\n".join(lines), text=text[:max_chars]),
        repo_root=repo_root,
        label="spec-review",
        model=model,
    )
    if not answer.ok or not isinstance(answer.data, dict):
        return {"ok": False, "error": answer.error or "模型沒有回傳有效分析"}
    return {
        "ok": True,
        "url": state.url,
        "summary": answer.data.get("summary", ""),
        "reviews": answer.data.get("reviews", []),
        "cost_usd": answer.cost_usd,
    }
