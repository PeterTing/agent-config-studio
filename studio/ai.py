"""The single boundary where this tool talks to a model.

Everything else in the codebase is deterministic. This module exists because two
jobs genuinely need judgement: proposing how to split or merge content, and
reading a changed specification to say what it now requires. Both are proposals.

Three rules hold everywhere a model is involved:

1. **The model never writes.** It returns a plan. The plan is validated by code,
   turned into a change set, and applied through the same backed-up path as
   every other write. A plan that fails validation is rejected, not applied
   partially.
2. **The model is optional.** Every feature that uses it degrades to "no
   proposal available" rather than failing. The tool still scans, grades,
   graphs, fixes and updates with no model access at all.
3. **Every call is on the record.** Prompt, response, cost and duration are
   written to ``var/ai/`` so a proposal can be audited after the fact.

Access is through the ``claude`` CLI in headless mode, which keeps the
zero-dependency promise and reuses the authentication that is already set up. No
API key handling, no HTTP client, no SDK.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_TIMEOUT = 300
LOG_DIR = os.path.join("var", "ai")


@dataclass
class Answer:
    ok: bool
    text: str = ""
    data: dict | list | None = None
    error: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "model": self.model,
            "has_data": self.data is not None,
        }


@dataclass
class Usage:
    """Running totals for one command, so cost is visible rather than silent."""

    calls: int = 0
    cost_usd: float = 0.0
    entries: list[dict] = field(default_factory=list)

    def add(self, answer: Answer) -> None:
        self.calls += 1
        self.cost_usd += answer.cost_usd
        self.entries.append(answer.to_dict())


def available() -> bool:
    """Whether a model can be reached at all."""
    return shutil.which("claude") is not None


def _log(repo_root: str, prompt: str, envelope: dict, label: str) -> None:
    try:
        d = os.path.join(repo_root, LOG_DIR)
        os.makedirs(d, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        with open(os.path.join(d, f"{stamp}-{label}.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "label": label,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "prompt": prompt,
                    "result": envelope.get("result"),
                    "cost_usd": envelope.get("total_cost_usd"),
                    "duration_ms": envelope.get("duration_ms"),
                    "is_error": envelope.get("is_error"),
                },
                fh,
                indent=2,
                ensure_ascii=False,
            )
    except OSError:
        pass  # an unwritable log is not a reason to fail the call


def _extract_json(text: str):
    """Pull a JSON object or array out of a reply.

    Models wrap JSON in prose or fences often enough that requiring a bare
    document would fail on correct answers. Parsing is still strict: the
    extracted span must parse, or this returns None and the caller rejects the
    proposal rather than working with a partial read.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) > 2:
            text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    return None


def ask(
    prompt: str,
    *,
    repo_root: str,
    label: str = "ask",
    expect_json: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
) -> Answer:
    """Send one prompt. Never raises; failure comes back as ``ok=False``."""
    if not available():
        return Answer(ok=False, error="找不到 claude CLI，無法取得 AI 建議。")

    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Answer(ok=False, error=f"模型呼叫逾時（{timeout}s）")
    except OSError as exc:
        return Answer(ok=False, error=str(exc))

    if proc.returncode != 0:
        return Answer(ok=False, error=(proc.stderr or proc.stdout or "")[-400:])

    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        return Answer(ok=False, error="模型回傳無法解析的輸出")

    _log(repo_root, prompt, envelope, label)

    if envelope.get("is_error"):
        return Answer(ok=False, error=str(envelope.get("result"))[:400])

    text = envelope.get("result") or ""
    answer = Answer(
        ok=True,
        text=text,
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        duration_ms=int(envelope.get("duration_ms") or 0),
        model=next(iter((envelope.get("modelUsage") or {}).keys()), ""),
    )
    if expect_json:
        answer.data = _extract_json(text)
        if answer.data is None:
            answer.ok = False
            answer.error = "模型沒有回傳可解析的 JSON"
    return answer
