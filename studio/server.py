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
                # A fresh GET runs the same full-history scan as the POST route,
                # and Host is legitimately 127.0.0.1 for a cross-origin no-CORS
                # request - so gating only the POST left the expensive path wide
                # open. Cached reads stay ungated; they cost nothing.
                if fresh:
                    refusal = self._from_this_page()
                    if refusal:
                        return self._send(
                            {"error": refusal, "command": "studio health"},
                            HTTPStatus.FORBIDDEN,
                        )
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

    def _file_peek(self, path: str) -> dict:
        """Show a config file in the dashboard. Confined to known config roots."""
        if not path:
            return {"error": "path required"}
        full = os.path.abspath(os.path.expanduser(path))
        inv = _inventory(False)
        allowed = [os.path.abspath(r) for r in inv.roots.values() if r]
        allowed.append(os.path.abspath(self.repo_root))
        if not any(full.startswith(a + os.sep) or full == a for a in allowed):
            return {"error": "path outside the audited config roots"}
        if not os.path.isfile(full):
            return {"error": "not a file"}
        if os.path.getsize(full) > 512 * 1024:
            return {"error": "file too large to preview"}
        with open(full, encoding="utf-8", errors="replace") as fh:
            return {"path": full, "text": fh.read()}


#: Shown when a write is refused, so the dashboard can always tell you the
#: equivalent command instead of just failing.
_COMMAND_FOR = {
    "/api/actions/sync-apply": "python3 -m studio.cli sync --apply",
    "/api/actions/rollback": "python3 -m studio.cli rollback <id>",
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
