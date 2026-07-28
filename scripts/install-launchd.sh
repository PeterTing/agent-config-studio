#!/usr/bin/env bash
# Install (or remove) the daily health-check LaunchAgent.
#
# Why this is not simply "run the repo from launchd":
#   macOS TCC protects ~/Documents. A LaunchAgent has no way to show a consent
#   prompt, so reading the repo from there fails with "Operation not permitted"
#   (and Python hangs in getcwd if WorkingDirectory points inside it). Verified
#   directly with a probe agent before choosing this layout.
#
# So the agent runs from a copy under ~/Library/Application Support, which is not
# TCC-protected, and shared state lives there too. The repo's var/reports is a
# symlink to the shared location, so the dashboard - which you run from a
# terminal that does have Documents access - sees exactly the reports the
# scheduled run produced. `status` reports drift between the repo and the copy,
# so a stale copy is visible rather than silent.
#
# The scheduled command is `studio health`, which only reads config and writes a
# report. An unattended run cannot modify ~/.claude or ~/.codex.
#
#   scripts/install-launchd.sh install    # sync the copy, write the plist, load it
#   scripts/install-launchd.sh uninstall  # unload, remove the plist and the copy
#   scripts/install-launchd.sh status     # loaded? drifted? last result?
#   scripts/install-launchd.sh run-now    # trigger one run and wait for it

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.agent-config-studio.healthcheck"
SUPPORT="$HOME/Library/Application Support/agent-config-studio"
LIB="$SUPPORT/lib"
SHARED_VAR="$SUPPORT/var"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$(command -v python3)"
DOMAIN="gui/$(id -u)"

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1; }

sync_lib() {
  mkdir -p "$LIB" "$SHARED_VAR/reports"
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO/studio/" "$LIB/studio/"
  rsync -a --delete "$REPO/canonical/" "$LIB/canonical/"
  # The scheduled run needs var/ inside the copy for Config/report paths.
  ln -sfn "$SHARED_VAR" "$LIB/var"
  cat > "$SUPPORT/source.json" <<JSON
{
  "repo": "$REPO",
  "python": "$PYTHON",
  "synced_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "note": "Runtime copy for the LaunchAgent. macOS TCC blocks launchd from reading ~/Documents; run scripts/install-launchd.sh install after changing studio/ or canonical/."
}
JSON
}

link_reports() {
  # Point the repo's report directory at the shared one so interactive runs and
  # scheduled runs write the same history.
  local repo_reports="$REPO/var/reports"
  if [ -L "$repo_reports" ]; then
    return
  fi
  if [ -d "$repo_reports" ]; then
    rsync -a "$repo_reports/" "$SHARED_VAR/reports/"
    rm -rf "$repo_reports"
  fi
  mkdir -p "$REPO/var"
  ln -sfn "$SHARED_VAR/reports" "$repo_reports"
}

drift_report() {
  if [ ! -d "$LIB/studio" ]; then
    echo "  copy: absent (run install)"
    return
  fi
  local diffs
  diffs="$(diff -rq --exclude '__pycache__' --exclude '*.pyc' "$REPO/studio" "$LIB/studio" 2>&1 || true)"
  local cdiffs
  cdiffs="$(diff -rq "$REPO/canonical" "$LIB/canonical" 2>&1 || true)"
  if [ -z "$diffs" ] && [ -z "$cdiffs" ]; then
    echo "  copy: in sync with the repo"
  else
    echo "  copy: DRIFTED from the repo - re-run install"
    printf '%s\n' "$diffs" "$cdiffs" | sed '/^$/d' | sed 's/^/      /' | head -12
  fi
}

case "${1:-}" in
  install)
    command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }
    sync_lib
    link_reports

    # Prove the copy actually runs before scheduling it: a LaunchAgent that fails
    # silently every morning is worse than having no schedule.
    if ! ( cd "$LIB" && "$PYTHON" -c 'import studio.cli' ); then
      echo "the runtime copy at $LIB cannot import studio.cli" >&2
      exit 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__LIB__|$LIB|g" -e "s|__VAR__|$SHARED_VAR|g" \
      "$REPO/launchd/$LABEL.plist" > "$TARGET"
    plutil -lint "$TARGET" >/dev/null

    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$TARGET"
    echo "installed: $TARGET"
    echo "  runtime copy: $LIB"
    echo "  shared state: $SHARED_VAR  (repo var/reports -> symlink)"
    echo "  schedule: daily 09:20"
    ;;

  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    rm -rf "$LIB"
    echo "removed the agent and the runtime copy."
    echo "kept shared reports at $SHARED_VAR/reports"
    ;;

  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo "loaded:"
      launchctl print "$DOMAIN/$LABEL" 2>/dev/null |
        grep -E '^\s+(state|last exit code|runs) = ' | sed 's/^/  /'
    else
      echo "not loaded"
    fi
    drift_report
    if [ -f "$SHARED_VAR/reports/latest.json" ]; then
      "$PYTHON" - "$SHARED_VAR/reports/latest.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    d = json.load(fh)
print(f"  last report: {d.get('generated_at')}  verdict={d.get('verdict')}  "
      f"blocking={d.get('counts', {}).get('blocking')}")
PY
    fi
    ;;

  run-now)
    launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1 || {
      echo "agent is not loaded; running the repo copy directly"
      ( cd "$REPO" && "$PYTHON" -m studio.cli health --with-updates )
      exit 0
    }
    echo "triggered; waiting for it to finish..."
    for _ in $(seq 1 60); do
      state="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E '^\s+state = ' | head -1 | awk '{print $3}')"
      [ "$state" = "running" ] || break
      sleep 2
    done
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E 'last exit code' | sed 's/^/  /'
    echo "  log: $SHARED_VAR/reports/launchd.out.log"
    tail -8 "$SHARED_VAR/reports/launchd.out.log" 2>/dev/null | sed 's/^/  /'
    ;;

  *) usage ;;
esac
