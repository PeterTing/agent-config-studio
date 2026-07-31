"""Run every rule, persist the report, and track the trend over time.

There is deliberately no synthesised 0-100 score. A single number invites tuning
the number instead of the config; the verdict is a count of unwaived findings you
own, which cannot be improved except by fixing or explicitly waiving something.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .model import Finding, Inventory, Owner, Severity
from .rules import Config, REGISTRY, run_all

REPORT_DIR = "var/reports"
LATEST = "latest.json"
HISTORY = "history.json"


@dataclass
class HealthReport:
    generated_at: str
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    inventory_counts: dict[str, int] = field(default_factory=dict)
    rules_run: int = 0
    usage: dict = field(default_factory=dict)
    updates: dict = field(default_factory=dict)
    #: Spec-drift result, when the run checked it. Carried in the report so the
    #: scheduled run surfaces a moved specification without anyone pressing a
    #: button - "check periodically whether the guidance changed" is only true
    #: if something checks it periodically.
    specs: dict = field(default_factory=dict)
    #: Measurements that are context, not violations - a limit here would be
    #: unsatisfiable, so they are reported rather than graded.
    metrics: dict = field(default_factory=dict)
    scan_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["findings"] = [
            {
                **{k: v for k, v in asdict(f).items() if k not in ("severity", "owner")},
                "severity": f.severity.value,
                "owner": f.owner.value,
                "location": f.location,
                # `key` and `location` are properties, which asdict() drops. The
                # dashboard matches findings to fixes by key, so it has to ship.
                "key": f.key,
                # The category selector was built from this field and it was
                # never serialised, so the advertised filter was always empty.
                "category": _category_of(f.rule),
            }
            for f in self.findings
        ]
        return payload


def _category_of(rule_code: str) -> str:
    """Look up a rule's category for serialisation."""
    from .rules import REGISTRY, ensure_loaded

    ensure_loaded()
    for r in REGISTRY:
        if r.code == rule_code:
            return r.category
    return "other"


def _blocking(findings: list[Finding]) -> list[Finding]:
    """Findings that must be fixed or waived before the config counts as compliant."""
    return [
        f
        for f in findings
        if not f.waived
        and f.owner is Owner.LOCAL
        and f.severity in (Severity.CRITICAL, Severity.IMPORTANT)
    ]


def _metrics(inv: Inventory, cfg: Config) -> dict:
    """Descriptive measurements shown alongside the verdict."""
    from .model import Origin
    from .rules import BYTES_PER_TOKEN

    by_bucket: dict[str, dict] = {}
    total = 0
    # Each runtime preloads only what it can load, so a single combined figure is
    # a number no session ever pays. Plugins and toolkits install under
    # ~/.claude, so they are Claude's cost alone.
    per_runtime: dict[str, dict] = {
        "claude": {"skills": 0, "bytes": 0},
        "codex": {"skills": 0, "bytes": 0},
    }
    for s in inv.skills:
        if s.origin is Origin.ORPHAN_LIBRARY:
            continue
        n = len(s.name.encode("utf-8")) + len(s.description.encode("utf-8"))
        total += n
        key = s.origin.value if s.origin is not Origin.LOCAL else f"local:{s.runtime.value}"
        b = by_bucket.setdefault(key, {"skills": 0, "bytes": 0})
        b["skills"] += 1
        b["bytes"] += n

        loads_in = "claude" if s.origin is not Origin.LOCAL else s.runtime.value
        if loads_in in per_runtime:
            per_runtime[loads_in]["skills"] += 1
            per_runtime[loads_in]["bytes"] += n
    for r in per_runtime.values():
        r["est_tokens"] = r["bytes"] // BYTES_PER_TOKEN

    # Use the same classifier the rule grades on, so the reported number and the
    # verdict can never disagree.
    avoidable = 0
    avoidable_plugins: list[dict] = []
    # Availability, not truthiness: a complete index that recorded no plugin
    # calls yields an empty dict, and skipping on that reported "no avoidable
    # waste" while CB001 was flagging unused plugins from the same evidence.
    # Same bar as the rules that use this evidence. Reporting an avoidable cost
    # from partial history put an unsupported number on the dashboard while the
    # rules themselves stayed correctly silent.
    if cfg.usage_available and cfg.usage_complete:
        from .plugins import avoidable as avoidable_of, classify, reference_corpus

        corpus = reference_corpus((os.path.join(cfg.repo_root, "canonical", "*.md"),))
        rows = classify(inv, cfg.plugin_usage, corpus)
        avoidable, _skills, disable_rows = avoidable_of(rows)
        avoidable_plugins = [
            {k: r[k] for k in ("key", "skills", "metadata_bytes", "reason")} for r in disable_rows
        ]

    return {
        "preloaded_skill_metadata": {
            "per_runtime": per_runtime,
            "total_bytes": total,
            "total_est_tokens": total // BYTES_PER_TOKEN,
            "avoidable_bytes": avoidable,
            "avoidable_est_tokens": avoidable // BYTES_PER_TOKEN,
            "avoidable_plugins": avoidable_plugins,
            "by_bucket": by_bucket,
            "bytes_per_token": BYTES_PER_TOKEN,
            "note": "Total is partly irreducible; only the avoidable share is graded (CB001).",
        },
        "instruction_files": [
            {"path": i.path, "lines": i.lines, "bytes": i.bytes} for i in inv.instructions
        ],
        "toolkits": inv.toolkits,
    }


