"""Loopback dashboard, standard library only.

Read-only by default. Scanning, grading, graphing, update checks and sync
previews never write anything.

Write actions - applying a sync, rolling back - are off unless you start the
server with ``--allow-actions``. Binding to loopback is not by itself a defence:
any web page you have open can POST to 127.0.0.1. Two things gate the write
endpoints instead:

* **Origin check.** Browsers always send ``Origin`` on a cross-origin POST, and
  it cannot be forged by page JavaScript. Anything that is not this server is
  rejected.
* **Session token.** Issued by ``GET /api/session`` and required on every write.
  A cross-origin page cannot read that response, because CORS blocks it.

Every write still goes through ``studio.patch``, so it is backed up and
reversible regardless of how it was triggered.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (
    canonical,
    fixes as fixes_mod,
    graph as graph_mod,
    health,
    patch,
    scan,
    updates as updates_mod,
    upgrade as upgrade_mod,
    usage,
)
from .model import to_json
from .rules import REGISTRY, Config, ensure_loaded

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, object] = {}

#: Per-process token gating write actions. Regenerated on every start, so a token
#: from an old session is useless.
_SESSION_TOKEN = secrets.token_urlsafe(32)

def _usage_cache() -> str:
    """Where the per-file usage cache lives."""
    return os.path.join(Handler.repo_root, "var", "usage-cache.json")


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def select_update_targets(todo: list[dict], target: str) -> list[dict]:
    """Pick the update plans a dashboard target refers to.

    An exact match wins first, because plugin keys already contain '@'
    (`superpowers@superpowers-marketplace`) and reading every '@' as a root
    separator broke plugin updates outright. Only when nothing matches exactly is
    the target interpreted as the `name@root` form, which is what keeps a toolkit
    installed under both skill roots addressable as two separate things: matching
    on name alone upgraded both checkouts from one confirmation while the page
    reported a single result.

    Module-level so the selection is tested through the code that actually runs,
    rather than through a copy of it in the test.
    """
    if not target:
        return todo
    exact = [t for t in todo if t["target"] == target]
    if exact:
        return exact
    if "@" in target:
        name, _, root = target.partition("@")
        return [t for t in todo if t["target"] == name and t.get("root", "") == root]
    return []


def _same_loopback(origin: str, expected: str) -> bool:
    """Whether `origin` is this server, under any loopback name.

    `_host_allowed` accepts both `localhost` and `127.0.0.1`, so opening the
    dashboard at either address works and /api/session hands out a token - but a
    strict string comparison here then rejected every action from whichever name
    was not the configured one. The two checks have to agree on what "this
    server" means.
    """
    from urllib.parse import urlsplit

    o, e = urlsplit(origin), urlsplit(expected)
    if o.scheme != e.scheme or (o.port or 80) != (e.port or 80):
        return False
    aliases = {"127.0.0.1", "localhost", "::1", "[::1]"}
    return o.hostname in aliases and e.hostname in aliases


def _fresh_inventory():
    inv = scan.scan()
    with _CACHE_LOCK:
        _CACHE["inventory"] = inv
    return inv


def _inventory(force: bool = False):
    with _CACHE_LOCK:
        cached = _CACHE.get("inventory")
    if cached is not None and not force:
        return cached
    return _fresh_inventory()


class Handler(SimpleHTTPRequestHandler):
    repo_root = ""
    web_root = ""
    #: Set from the CLI. False means every write endpoint returns 405.
    allow_actions = False
    origin = ""

    # -- plumbing --------------------------------------------------------- #

    def log_message(self, fmt: str, *args) -> None:  # quieter than the default
        if self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    def _send(self, payload, status: int = HTTPStatus.OK, ctype: str = "application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Local-only tool; block framing and sniffing regardless.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, rel: str):
        safe = os.path.normpath(rel).lstrip("/")
        full = os.path.join(self.web_root, safe)
        if not os.path.abspath(full).startswith(os.path.abspath(self.web_root)):
            self._send({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if not os.path.isfile(full):
            self._send({"error": f"not found: {safe}"}, HTTPStatus.NOT_FOUND)
            return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as fh:
            self._send(fh.read(), ctype=_CONTENT_TYPES.get(ext, "application/octet-stream"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _host_allowed(self) -> bool:
        """Reject a Host header that is not this server.

        Binding to 127.0.0.1 does not stop DNS rebinding: a remote page can
        resolve its own domain to the loopback address and then read GET
        endpoints as same-origin, with no token involved. Checking Host is what
        makes the address binding actually mean something.
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        name = host.rsplit(":", 1)[0].strip("[]")
        return name in ("127.0.0.1", "localhost", "::1", self.server.server_address[0])

    def _from_this_page(self) -> str | None:
        """Reject callers that are not this dashboard, else None.

        Binding to loopback is not a defence on its own: any page in any tab can
        POST to 127.0.0.1. Browsers always send Origin on a cross-origin POST and
        page JavaScript cannot forge it, and a cross-origin page cannot read
        /api/session because CORS blocks the response - so it cannot obtain the
        token either.

        Separate from :meth:`_authorised` because two different questions are
        being asked. Writes additionally require --allow-actions; an expensive
        read does not, but still must not be drivable from another origin.
        """
        origin = self.headers.get("Origin")
        if origin and not _same_loopback(origin, self.origin):
            return f"refusing a request from another origin: {origin}"
        token = self.headers.get("X-Studio-Token")
        if not token or not secrets.compare_digest(token, _SESSION_TOKEN):
            return "missing or stale session token; reload the page"
        return None

    def _authorised(self) -> str | None:
        """Return an error string when a write must be refused, else None."""
        if not self.allow_actions:
            return (
                "write actions are disabled. Restart with "
                "`python3 -m studio.cli serve --allow-actions`, or run the CLI command shown."
            )
        refusal = self._from_this_page()
        if refusal:
            return refusal
        return None

    # -- routes ----------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._host_allowed():
            return self._send({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        fresh = query.get("fresh", ["0"])[0] not in ("0", "", "false")

        # `fresh=1` means "rescan", on whichever route it appears - summary,
        # inventory, graph and updates all accept it, and updates additionally
        # goes to the network. Gating each expensive route by hand left those
        # four open, so the rule lives here instead: any read that does work is
        # gated, any read served from cache is not.
        if fresh and route.startswith("/api/"):
            refusal = self._from_this_page()
            if refusal:
                return self._send(
                    {"error": refusal, "command": "studio health"}, HTTPStatus.FORBIDDEN
                )

        try:
            if route in ("/", "/index.html"):
                return self._file("index.html")
            if route.startswith("/static/"):
                return self._file(route[len("/static/") :])

            if route == "/api/summary":
                return self._send(self._summary(fresh))
            if route == "/api/inventory":
                return self._send(json.loads(to_json(_inventory(fresh))))
            if route == "/api/graph":
                expand = query.get("expand", ["0"])[0] not in ("0", "", "false")
                cfg = Config.load(self.repo_root)
                return self._send(graph_mod.build(_inventory(fresh), cfg, include_plugin_skills=expand))
            if route == "/api/health":
                # `fresh` is gated centrally above, for every route that takes it.
                return self._send(self._health(fresh))
            if route == "/api/history":
                return self._send(health.load_history(self.repo_root))
            if route == "/api/rules":
                ensure_loaded()
                return self._send(
                    [
                        {
                            "code": r.code,
                            "title": r.title,
                            "severity": r.severity.value,
                            "category": r.category,
                            "spec": r.spec,
                        }
                        for r in sorted(REGISTRY, key=lambda x: x.code)
                    ]
                )
            if route == "/api/updates":
                return self._send(self._updates(fresh))
            if route == "/api/sync-preview":
                return self._send(self._sync_preview())
            if route == "/api/specs":
                # Network-bound and re-fetches every cited document, so it is
                # gated like the other expensive reads.
                refusal = self._from_this_page()
                if refusal:
                    return self._send(
                        {"error": refusal, "command": "studio specs"}, HTTPStatus.FORBIDDEN
                    )
                return self._send(self._specs())
            if route == "/api/schedule":
                return self._send(self._schedule())
            if route == "/api/backups":
                return self._send(patch.list_backups(self.repo_root))
            if route == "/api/session":
                return self._send(
                    {
                        "token": _SESSION_TOKEN,
                        "allow_actions": self.allow_actions,
                        "origin": self.origin,
                    }
                )
            if route == "/api/fixes":
                # Rebuilds the usage index and walks local history, so it costs
                # what the gated health route costs. Leaving it open let any page
                # start unbounded concurrent scans through the cheap-looking door.
                refusal = self._from_this_page()
                if refusal:
                    return self._send(
                        {"error": refusal, "command": "studio fix --list"},
                        HTTPStatus.FORBIDDEN,
                    )
                return self._send(self._fixes())
            if route == "/api/file":
                # Hands out file contents, so it needs the same gate as every
                # other content endpoint. It was the only one without it.
                refusal = self._from_this_page()
                if refusal:
                    return self._send({"error": refusal}, HTTPStatus.FORBIDDEN)
                return self._send(self._file_peek(query.get("path", [""])[0]))

            self._send({"error": f"no route {route}"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # never take the dashboard down on one bad request
            self._send(
                {"error": f"{type(exc).__name__}: {exc}", "route": route},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            return self._send({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            # Gated even though it never touches agent configuration. The
            # original reasoning only asked "does this modify anything?", which
            # missed cost: a full run reads the entire transcript history - tens
            # of gigabytes - so an ungated POST lets any page in any tab spawn
            # unbounded concurrent scans.
            if route == "/api/health/run":
                refusal = self._from_this_page()
                if refusal:
                    return self._send(
                        {"error": refusal, "command": "studio health"},
                        HTTPStatus.FORBIDDEN,
                    )
                return self._send(self._health(True))

            if route in (
                "/api/actions/sync-apply",
                "/api/actions/rollback",
                "/api/actions/fix",
                "/api/actions/update",
                "/api/actions/specs-accept",
                "/api/actions/specs-review",
                "/api/actions/waive",
                "/api/actions/quarantine",
                "/api/actions/consolidate",
                "/api/actions/edit",
            ):
                refusal = self._authorised()
                if refusal:
                    return self._send(
                        {"error": refusal, "command": _COMMAND_FOR.get(route, "")},
                        HTTPStatus.FORBIDDEN,
                    )
                if route == "/api/actions/sync-apply":
                    return self._send(self._sync_apply())
                if route == "/api/actions/fix":
                    body = self._body()
                    return self._send(self._fix(body.get("keys") or [], bool(body.get("dry_run"))))
                if route == "/api/actions/update":
                    return self._send(self._update(self._body().get("target") or ""))
                if route == "/api/actions/specs-accept":
                    return self._send(self._specs_accept())
                if route == "/api/actions/specs-review":
                    return self._send(self._specs_review(self._body().get("urls") or []))
                if route == "/api/actions/waive":
                    b = self._body()
                    return self._send(
                        self._waive(
                            b.get("rule") or "",
                            b.get("path") or "",
                            b.get("reason") or "",
                            bool(b.get("remove")),
                        )
                    )
                if route == "/api/actions/edit":
                    b = self._body()
                    return self._send(
                        self._edit(
                            b.get("path") or "",
                            b.get("text") if b.get("text") is not None else "",
                            create=bool(b.get("create")),
                        )
                    )
                if route == "/api/actions/quarantine":
                    return self._send(self._quarantine(self._body().get("path") or ""))
                if route == "/api/actions/consolidate":
                    body = self._body()
                    return self._send(
                        self._consolidate(body.get("keys") or [], bool(body.get("apply")))
                    )
                return self._send(self._rollback(self._body().get("id", "")))

            self._send({"error": f"no route {route}"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send(
                {"error": f"{type(exc).__name__}: {exc}", "route": route},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    # -- fixes -------------------------------------------------------------- #

    def _report_findings(self):
        """Rebuild findings so a fix acts on current state, not a stale report."""
        inv = _inventory(False)
        cfg = Config.load(self.repo_root)
        idx = usage.build(cache_path=_usage_cache())
        cfg.plugin_usage = usage.plugin_usage(idx, inv)
        cfg.skill_usage = dict(idx.tokens)
        cfg.usage_available = idx.available
        _s = idx.summary()
        cfg.usage_complete = bool(
            idx.available
            and not _s.get('truncated')
            and not _s.get('files_skipped')
            and (_s.get('file_coverage_pct') or 0) >= 99.9
        )
        report = health.run(inv, cfg, usage=idx.summary())
        return inv, cfg, report

    def _fixes(self) -> dict:
        _inv, _cfg, report = self._report_findings()
        info = fixes_mod.available(report.findings)
        auto = fixes_mod.bulk_keys(info)
        return {
            "fixes": info,
            "auto_fixable": auto,
            "auto_fixable_count": len(auto),
            "individual_count": sum(
                1 for v in info.values() if v.get("fixable") and not v.get("bulk")
            ),
            "allow_actions": self.allow_actions,
        }

    def _fix(self, keys: list[str], dry_run: bool) -> dict:
        inv, cfg, report = self._report_findings()
        if not keys:
            # No selection means "everything that can be fixed automatically".
            keys = fixes_mod.bulk_keys(fixes_mod.available(report.findings))
        cs, applied, skipped = fixes_mod.build_change_set(
            keys, report.findings, inv, cfg, self.repo_root
        )
        if not cs.has_work():
            return {"applied": 0, "message": "沒有可套用的變更", "skipped": skipped}
        if dry_run:
            return {
                "dry_run": True,
                "would_apply": applied,
                "skipped": skipped,
                "changes": cs.manifest()["changes"],
                "remove_dirs": cs.remove_dirs,
                "diff": cs.diff(),
            }
        patch.save(cs, self.repo_root)
        result = patch.apply(cs, self.repo_root)
        with _CACHE_LOCK:
            _CACHE.pop("inventory", None)
        return {
            "applied": result["applied"],
            "backup": os.path.basename(result["backup"] or ""),
            "fixed": applied,
            "skipped": skipped,
            "changes": result["changes"],
            "removed_dirs": result.get("removed_dirs", []),
        }

    def _update(self, target: str) -> dict:
        """Run the official updater for one target, or for everything."""
        inv = _inventory(True)
        updates_mod.check(inv, allow_network=True)
        todo = upgrade_mod.plan(inv)
        if target:
            # Match on root when the caller supplies one. Two toolkits can share
            # a name across the Claude and Codex skill roots, and selecting by
            # name alone upgraded both from a single confirmation while the page
            # showed only one result.
            todo = select_update_targets(todo, target)
        if not todo:
            return {"results": [], "message": "沒有偵測到可用更新"}

        results = []
        for item in todo:
            if not item["automatic"]:
                results.append(
                    {
                        "target": item["target"],
                        "kind": item["kind"],
                        "ok": False,
                        "message": "沒有自動路徑，請手動執行：" + item["method"],
                    }
                )
                continue
            if item["kind"] == "plugin":
                r = upgrade_mod.update_plugin(item["target"])
            else:
                r = upgrade_mod.update_toolkit(item["root"], item["target"])
            results.append(r.to_dict())
        with _CACHE_LOCK:
            _CACHE.pop("inventory", None)
            _CACHE.pop("updates", None)
        return {"results": results}

    # -- write actions ----------------------------------------------------- #

    def _sync_apply(self) -> dict:
        cfg = Config.load(self.repo_root)
        changes: list[patch.Change] = []
        for target, text in canonical.targets(cfg):
            changes.append(patch.Change(path=target, new_text=text, reason="canonical render"))
        for name, src, others in canonical.mirror_groups(cfg):
            if not os.path.isfile(src):
                continue
            with open(src, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for dest in others:
                changes.append(patch.Change(path=dest, new_text=text, reason=f"mirror {name}"))
        cs = patch.ChangeSet(
            name="sync",
            description="Render instruction files and re-sync mirrors (from the dashboard).",
            changes=changes,
        )
        if not cs.effective():
            return {"applied": 0, "message": "already in sync"}
        patch.save(cs, self.repo_root)
        result = patch.apply(cs, self.repo_root)
        with _CACHE_LOCK:
            _CACHE.pop("inventory", None)
        return {
            "applied": result["applied"],
            "backup": os.path.basename(result["backup"] or ""),
            "changes": result["changes"],
        }

    def _rollback(self, backup_id: str) -> dict:
        if not backup_id:
            return {"error": "a backup id is required"}
        result = patch.rollback(self.repo_root, backup_id)
        with _CACHE_LOCK:
            _CACHE.pop("inventory", None)
        return result

    # -- payload builders -------------------------------------------------- #

    def _health(self, fresh: bool) -> dict:
        if not fresh:
            cached = health.load_latest(self.repo_root)
            if cached:
                return cached
        inv = _inventory(fresh)
        cfg = Config.load(self.repo_root)
        idx = usage.build(cache_path=_usage_cache())
        cfg.plugin_usage = usage.plugin_usage(idx, inv)
        cfg.skill_usage = dict(idx.tokens)
        cfg.usage_available = idx.available
        _s = idx.summary()
        cfg.usage_complete = bool(
            idx.available
            and not _s.get('truncated')
            and not _s.get('files_skipped')
            and (_s.get('file_coverage_pct') or 0) >= 99.9
        )
        report = health.run(inv, cfg, usage=idx.summary())
        health.save(report, self.repo_root)
        return report.to_dict()

    def _updates(self, fresh: bool) -> dict:
        with _CACHE_LOCK:
            cached = _CACHE.get("updates")
        if cached is not None and not fresh:
            return cached  # type: ignore[return-value]
        inv = _inventory(False)
        rep = updates_mod.check(inv)
        payload = {
            "summary": rep.summary(),
            "updates": rep.updates,
            "up_to_date": rep.up_to_date,
            "unknown": rep.unknown,
            "errors": rep.errors,
            "toolkits": rep.toolkits,
            "plan": upgrade_mod.plan(inv),
            "plugins": [
                {
                    "key": p.key,
                    "runtime": p.runtime.value,
                    "enabled": p.enabled,
                    "marketplace": p.marketplace,
                    "skill_count": p.skill_count,
                    "source_type": p.source_type,
                    "update_available": p.update_available,
                    "note": p.update_note,
                }
                for p in inv.plugins
            ],
        }
        with _CACHE_LOCK:
            _CACHE["updates"] = payload
        return payload

    def _specs(self) -> dict:
        """Whether any document the rules cite has changed since the baseline."""
        from . import specs as specs_mod

        cfg = Config.load(self.repo_root)
        states = specs_mod.check(self.repo_root, allow_network=True)
        return {
            "specs": [s.to_dict() for s in states],
            "changed": [s.url for s in states if s.status == "changed"],
            "new": [s.url for s in states if s.status == "new"],
            "unreachable": [s.url for s in states if s.status == "unreachable"],
            "governance_error": cfg.governance_error,
        }

    def _specs_accept(self) -> dict:
        """Record the current documents as reviewed.

        Never automatic: a rule is a claim about what the guidance says, and that
        claim should only change when a person agrees it should.
        """
        from . import specs as specs_mod

        states = specs_mod.check(self.repo_root, allow_network=True)
        path = specs_mod.record(self.repo_root, states)
        return {"baseline": path, "recorded": [s.url for s in states if s.current_hash]}

    def _waive(self, rule: str, path: str, reason: str, remove: bool = False) -> dict:
        """Record - or withdraw - a decision not to fix a finding.

        A waiver is a decision on the record, not a mute button, so a reason is
        required. Written through the same backed-up change set as every other
        write, and the path is stored in `~` form so the file stays portable.
        """
        if not rule or not path:
            return {"error": "rule and path are required"}
        if not remove and not reason.strip():
            return {"error": "豁免一定要寫理由 —— 沒有理由的豁免就只是把它靜音。"}

        gov = os.path.join(self.repo_root, "canonical", "governance.json")
        try:
            with open(gov, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            return {"error": f"讀不到 governance.json：{exc}"}
        if not isinstance(data, dict):
            return {"error": "governance.json 不是一個物件，先修好它再試"}

        home = os.path.expanduser("~")
        stored = path.replace(home, "~", 1) if path.startswith(home) else path
        waivers = [w for w in (data.get("waivers") or []) if isinstance(w, dict)]
        waivers = [w for w in waivers if not (w.get("rule") == rule and w.get("path") == stored)]
        if not remove:
            waivers.append({"rule": rule, "path": stored, "reason": reason.strip()})
        data["waivers"] = waivers

        cs = patch.ChangeSet(
            name="waive" if not remove else "unwaive",
            description=(
                f"Record a waiver for {rule} on {stored}."
                if not remove
                else f"Withdraw the waiver for {rule} on {stored}."
            ),
            changes=[
                patch.Change(
                    path=gov,
                    new_text=json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    reason="waiver change from the dashboard",
                )
            ],
        )
        result = patch.apply(cs, self.repo_root)
        return {"waivers": len(waivers), "backup": result["backup"], "removed": remove}

    def _quarantine(self, path: str) -> dict:
        """Move one config file into the repo's quarantine.

        Copy first, then remove, both inside one backed-up change set - so the
        file exists in two places before it exists in neither, and rollback puts
        it back exactly.
        """
        if not path:
            return {"error": "path required"}
        full = os.path.abspath(os.path.expanduser(path))
        inv = _inventory(False)
        allowed = [os.path.abspath(r) for r in inv.roots.values() if r]
        if not any(full.startswith(a + os.sep) for a in allowed):
            return {"error": "path outside the audited config roots"}
        if not os.path.isfile(full):
            return {"error": "not a file"}
        try:
            text = open(full, encoding="utf-8").read()
        except UnicodeDecodeError:
            return {"error": "不是 UTF-8 文字檔，變更集載不了，請自己搬"}

        dest = os.path.join(self.repo_root, "var", "quarantine", full.lstrip("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        cs = patch.ChangeSet(
            name="quarantine-file",
            description=f"Move {os.path.basename(full)} out of the live config tree.",
            changes=[
                patch.Change(path=dest, new_text=text, action="create", reason="quarantined copy"),
                patch.Change(path=full, new_text="", action="delete", reason="quarantined"),
            ],
        )
        result = patch.apply(cs, self.repo_root)
        _fresh_inventory()
        return {"quarantined": full, "copy": dest, "backup": result["backup"]}

    def _writable_reason(self, full: str, requested: str, inv) -> str | None:
        """Why this path must not be written from the dashboard, or None.

        Three separate refusals, because each has a different right answer and
        saying "cannot edit" for all of them would be useless:

        * outside the audited roots - nothing here has any business there;
        * vendor-owned - a plugin or toolkit upgrade overwrites the edit, so
          saving would look like it worked and quietly revert later;
        * generated from ``canonical/`` - editing the rendered file is the exact
          drift MR003 exists to catch, and the fix is to edit the source.
        """
        allowed = [os.path.realpath(r) for r in inv.roots.values() if r]
        if not any(full.startswith(a + os.sep) for a in allowed):
            return "path outside the audited config roots"
        if self._is_credential(full):
            return "credential file — this tool never reads or writes secrets"

        # Vendor ownership belongs to the *install*, not to the one file the
        # scanner happened to index. A plugin's skill directory also holds
        # reference material and scripts the skill loads, and those are
        # overwritten by the same upgrade - so the check is "under a vendor
        # skill's directory", not "is that exact SKILL.md".
        for s in inv.skills:
            if s.origin.value not in ("plugin", "toolkit"):
                continue
            owned = os.path.dirname(os.path.realpath(s.path))
            if full == os.path.realpath(s.path) or full.startswith(owned + os.sep):
                where = (
                    f"停用 plugin {s.plugin}" if s.origin.value == "plugin" else "用工具組自己的指令"
                )
                return (
                    f"這個檔案是 {s.origin.value} 帶進來的，改了會被下次升級覆蓋。"
                    f"要移除請{where}。"
                )

        cfg = Config.load(self.repo_root)
        # The same question the rules ask, asked the same way: governance.json
        # already declares which files an external tool re-copies on upgrade.
        # Declarations are written the way a person writes a path -
        # `~/.codex/skills/x/SKILL.md` - so they are matched against every
        # spelling of this file, not only the fully resolved one. On macOS
        # /var resolves to /private/var, and matching realpath alone silently
        # stopped recognising declared files.
        home = os.path.expanduser("~")
        spellings = {full, requested}
        spellings |= {s.replace(home, "~", 1) for s in (full, requested) if s.startswith(home)}
        declared = next(
            (r for r in (cfg.vendored_reason(s) for s in sorted(spellings)) if r), None
        )
        if declared:
            return f"這個檔案由外部工具管理，改了會被覆蓋：{declared}"
        for spec in cfg.generated:
            if os.path.realpath(os.path.expanduser(spec.get("target", ""))) == full:
                sources = "、".join(spec.get("sources", [])) or "canonical/"
                return (
                    f"這個檔案是從 {sources} 產生的。直接改會被 MR003 抓到漂移，"
                    "請改來源再跑 `studio sync --apply`。"
                )
        return None

    def _edit(self, path: str, text: str, *, create: bool) -> dict:
        """Write a config file through the same backed-up change set as every
        other write, so an edit made here is reviewable and reversible exactly
        like one made by a fix."""
        if not path:
            return {"error": "path required"}
        if not isinstance(text, str):
            return {"error": "text must be a string"}
        requested = os.path.abspath(os.path.expanduser(path))
        full = os.path.realpath(requested)
        inv = _inventory(False)
        refusal = self._writable_reason(full, requested, inv)
        if refusal:
            return {"error": refusal}

        if os.path.islink(requested):
            # `full` is the resolved target and passed the checks above, but the
            # write goes through the link - and for a create the link may be
            # dangling, so the file lands wherever it points. Editing a config
            # file through a link is never what someone means here.
            return {"error": "這是一個 symlink，請直接編輯它指向的檔案"}

        exists = os.path.isfile(full)
        if create and exists:
            return {"error": "檔案已存在，改用編輯"}
        if not create and not exists:
            return {"error": "not a file"}
        if exists:
            # The preview decodes with errors="replace", so a file that is not
            # valid UTF-8 shows as U+FFFD and saving it back would write those
            # replacements over the original bytes. Refuse rather than silently
            # rewrite bytes the editor never actually showed.
            try:
                with open(full, "rb") as fh:
                    fh.read().decode("utf-8")
            except UnicodeDecodeError:
                return {"error": "這個檔案不是有效的 UTF-8，編輯會破壞原始位元組，請用其他編輯器"}
        if not exists and not os.path.isdir(os.path.dirname(full)):
            os.makedirs(os.path.dirname(full), exist_ok=True)

        cs = patch.ChangeSet(
            name="edit-file" if exists else "create-file",
            description=f"{'Edit' if exists else 'Create'} {os.path.basename(full)} from the dashboard.",
            changes=[
                patch.Change(
                    path=full,
                    new_text=text,
                    action="modify" if exists else "create",
                    reason="edited in the dashboard",
                )
            ],
        )
        if not cs.has_work():
            return {"path": full, "unchanged": True}
        result = patch.apply(cs, self.repo_root)
        _fresh_inventory()
        return {"path": full, "backup": result["backup"], "created": not exists}

    def _specs_review(self, urls: list[str]) -> dict:
        """Ask what changed and whether the dependent rules still hold.

        The only place a model touches the rule set, and it produces a review
        and nothing else: a rule is a claim about what the guidance says, and
        that claim changes only when a person agrees it should.
        """
        from . import specs as specs_mod

        states = {s.url: s for s in specs_mod.check(self.repo_root, allow_network=True)}
        reviews = []
        for url in urls[:4]:
            state = states.get(url)
            if state is None:
                continue
            reviews.append(specs_mod.review_change(state, self.repo_root))
        return {"reviews": reviews}

    def _schedule(self) -> dict:
        """Status of the scheduled daily check, if one is installed."""
        import subprocess

        script = os.path.join(self.repo_root, "scripts", "install-launchd.sh")
        if not os.path.isfile(script):
            return {"available": False, "reason": "installer not present"}
        try:
            p = subprocess.run(
                [script, "status"], capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "reason": str(exc)}
        out = p.stdout or ""
        # Drift gets its own field rather than living inside the status blob.
        # The scheduled job runs from a copy of the package, so after any edit
        # here it keeps checking with old code - it reported five blocking
        # findings for two days after they were fixed - and the only warning was
        # a line inside a collapsed section nobody opens.
        return {
            "available": True,
            "installed": "not loaded" not in out.lower() and p.returncode == 0,
            "drifted": "DRIFTED" in out,
            "output": out.strip()[:2000],
            "install_command": "scripts/install-launchd.sh install",
        }

    def _consolidate(self, keys: list[str], apply: bool) -> dict:
        """AI-planned consolidation. The model proposes; code validates."""
        from . import ai, consolidate as consolidate_mod

        if not ai.available():
            return {"error": "找不到 claude CLI，無法取得 AI 建議。", "proposals": []}

        _inv, _cfg, report = self._report_findings()
        wanted = set(keys)
        targets = [
            f
            for f in report.findings
            if consolidate_mod.can_propose(f.rule) and (not wanted or f.key in wanted)
        ]
        proposals, applied, cost = [], 0, 0.0
        for finding in targets[:5]:
            p = consolidate_mod.propose(finding, self.repo_root)
            if p is None:
                continue
            cost += getattr(p, "cost_usd", 0.0) or 0.0
            row = {
                "rule": finding.rule,
                "path": finding.path,
                "key": finding.key,
                "ok": p.ok,
                "summary": p.summary,
                "rejected_because": p.rejected_because,
                "diff": p.change_set.diff() if (p.ok and p.change_set) else "",
            }
            if apply and p.ok and p.change_set:
                result = patch.apply(p.change_set, self.repo_root)
                row["applied"] = True
                row["backup"] = result["backup"]
                applied += 1
            proposals.append(row)
        return {"proposals": proposals, "applied": applied, "cost_usd": round(cost, 4)}

    def _sync_preview(self) -> dict:
        cfg = Config.load(self.repo_root)
        changes: list[patch.Change] = []
        errors: list[str] = []
        try:
            for target, text in canonical.targets(cfg):
                changes.append(patch.Change(path=target, new_text=text, reason="canonical render"))
        except FileNotFoundError as exc:
            errors.append(f"missing canonical source: {exc}")
        for name, src, others in canonical.mirror_groups(cfg):
            if not os.path.isfile(src):
                errors.append(f"mirror {name}: source missing {src}")
                continue
            with open(src, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for dest in others:
                changes.append(patch.Change(path=dest, new_text=text, reason=f"mirror {name}"))
        cs = patch.ChangeSet(name="sync-preview", changes=changes)
        eff = cs.effective()
        return {
            "in_sync": not eff and not errors,
            "errors": errors,
            "pending": cs.manifest()["changes"],
            "diff": cs.diff(),
            "apply_command": "python3 -m studio.cli sync --apply",
            # What is actually being kept in sync. Without this the page could
            # only hardcode a description, and it did - naming two files while
            # six were checked, so a reader could hand-edit a generated skill
            # believing sync did not cover it.
            "targets": [os.path.basename(c.path) for c in changes],
        }

    def _summary(self, fresh: bool) -> dict:
        inv = _inventory(fresh)
        latest = health.load_latest(self.repo_root)
        hist = health.load_history(self.repo_root)
        return {
            "roots": inv.roots,
            "scanned_at": inv.scanned_at,
            "inventory": inv.counts(),
            "verdict": (latest or {}).get("verdict"),
            "counts": (latest or {}).get("counts", {}),
            "by_category": (latest or {}).get("by_category", {}),
            "by_severity": (latest or {}).get("by_severity", {}),
            "last_report_at": (latest or {}).get("generated_at"),
            "history_points": len(hist),
            "rules_available": (ensure_loaded(), len(REGISTRY))[1],
            "scan_errors": inv.scan_errors[:20],
        }

    #: Files that hold credentials rather than configuration. Refused whatever
    #: the gate says: a config auditor never needs to display a token to do its
    #: job, and `~/.codex/auth.json` holds a long-lived OAuth refresh token that
    #: the OS protects at 0600.
    _CREDENTIAL_NAMES = frozenset(
        {
            "auth.json",
            ".credentials.json",
            "credentials.json",
            ".netrc",
            "id_rsa",
            "id_ed25519",
            ".env",
            ".env.local",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
        }
    )
    _CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")

    @classmethod
    def _is_credential(cls, full: str) -> bool:
        base = os.path.basename(full).lower()
        if base in cls._CREDENTIAL_NAMES or base.startswith(("auth.", ".env.")):
            return True
        return base.endswith(cls._CREDENTIAL_SUFFIXES)

    def _file_peek(self, path: str) -> dict:
        """Show a config file in the dashboard. Confined to known config roots."""
        if not path:
            return {"error": "path required"}
        # Resolve symlinks before deciding anything. Both the confinement check
        # and the credential check look at a path, and `abspath` leaves links
        # intact - so a file named `notes.md` inside an audited root, pointing at
        # `~/.codex/auth.json`, passed both and had its target read out. Judge the
        # file that will actually be opened, and refuse if either spelling of it
        # looks like a credential.
        requested = os.path.abspath(os.path.expanduser(path))
        full = os.path.realpath(requested)
        inv = _inventory(False)
        allowed = [os.path.realpath(r) for r in inv.roots.values() if r]
        allowed.append(os.path.realpath(self.repo_root))
        if not any(full.startswith(a + os.sep) or full == a for a in allowed):
            return {"error": "path outside the audited config roots"}
        if not os.path.isfile(full):
            return {"error": "not a file"}
        if self._is_credential(full) or self._is_credential(requested):
            return {"error": "credential file — not shown. This tool never displays secrets."}
        if os.path.getsize(full) > 512 * 1024:
            return {"error": "file too large to preview"}
        with open(full, encoding="utf-8", errors="replace") as fh:
            return {"path": full, "text": fh.read()}


#: Shown when a write is refused, so the dashboard can always tell you the
#: equivalent command instead of just failing.
_COMMAND_FOR = {
    "/api/actions/sync-apply": "python3 -m studio.cli sync --apply",
    "/api/actions/rollback": "python3 -m studio.cli rollback <id>",
    "/api/actions/edit": "直接用編輯器改該檔案",
    "/api/actions/fix": "python3 -m studio.cli fix --apply",
    "/api/actions/update": "python3 -m studio.cli update --apply",
}


def serve(
    repo_root: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
    allow_actions: bool = False,
) -> None:
    Handler.repo_root = repo_root
    Handler.web_root = os.path.join(repo_root, "web")
    Handler.allow_actions = allow_actions
    Handler.origin = f"http://{host}:{port}"
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            f"refusing to bind {host}: this dashboard exposes your local agent "
            "configuration and is loopback-only by design."
        )
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"agent-config-studio -> {url}   (Ctrl-C to stop)")
    print(
        "  write actions: ENABLED from the dashboard"
        if allow_actions
        else "  write actions: disabled (read-only). Add --allow-actions to enable them."
    )
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
