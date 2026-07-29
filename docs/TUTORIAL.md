# Tutorial

A walkthrough of what this tool does, what each view answers, and the routines it
is built around. If you only read one section, make it
[Reading the verdict](#reading-the-verdict) — everything else is detail.

## Contents

- [First run](#first-run)
- [Reading the verdict](#reading-the-verdict)
- [The six views](#the-six-views)
- [Reading the graph](#reading-the-graph)
- [Three routines](#three-routines)
- [The five actions](#the-five-actions)
- [Before you press anything: the safety model](#before-you-press-anything-the-safety-model)
- [One source of truth for two runtimes](#one-source-of-truth-for-two-runtimes)
- [Keeping the rules current](#keeping-the-rules-current)
- [Scheduled checks](#scheduled-checks)
- [Troubleshooting](#troubleshooting)
- [Command reference](#command-reference)

---

## First run

```bash
git clone <repo> && cd agent-config-studio
python3 -m studio.cli health
```

No install step. Python 3.11+, standard library only.

The first run scans `~/.claude` and `~/.codex`, indexes your local history, runs
56 checks and prints a verdict. It writes a report under `var/reports/` and
touches nothing else — scanning and grading never modify your configuration.

Expect the first run to take about a minute: it reads every transcript you have
to work out what you actually use. Results are cached per file, so the second run
takes about a second.

Then open the dashboard:

```bash
python3 -m studio.cli serve
```

## Reading the verdict

The dashboard leads with one line, because there is only one question that
matters day to day: **do I need to do something?**

> ✓ **Healthy — nothing needs your attention**

or

> ✕ **Unhealthy — N items need you**

Everything else on that screen is context for that sentence. In particular, three
numbers routinely look alarming and are not:

| Number | What it means |
| --- | --- |
| **vendor** | Findings inside plugin- or toolkit-supplied files. Editing them is undone by the next upgrade, so they never count against the verdict. Upgrade, remove, or waive. |
| **minor** | Improvements that do not affect behaviour: an unused plugin still enabled, a leftover backup file, a long reference file with no contents list. |
| **waived** | Findings you recorded a reason for and decided not to fix. A waiver is a decision on the record, not a mute button. |

The only number that decides the verdict is **blocking**: findings in files you
own, at important or critical severity, that you have not waived.

There is deliberately no 0–100 score. A single number invites tuning the number
instead of the configuration.

## The six views

Each tab answers a different question.

**Overview** — *Is this healthy, and is it getting better or worse?* The verdict,
the preloaded metadata cost, available updates, instruction-file sizes, and a bar
per past run. All-green across the trend means it has stayed healthy.

**Graph** — *How is this wired together?* See [Reading the graph](#reading-the-graph).

**Findings** — *What exactly is wrong and what do I do?* Every finding with its
severity, ownership, location, what is wrong, how to fix it, and a link to the
documentation the rule comes from. Filter by severity, ownership, category, or
free text. Findings that can be fixed automatically show a button; those that
cannot show *why* not.

**Plugins & updates** — *Is anything stale?* Installed version against remote,
per plugin and per toolkit, plus an update button for each. Anything that cannot
be compared is reported as **unknown**, never as up to date.

**Inventory** — *What can I use, and how?* A catalogue: one card per skill,
command, agent and workflow, saying what it does, when it fires, how you invoke
it, and where it lives. The panel above explains the trigger mechanism for that
kind. It lists only what is actually **usable** by default — items that cannot
load are behind the "all" filter and are labelled as such.

**Specs & schedule** — *Has the guidance itself moved, and is the daily check running?* Every cited document re-fetched and compared against a recorded baseline, with an optional AI review of what a change means for the rules that depend on it. Detection is deterministic; the review is an opinion and never edits a rule.

**Sync** — *Have my generated instruction files drifted?* Whether `CLAUDE.md` and
`AGENTS.md` still match their canonical sources, the pending diff, and every
restore point.

## Reading the graph

Start with what the shapes mean:

| | |
| --- | --- |
| **Colour** | The kind of thing: instruction, skill, workflow, command, agent, hook, plugin, canonical source |
| **Size** | Weight — a skill's body length, a plugin's skill count |
| **Red ring** | This file has a blocking finding |
| **Line style** | The relationship: reference, invocation, declared mirror, generated-from, undeclared duplicate, name collision |

Labels that would overlap are hidden and reappear as you zoom, so the status line
reads something like `58/265 labels`. That is working as intended, not a bug.

**Do not start by ticking "expand plugin skills."** That takes the view from a
few hundred nodes to well over a thousand and buries the config you actually
wrote. A workable order:

1. The default view already has **only connected** ticked, so what you see first
   is the real skeleton rather than a cloud of isolated dots
2. Filter by kind to `instruction` or `workflow` — see the routing structure
3. **Click a node** — the panel lists everything it connects to, and any findings
   in that file
4. Expand plugin skills only when hunting something specific, with the search box
5. If a line style is unclear, read the legend — every edge type is drawn as an
   actual sample with a one-line explanation

The graph is most useful for answering a specific question — *who uses this
skill? why are these two identical? why does nothing point at this workflow?* —
rather than for staring at the whole picture.

## Three routines

**Daily, or whenever you wonder.** Open the dashboard, read the verdict line.
That is it. If it is green, close the tab.

**After adding or editing a skill.**

```bash
python3 -m studio.cli health
```

Exits 1 if it finds something blocking, so it drops straight into a pre-commit
hook or CI if you want it there.

**Monthly, or when something feels bloated.**

```bash
python3 -m studio.cli usage      # what you actually invoke
python3 -m studio.cli fix --list # what can be cleaned up automatically
python3 -m studio.cli update     # what is out of date
python3 -m studio.cli specs      # has the guidance itself moved?
```

## The five actions

Every one of them previews before it writes, backs up what it replaces, and is
reversible with `studio rollback`.

### 1. Health check

```bash
python3 -m studio.cli health
python3 -m studio.cli health --with-updates   # also check remotes
python3 -m studio.cli health --json           # machine-readable
```

Dashboard: **Run check** in the header.

### 2. Fix

```bash
python3 -m studio.cli fix --list    # what is auto-fixable, and why the rest is not
python3 -m studio.cli fix           # preview the diff
python3 -m studio.cli fix --apply   # write it
```

Dashboard: a button per finding, plus **fix everything** for the ones safe in
bulk.

A finding is auto-fixable only when the remedy is mechanical — one sensible
outcome, no judgement about intent. Adding a contents list to a long reference
file qualifies. Splitting an oversized skill does not.

Two boundaries worth knowing:

- **Vendor-owned findings never get a button.** Writing into a file a plugin will
  overwrite wastes your time.
- **Per-item decisions stay per-item.** The rule that reports unused plugins
  flags every plugin with no recorded usage, but the classifier deliberately
  keeps some of them — a plugin can have zero recorded calls and still be one
  your instructions depend on. Those get a button each, and are excluded from
  *fix everything*.

### 3. Consolidate (uses a model)

```bash
python3 -m studio.cli consolidate                  # propose, validate, show diffs
python3 -m studio.cli consolidate --apply          # write the plans that passed
python3 -m studio.cli consolidate --only SK007 --limit 3
```

For findings with no single correct answer: which sections of an oversized skill
should move out, whether two identical files are a deliberate mirror or a
leftover.

The model proposes a plan and never touches a file. Code then checks the plan
against the file it claims to act on — do the sections exist, is one claimed
twice, does the result satisfy the rule, does the target path stay inside the
skill, is any content lost — and rejects it whole if any check fails.

Costs roughly $0.2–0.3 per finding. The command prints the total before
`--apply` writes anything.

### 4. Update

```bash
python3 -m studio.cli update                    # what is out of date and how
python3 -m studio.cli update --apply
python3 -m studio.cli update --only gstack --apply
```

Dashboard: an **update** button beside each item.

Updates run each package's own updater. Plugins go through
`claude plugin update`; git-checkout toolkits get their documented stash → fetch
→ reset → `setup` sequence, plus any version migrations they ship. The
pre-upgrade commit is captured first, so a failed setup still has somewhere to
go back to.

Plugin updates need a Claude Code restart to take effect.

### 5. Sync

```bash
python3 -m studio.cli sync           # preview
python3 -m studio.cli sync --apply   # write
```

Renders your canonical sources into `CLAUDE.md` and `AGENTS.md`, and re-syncs any
declared mirrors. See
[One source of truth for two runtimes](#one-source-of-truth-for-two-runtimes).

### Undoing anything

```bash
python3 -m studio.cli backups          # every restore point
python3 -m studio.cli rollback <id>    # put those exact bytes back
```

## Before you press anything: the safety model

Four properties, in the order they matter:

**Scanning and grading never write.** Every mutation goes through a change set
that saves the exact bytes it replaces, first.

**The dashboard is read-only until you opt in.** Write endpoints exist only when
you start it with `--allow-actions`. Even then they require a matching `Origin`
and a per-process session token, so a stray page in another tab cannot drive
them. Without the flag, buttons are disabled and the equivalent command is shown
instead.

**Heuristic rules cannot act.** 56 rules; some are exact (file sizes, hashes,
version numbers) and some are judgement-shaped patterns (does this sentence add a
verification step? do these two sections say the same thing?). The
judgement-shaped ones are wrong sometimes. None of them has an auto-fix — they
report, and a person decides.

**AI proposes, code disposes.** A model is called in exactly two places:
proposing a consolidation plan, and reading a changed specification. Both produce
proposals that are validated by code before anything happens, and neither ever
edits a rule or writes a file.

## One source of truth for two runtimes

Claude Code reads `CLAUDE.md`; Codex reads `AGENTS.md`. Keeping the same rules in
both by hand guarantees they drift. Instead, render both:

```
canonical/core.md          shared rules
canonical/claude.delta.md  Claude-specific tool names and gates
canonical/codex.delta.md   Codex-specific tool names and gates
        │  studio sync
        ├─→ ~/.claude/CLAUDE.md
        └─→ ~/.codex/AGENTS.md
```

Set it up:

```bash
cp canonical/examples/core.example.md          canonical/core.md
cp canonical/examples/claude.delta.example.md  canonical/claude.delta.md
cp canonical/examples/codex.delta.example.md   canonical/codex.delta.md
cp canonical/examples/governance.example.json  canonical/governance.json
# edit, then:
python3 -m studio.cli sync --apply
```

Edit the sources, never the rendered files. Editing a rendered file is drift, and
rule `MR003` fails on it with the first differing line.

`canonical/governance.json` also declares **mirrors** (paths that must stay
byte-identical), **vendored** paths (not yours to fix), and **waivers** (findings
acknowledged with a reason).

All of this is optional. Skip it and the tool still scans, grades, graphs and
checks updates — you just do not get rendered instruction files or drift
detection.

## Keeping the rules current

Every rule cites a published document. Those documents get revised, and a rule
written against last year's advice becomes confidently wrong with nothing
noticing.

```bash
python3 -m studio.cli specs             # has any cited document changed?
python3 -m studio.cli specs --review    # ask what changed and whether rules still hold
python3 -m studio.cli specs --accept    # record current content as the new baseline
```

Detection is deterministic: normalised hashes, no model, no judgement.
Reformatting a paragraph does not register as a change of guidance.

`--review` is the only part that calls a model, and it produces a review and
nothing else. **Rule code is never edited automatically.** A rule is a claim
about what the guidance says; that claim should change only when a person agrees
it should. `--accept` is you saying you looked.

## Scheduled checks

```bash
scripts/install-launchd.sh install   # daily 09:20
scripts/install-launchd.sh status    # loaded? drifted from the repo? last verdict?
scripts/install-launchd.sh run-now   # trigger once and wait
```

macOS only for now.

The agent deliberately runs from a copy of the package under
`~/Library/Application Support`, not from the repo: macOS TCC blocks a
LaunchAgent from reading `~/Documents`, and pointing `WorkingDirectory` there
makes Python hang in `getcwd()` before running any of your code. Shared state
lives next to the copy, and the repo's `var/reports` is a symlink to it, so the
dashboard and the scheduled run share one history. `status` reports drift between
the repo and the copy — re-run `install` after changing `studio/` or `canonical/`.

## Troubleshooting

**The graph is an unlabelled dot cloud.** You are zoomed out with too many nodes.
Check that "only connected" is ticked (it is by default), untick "expand plugin
skills", or zoom in — labels appear as space allows.

**Buttons are greyed out.** The dashboard is read-only. Restart with
`python3 -m studio.cli serve --allow-actions`.

**`health` is slow.** The first run reads your entire transcript history. Later
runs use a per-file cache and take about a second. If it is slow every time, the
cache is not being written — check that `var/` is writable.

**A finding looks wrong.** Some rules are pattern-based and do get things wrong.
Open the rule's `spec` link to see the requirement it is enforcing. If it is
genuinely a false positive, record a waiver with the reason, and consider filing
an issue with the case.

**A plugin's tools disappeared after a fix.** You disabled it. `studio backups`
then `studio rollback <id>`, or re-enable it with `claude plugin enable <name>`.

**Consolidation says "no proposal available".** The `claude` CLI is not on your
PATH. Everything except consolidation and `specs --review` works without it.

## Command reference

| Command | What it does |
| --- | --- |
| `scan` | Inventory every config root; writes `var/inventory.json` |
| `health` | Run all rules, save a report, print the verdict. Exit 1 on failure |
| `fix` | Apply the fixes that have one correct answer (`--list`, `--apply`) |
| `consolidate` | AI-planned consolidation, validated before applying (`--apply`, `--only`, `--limit`) |
| `update` | Run each package's own updater (`--only`, `--apply`) |
| `specs` | Check whether the cited guidance changed (`--review`, `--accept`) |
| `sync` | Render canonical sources into each runtime (`--apply`) |
| `graph` | Emit the relationship graph as JSON (`--expand-plugins`) |
| `usage` | Build the invocation index from local history |
| `updates` | Compare installed plugins and toolkits against their remotes |
| `apply <payload>` | Apply a saved change set |
| `backups` / `rollback <id>` | List and restore backups |
| `serve` | Start the dashboard (`--allow-actions`, `--port`) |