def run(
    inv: Inventory,
    cfg: Config,
    *,
    usage: dict | None = None,
    updates: dict | None = None,
    specs: dict | None = None,
) -> HealthReport:
    findings = run_all(inv, cfg)
    blocking = _blocking(findings)

    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    cat_of = {r.code: r.category for r in REGISTRY}
    for f in findings:
        if f.waived:
            continue
        by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
        cat = cat_of.get(f.rule, "other")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1

    report = HealthReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        verdict="PASS" if not blocking else "FAIL",
        findings=findings,
        counts={
            "total": len(findings),
            "blocking": len(blocking),
            "waived": sum(1 for f in findings if f.waived),
            "vendor_owned": sum(1 for f in findings if f.owner is Owner.VENDOR and not f.waived),
            "minor": sum(
                1 for f in findings if f.severity is Severity.MINOR and not f.waived
            ),
        },
        by_severity=by_severity,
        by_category=by_category,
        by_rule=dict(sorted(by_rule.items())),
        inventory_counts=inv.counts(),
        rules_run=len(REGISTRY),
        usage=usage or {},
        specs=specs or {},
        updates=updates or {},
        metrics=_metrics(inv, cfg),
        scan_errors=list(inv.scan_errors),
    )
    if not usage:
        report.notes.append(
            "No usage index supplied: plugin-usage rule CB002 was skipped rather than "
            "assuming plugins are unused."
        )
    if report.counts["vendor_owned"]:
        report.notes.append(
            f"{report.counts['vendor_owned']} finding(s) are in vendor-shipped content. "
            "Editing those files is undone by the next plugin upgrade, so they do not "
            "block the verdict; remove the plugin or record a waiver instead."
        )
    return report


def save(report: HealthReport, repo_root: str) -> str:
    out_dir = os.path.join(repo_root, REPORT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    stamp = report.generated_at.replace(":", "").replace("-", "")
    payload = report.to_dict()

    path = os.path.join(out_dir, f"health-{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, LATEST), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    hist_path = os.path.join(out_dir, HISTORY)
    history: list[dict] = []
    if os.path.isfile(hist_path):
        try:
            with open(hist_path, encoding="utf-8") as fh:
                history = json.load(fh)
        except (OSError, json.JSONDecodeError):
            history = []
    history.append(
        {
            "generated_at": report.generated_at,
            "verdict": report.verdict,
            "counts": report.counts,
            "by_severity": report.by_severity,
            "by_category": report.by_category,
            "inventory_counts": report.inventory_counts,
            "report": os.path.basename(path),
        }
    )
    history = history[-500:]
    with open(hist_path, "w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2, ensure_ascii=False)
    return path


def load_latest(repo_root: str) -> dict | None:
    path = os.path.join(repo_root, REPORT_DIR, LATEST)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def load_history(repo_root: str) -> list[dict]:
    path = os.path.join(repo_root, REPORT_DIR, HISTORY)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def format_text(report: HealthReport, *, show_waived: bool = False) -> str:
    """Human-readable report for the CLI and for launchd logs."""
    lines: list[str] = []
    c = report.counts
    lines.append(f"VERDICT: {report.verdict}")
    lines.append(
        f"  blocking={c['blocking']}  vendor={c['vendor_owned']}  "
        f"minor={c['minor']}  waived={c['waived']}  total={c['total']}"
    )
    lines.append(f"  rules run: {report.rules_run}   inventory: {report.inventory_counts}")
    meta = (report.metrics or {}).get("preloaded_skill_metadata") or {}
    if meta:
        lines.append(
            f"  preloaded skill metadata: ~{meta.get('total_est_tokens', 0):,} tokens total, "
            f"~{meta.get('avoidable_est_tokens', 0):,} avoidable"
        )
    if report.updates:
        lines.append(f"  plugin updates: {report.updates}")
    for note in report.notes:
        lines.append(f"  note: {note}")
    lines.append("")

    groups: dict[str, list[Finding]] = {}
    for f in report.findings:
        if f.waived and not show_waived:
            continue
        bucket = (
            "BLOCKING"
            if (not f.waived and f.owner is Owner.LOCAL and f.severity in (Severity.CRITICAL, Severity.IMPORTANT))
            else "WAIVED"
            if f.waived
            else "VENDOR"
            if f.owner is Owner.VENDOR
            else "MINOR"
        )
        groups.setdefault(bucket, []).append(f)

    for bucket in ("BLOCKING", "MINOR", "VENDOR", "WAIVED"):
        items = groups.get(bucket)
        if not items:
            continue
        lines.append(f"--- {bucket} ({len(items)}) ---")
        for f in items:
            loc = f.location or "-"
            lines.append(f"[{f.rule}] {f.severity.value:<9} {loc}")
            lines.append(f"    {f.detail}")
            if f.remedy:
                lines.append(f"    fix: {f.remedy}")
            if f.waiver_reason:
                lines.append(f"    waived: {f.waiver_reason}")
        lines.append("")
    return "\n".join(lines)
